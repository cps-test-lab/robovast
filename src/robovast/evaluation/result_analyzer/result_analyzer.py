#!/usr/bin/env python3
# Copyright (C) 2025 Frederik Pasch
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

import json
import math
import os
import subprocess
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QThread, QTimer, Slot
from PySide6.QtGui import QBrush, QColor, QIcon, QPalette
from PySide6.QtWidgets import (QApplication, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QMainWindow, QMenu, QProgressBar,
                               QSplitter, QStatusBar, QTabWidget, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from robovast.common import load_config
from robovast.common.analysis import get_run_status
from robovast.common.execution import is_campaign_dir
from robovast.common.store import STORE_FILENAME, CampaignStore

from .widgets.common import RunType
from .widgets.jupyter_widget import DataAnalysisWidget, JupyterNotebookRunner
from .widgets.local_execution_widget import LocalExecutionWidget
from .widgets.log_viewer_widget import LogViewerWidget
from .widgets.worker_thread import LatestOnlyWorker

# Per-tree-item data roles. Qt.UserRole holds the node's filesystem path and
# Qt.UserRole+1 its run status; the node's RunType and (for batch nodes) the
# batch index are stored on the item itself — the flat layout makes batch and
# campaign nodes share the same path, so the level can't be keyed by path.
_RUN_TYPE_ROLE = Qt.UserRole + 2
_BATCH_IDX_ROLE = Qt.UserRole + 3


class RunResultsAnalyzer(QMainWindow):
    def __init__(self, base_dir=None, override_vast=None):
        super().__init__()

        # Initialize QSettings for local/system-specific settings (window state, etc.)
        self.settings = QSettings("RunResultsAnalyzer", "Settings")

        # Resolve override_vast to an absolute path once so it can be compared/logged consistently
        self._override_vast = str(Path(override_vast).resolve()) if override_vast else None

        # Discover campaigns from every campaign.db under base_dir.
        # self.campaign_notebooks maps campaign_name -> {"workloads": [...], "config_file": str|None}
        # self._campaign_index maps campaign_name -> full store-derived structure
        # (mode, config_dir, batches -> units) used to build the tree.
        self.campaign_notebooks = {}
        self._campaign_index = {}
        self._current_campaign = None  # name of the campaign currently shown in the UI

        if base_dir:
            self.campaign_notebooks = self._discover_all_campaign_notebooks(base_dir)

        # Pick the most recent campaign (lexicographically last) as the initial default
        sorted_campaigns = sorted(self.campaign_notebooks.keys(), reverse=True)
        initial_campaign = sorted_campaigns[0] if sorted_campaigns else None
        self._current_campaign = initial_campaign
        initial_data = self.campaign_notebooks.get(initial_campaign, {})
        workloads = initial_data.get("workloads", [])
        self.config_file = initial_data.get("config_file", None)

        # Initialize variables to None first
        self.local_execution_widget = None
        self.tree = None
        self.log_viewer = None
        self.details_tabs = None
        self.analysis_tabs = {}

        # Worker thread setup
        self.worker_thread = QThread()

        self.worker = LatestOnlyWorker(workloads)
        self.worker.moveToThread(self.worker_thread)

        # Connect signals - note the updated signatures
        self.worker.finished.connect(self._on_worker_finished)
        self.worker.progress.connect(self._on_worker_progress_updated)
        self.worker.error.connect(self._on_worker_error)

        # Start worker loop when thread starts
        self.worker_thread.started.connect(self.worker.run_worker_loop)

        # Start the worker thread
        self.worker_thread.start()

        self.base_dir = Path(base_dir)

        self.setWindowTitle(f"RoboVAST Results Browser - {self.base_dir}")

        # Set window icon
        icon_path = Path(__file__).parent.parent.parent.parent.parent / "docs" / "images" / "icon.png"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        self.resize(1400, 900)

        self.setup_ui()
        self.load_window_state()
        self.populate_tree()

    # ------------------------------------------------------------------
    # Campaign notebook discovery
    # ------------------------------------------------------------------

    def _discover_all_campaign_notebooks(self, base_dir):
        """Scan ``base_dir/*/campaign.db`` and return per-campaign notebook info.

        The campaign store is the single source of truth: it carries the mode,
        the full config (``evaluation.visualization``) and the base ``config_dir``
        against which notebooks resolve, plus the generation/unit structure used
        to build the tree (cached in ``self._campaign_index``). When
        ``self._override_vast`` is set the override file (and its parent) is used
        for *every* campaign's notebook discovery instead of the stored config.

        Returns:
            dict: ``{campaign_name: {"workloads": [...], "config_file": str|None}}``
        """
        root = Path(base_dir)
        result = {}
        self._campaign_index = {}

        # When an override is given, load it once and reuse for all campaigns.
        override_eval = None
        override_dir = None
        if self._override_vast:
            override_dir = str(Path(self._override_vast).parent)
            try:
                override_eval = load_config(
                    self._override_vast, "evaluation", allow_missing=True)
                print(f"Using override .vast for notebook discovery: {self._override_vast}")
            except Exception as e:
                raise RuntimeError(
                    f"Could not load override config from {self._override_vast}: {e}") from e

        for store_path in sorted(root.glob(f"*/{STORE_FILENAME}")):
            campaign_dir = store_path.parent
            try:
                entry = self._read_campaign_store(campaign_dir, store_path)
            except Exception as e:  # pylint: disable=broad-except
                print(f"Warning: could not read campaign store {store_path}: {e}")
                continue

            # Resolve the evaluation block + notebook base dir (override wins).
            if override_eval is not None:
                eval_block, nb_base = override_eval, override_dir
                config_file = self._override_vast
            else:
                config_json = entry["config_json"]
                eval_block = config_json.get("evaluation") or {}
                nb_base = entry["config_dir"]
                config_file = entry["config_file"]

            entry["workloads"] = self._build_workloads(
                eval_block, nb_base, entry["name"])
            entry["config_file"] = config_file
            self._campaign_index[entry["name"]] = entry
            result[entry["name"]] = {
                "workloads": entry["workloads"],
                "config_file": config_file,
            }
            print(f"Discovered campaign {entry['name']}: "
                  f"{len(entry['workloads'])} workload(s), mode={entry['mode']}")

        return result

    def _read_campaign_store(self, campaign_dir, store_path):
        """Read one campaign store into a plain dict (mode, config, batches)."""
        with CampaignStore(store_path) as store:
            campaigns = store.list_campaigns()
            if not campaigns:
                raise ValueError("empty campaign store")
            row = campaigns[0]
            batches = []
            for b in store.batches(row["id"]):
                # Paths are recorded relative to the campaign root; resolve them
                # against this campaign's real on-disk location.
                units = [
                    {
                        "config_name": u["config_name"],
                        "status": u["status"],
                        "n_samples": u["n_samples"],
                        "objective": u["objective"],
                        "result_dir": str(campaign_dir / u["result_dir"]),
                    }
                    for u in store.units(b["id"])
                ]
                batches.append({
                    "idx": b["idx"],
                    "dir": str(campaign_dir / b["dir"]),
                    "units": units,
                })
        config_json = json.loads(row["config_json"]) if row["config_json"] else {}
        config_dir = str(campaign_dir / (row["config_dir"] or "_config"))
        # The vast for the local-execution widget: the copy in config_dir if any.
        vast_files = sorted(Path(config_dir).glob("*.vast")) if Path(config_dir).is_dir() else []
        return {
            "name": campaign_dir.name,
            "root": str(campaign_dir),
            "mode": row["mode"] or "batch",
            "config_dir": config_dir,
            "config_json": config_json,
            "config_file": str(vast_files[0]) if vast_files else None,
            "batches": batches,
        }

    def _build_workloads(self, eval_block, nb_base, campaign_name):
        """Build notebook workloads from an evaluation block resolved against nb_base."""
        workloads = []
        for view in (eval_block.get("visualization") or []):
            if not isinstance(view, dict):
                continue
            for name, values in view.items():
                if not isinstance(values, dict):
                    continue
                try:
                    def _nb(key, values=values):
                        val = values.get(key)
                        return os.path.join(nb_base, val) if val and nb_base else None
                    workloads.append(JupyterNotebookRunner(
                        name, run_nb=_nb("run"), config_nb=_nb("config"),
                        campaign_nb=_nb("campaign"), batch_nb=_nb("batch")))
                except Exception as e:  # pylint: disable=broad-except
                    print(f"Warning: could not add notebook workload '{name}' "
                          f"for {campaign_name}: {e}")
        return workloads

    def _get_campaign_for_path(self, path):
        """Return the campaign folder name that contains *path*, or ``None``."""
        try:
            rel = Path(path).relative_to(self.base_dir)
            first = rel.parts[0] if rel.parts else None
            if first and is_campaign_dir(first):
                return first
        except Exception:
            pass
        return None

    def _update_analysis_tabs_for_campaign(self, campaign_name):
        """Swap analysis tabs and worker workloads to match *campaign_name*.

        Must be called from the UI thread.  The worker's in-progress work is
        cancelled before the workload list is replaced.
        """
        if campaign_name == self._current_campaign:
            return

        campaign_data = self.campaign_notebooks.get(campaign_name, {})
        new_workloads = campaign_data.get("workloads", [])
        new_config_file = campaign_data.get("config_file", None)

        print(f"Switching campaign: {self._current_campaign} -> {campaign_name} "
              f"({len(new_workloads)} workload(s))")

        # Cancel in-progress work and install new workloads in the worker
        self.worker.set_workloads(new_workloads)

        # Rebuild analysis tabs.
        # Fixed tabs ("Logs", "Local Execution") are always at the end;
        # analysis tabs are inserted before them at positions 0..N-1.
        for _, widget in list(self.analysis_tabs.items()):
            idx = self.details_tabs.indexOf(widget)
            if idx >= 0:
                self.details_tabs.removeTab(idx)
            try:
                widget.setParent(None)
                widget.deleteLater()
            except Exception:
                pass
        self.analysis_tabs.clear()

        for i, workload in enumerate(new_workloads):
            analysis_tab = DataAnalysisWidget()
            self.analysis_tabs[workload.name] = analysis_tab
            self.details_tabs.insertTab(i, analysis_tab, workload.name)

        # Update the config file used by the local execution widget
        self.config_file = new_config_file
        if self.local_execution_widget and hasattr(self.local_execution_widget, "update_config_file"):
            self.local_execution_widget.update_config_file(new_config_file)

        self._current_campaign = campaign_name

    def setup_ui(self):
        """Setup the main UI"""
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        # Setup status bar with progress bar
        self.setup_status_bar()

        # Main layout
        main_layout = QHBoxLayout(central_widget)

        # Main splitter
        main_splitter = QSplitter(Qt.Horizontal)
        main_layout.addWidget(main_splitter)

        # Left panel splitter (tree + video)
        left_splitter = QSplitter(Qt.Vertical)
        main_splitter.addWidget(left_splitter)

        # Tree widget
        self.setup_tree_widget(left_splitter)

        # Right panel (details)
        self.setup_details_panel(main_splitter)

        # Set splitter proportions
        main_splitter.setSizes([200, 1200])
        left_splitter.setSizes([400, 300])  # tree, video

    def setup_status_bar(self):
        """Setup status bar with progress bar"""
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)

        # Create progress bar for status bar
        self.worker_progress_bar = QProgressBar()
        self.worker_progress_bar.setRange(0, 0)  # Indeterminate progress initially
        self.worker_progress_bar.setMaximumWidth(300)
        self.worker_progress_bar.setMaximumHeight(30)

        # Create a separate status label
        self.status_label = QLabel("Ready")

        # Add progress bar to status bar (left side)
        self.status_bar.addWidget(self.worker_progress_bar)

        # Add status label to status bar (next to progress bar)
        self.status_bar.addWidget(self.status_label)

        # Initially hide the progress bar
        self.worker_progress_bar.hide()

    def setup_tree_widget(self, parent):
        """Setup the tree widget"""
        tree_group = QGroupBox("Run Results")
        tree_layout = QVBoxLayout(tree_group)

        # Add search bar for runs
        search_layout = QHBoxLayout()
        search_label = QLabel("Search campaigns:")
        search_layout.addWidget(search_label)
        self.campaign_search_input = QLineEdit()
        self.campaign_search_input.setPlaceholderText("Type to filter runs...")
        self.campaign_search_input.textChanged.connect(self.filter_tree_items)
        search_layout.addWidget(self.campaign_search_input)
        tree_layout.addLayout(search_layout)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Run Results"])
        self.tree.itemSelectionChanged.connect(self.on_tree_selection_changed)

        # Enable context menu
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.show_tree_context_menu)

        tree_layout.addWidget(self.tree)
        parent.addWidget(tree_group)

    def filter_tree_items(self):
        """Filter tree items based on search bar input"""
        search_text = self.campaign_search_input.text().strip().lower()

        def filter_item(item):
            # Search in both the visible text and the UserRole data
            visible = search_text in item.text(0).lower()

            # Also search in UserRole data if it exists
            user_data = item.data(0, Qt.UserRole)
            if user_data and isinstance(user_data, str):
                visible = visible or search_text in user_data.lower()

            item.setHidden(not visible)
            for i in range(item.childCount()):
                child = item.child(i)
                child_visible = filter_item(child)
                visible = visible or child_visible
            item.setHidden(not visible)
            return visible
        root = self.tree.invisibleRootItem()
        for i in range(root.childCount()):
            filter_item(root.child(i))

    def show_tree_context_menu(self, position):
        """Show context menu for tree widget"""
        # Get the item at the position
        item = self.tree.itemAt(position)
        if not item:
            return

        # Get the path from the item data
        item_path = item.data(0, Qt.UserRole)
        if not item_path:
            return

        directory_path = Path(item_path)
        run_type = self._item_run_type(item)

        # Create context menu
        menu = QMenu(self)
        copy_path_action = menu.addAction("Copy Path")

        # Add "Open Folder" action if applicable
        open_folder_action = None
        if directory_path.is_dir():
            open_folder_action = menu.addAction("Open Folder")

        # Config-level: add Copy vast cluster/local run command actions
        copy_cluster_action = None
        copy_local_action = None
        config_name = None
        vast_file = None
        if run_type == RunType.CONFIG:
            config_name = directory_path.name
            campaign_name = self._get_campaign_for_path(directory_path)
            if campaign_name:
                vast_file = self.campaign_notebooks.get(campaign_name, {}).get("config_file")
            copy_cluster_action = menu.addAction("Copy vast cluster run command")
            copy_local_action = menu.addAction("Copy vast local run command")

        # Add "Open Notebook in VS Code" action if applicable
        open_notebook_action = None
        if directory_path.is_dir() and run_type is not None:
            open_notebook_action = menu.addAction("Open Notebook in VS Code")

        # Show menu and get selected action
        action = menu.exec(self.tree.viewport().mapToGlobal(position))

        # Handle the action
        if action == copy_path_action:
            clipboard = QApplication.clipboard()
            clipboard.setText(str(item_path))
        elif action == open_folder_action:
            subprocess.Popen(["xdg-open", str(directory_path)])
        elif action == copy_cluster_action and run_type == RunType.CONFIG:
            clipboard = QApplication.clipboard()
            vast_flag = f" -V {vast_file}" if vast_file else ""
            clipboard.setText(f"vast{vast_flag} exec cluster run -c {config_name}")
        elif action == copy_local_action and run_type == RunType.CONFIG:
            clipboard = QApplication.clipboard()
            vast_flag = f" -V {vast_file}" if vast_file else ""
            clipboard.setText(f"vast{vast_flag} exec local run -c {config_name}")
        elif action == open_notebook_action:
            self.open_notebook_in_vscode(directory_path, run_type)

    def open_notebook_in_vscode(self, directory_path, run_type=None):
        """Open the corresponding Jupyter notebook in VS Code"""

        # Level is taken from the clicked item; fall back to the path-based guess.
        if run_type is None:
            run_type = self.get_run_type(directory_path)
        if run_type is None:
            self.status_label.setText("No notebook found for this directory")
            QTimer.singleShot(2000, lambda: self.status_label.setText("Ready"))
            return

        # Find the appropriate notebook from the current campaign's workloads
        notebook_path = None
        campaign_data = self.campaign_notebooks.get(self._current_campaign, {})
        for workload in campaign_data.get("workloads", []):
            if run_type == RunType.RUN and workload.run_nb:
                notebook_path = workload.run_nb
                break
            elif run_type == RunType.CONFIG and workload.config_nb:
                notebook_path = workload.config_nb
                break
            elif run_type == RunType.BATCH and workload.batch_nb:
                notebook_path = workload.batch_nb
                break
            elif run_type == RunType.CAMPAIGN and workload.campaign_nb:
                notebook_path = workload.campaign_nb
                break

        if notebook_path and Path(notebook_path).exists():
            try:
                # Open the notebook in VS Code
                subprocess.Popen(['code', str(notebook_path)])
                self.status_label.setText(f"Opening notebook in VS Code: {Path(notebook_path).name}")
                QTimer.singleShot(2000, lambda: self.status_label.setText("Ready"))
            except Exception as e:
                self.status_label.setText(f"Error opening notebook: {e}")
                QTimer.singleShot(3000, lambda: self.status_label.setText("Ready"))
        else:
            self.status_label.setText("Notebook file not found")
            QTimer.singleShot(2000, lambda: self.status_label.setText("Ready"))

    @Slot()
    def refresh_tree(self):
        """Refresh the tree widget by clearing and repopulating it"""
        if self.tree:
            # Clear the search filter first
            self.campaign_search_input.clear()

            # Clear the tree
            self.tree.clear()

            # Repopulate the tree
            self.populate_tree()

            # Show status message
            self.status_label.setText("Tree refreshed")
            QTimer.singleShot(2000, lambda: self.status_label.setText("Ready"))

    def load_window_state(self):
        """Load window state from settings"""
        remember_state = True  # self.config.get_bool("general", "remember_window_state")
        if remember_state:
            # Restore window geometry
            geometry = self.settings.value("window/geometry")
            if geometry:
                self.restoreGeometry(geometry)

            # Restore window state
            state = self.settings.value("window/state")
            if state:
                self.restoreState(state)

    def save_window_state(self):
        """Save window state to settings"""
        remember_state = True  # self.config.get_bool("general", "remember_window_state")
        if remember_state:
            self.settings.setValue("window/geometry", self.saveGeometry())
            self.settings.setValue("window/state", self.saveState())
            self.settings.sync()

    def get_theme_colors(self, status_type):
        """Get theme-appropriate colors for run status"""
        # Check if we're in dark mode by looking at the window background
        palette = self.palette()
        bg_color = palette.color(QPalette.ColorRole.Window)
        is_dark_theme = bg_color.lightness() < 128

        if is_dark_theme:
            # Dark theme colors - more subtle, less saturated
            colors = {
                "passed": ("#1a4d2e", "#90ee90"),    # Dark green bg, light green text
                "failed": ("#4d1a1a", "#ffb3b3"),    # Dark red bg, light red text
                "unknown": ("#4d4d1a", "#ffff99")    # Dark yellow bg, light yellow text
            }
        else:
            # Light theme colors - original colors but slightly more subtle
            colors = {
                "passed": ("#e8f5e8", "#2d5a2d"),    # Light green bg, dark green text
                "failed": ("#ffe8e8", "#5a2d2d"),    # Light red bg, dark red text
                "unknown": ("#fff8e1", "#5a5a2d")    # Light yellow bg, dark yellow text
            }

        return colors[status_type]

    def setup_details_panel(self, parent):
        """Setup the details panel"""
        details_group = QGroupBox("Details")
        details_layout = QVBoxLayout(details_group)

        # Tab widget
        self.details_tabs = QTabWidget()
        details_layout.addWidget(self.details_tabs)

        # Create analysis tabs from the initial campaign's workloads
        initial_data = self.campaign_notebooks.get(self._current_campaign, {})
        for workload in initial_data.get("workloads", []):
            analysis_tab = DataAnalysisWidget()
            self.analysis_tabs[workload.name] = analysis_tab
            self.details_tabs.addTab(analysis_tab, workload.name)

        # Log content tab
        self.log_viewer = LogViewerWidget()
        self.details_tabs.addTab(self.log_viewer, "Logs")

        # Local execution tab
        try:
            self.local_execution_widget = LocalExecutionWidget(self.config_file)
        except Exception as e:
            print(f"Failed to create local execution widget: {e}")
            self.local_execution_widget = QLabel("Local execution widget initialization failed")
            self.local_execution_widget.setAlignment(Qt.AlignCenter)
            self.local_execution_widget.setStyleSheet("background-color: #f8f9fa; color: red; border: 1px solid gray;")

        self.details_tabs.addTab(self.local_execution_widget, "Local Execution")

        parent.addWidget(details_group)

    def populate_tree(self):
        """Populate the tree with directory structure"""
        if not self.base_dir.exists():
            item = QTreeWidgetItem(self.tree)
            item.setText(0, f"Directory not found: {self.base_dir}")
            print(f"Directory not found: {self.base_dir}")
            return

        self.populate_directory(self.tree.invisibleRootItem(), self.base_dir)

        # Apply auto-expand setting
        self.tree.expandAll()

    def populate_directory(self, parent_item, directory_path, max_depth=2, current_depth=0):
        """Populate the tree from the campaign stores (store-driven, no FS-walk).

        Structure comes entirely from ``self._campaign_index`` (campaign ->
        [batch, search only] -> config/unit); only the run-level leaves are
        enumerated from disk, via each unit's recorded ``result_dir``. Each node's
        :class:`RunType` (and the batch index, for batch nodes) is stored on the
        item, so selection resolves the level from the clicked node — not from its
        path, which the flat layout shares between the campaign and batch nodes.
        """
        try:
            for name in sorted(self._campaign_index.keys(), reverse=True):
                entry = self._campaign_index[name]
                campaign_path = entry["root"]
                campaign_item = QTreeWidgetItem(parent_item)
                campaign_item.setData(0, Qt.UserRole, campaign_path)
                campaign_item.setData(0, _RUN_TYPE_ROLE, RunType.CAMPAIGN)
                stats = self._campaign_stats(entry)
                display_text = name
                if stats["total"] > 0:
                    mixed_str = f" ~{stats['mixed']}" if stats["mixed"] > 0 else ""
                    display_text = (f"{name} (✓{stats['passed']} "
                                    f"✗{stats['failed']}{mixed_str})")
                campaign_item.setText(0, display_text)

                is_search = entry["mode"] == "search"
                for batch in entry["batches"]:
                    container = campaign_item
                    if is_search:
                        batch_path = batch["dir"] or campaign_path
                        batch_item = QTreeWidgetItem(campaign_item)
                        batch_item.setData(0, Qt.UserRole, batch_path)
                        batch_item.setData(0, _RUN_TYPE_ROLE, RunType.BATCH)
                        batch_item.setData(0, _BATCH_IDX_ROLE, batch["idx"])
                        batch_item.setText(0, f"batch-{batch['idx']}")
                        container = batch_item
                    for unit in batch["units"]:
                        result_dir = unit["result_dir"]
                        config_item = QTreeWidgetItem(container)
                        config_item.setData(0, Qt.UserRole, result_dir)
                        config_item.setData(0, _RUN_TYPE_ROLE, RunType.CONFIG)
                        label = unit["config_name"]
                        if unit.get("objective") is not None:
                            label = f"{label}  [{unit['objective']:.4g}]"
                        config_item.setText(0, self._decorate_status(label, unit["status"]))
                        self._apply_status_color(config_item, unit["status"])
                        for run_dir in self._list_run_dirs(result_dir):
                            self._add_run_item(config_item, run_dir)

        except Exception as e:  # pylint: disable=broad-except
            error_item = QTreeWidgetItem(parent_item)
            error_item.setText(0, f"[Error: {str(e)}]")

    @staticmethod
    def _list_run_dirs(result_dir):
        """Numeric run subdirectories of a unit's result dir, ascending."""
        try:
            return sorted(
                (d for d in Path(result_dir).iterdir() if d.is_dir() and d.name.isdigit()),
                key=lambda d: int(d.name))
        except (OSError, ValueError):
            return []

    def _add_run_item(self, config_item, run_dir):
        """Add one run leaf under a config node, colored by its status."""
        tree_item = QTreeWidgetItem(config_item)
        tree_item.setData(0, Qt.UserRole, str(run_dir))
        tree_item.setData(0, _RUN_TYPE_ROLE, RunType.RUN)
        run_number = run_dir.name
        display_text = run_number
        if self.is_run_directory(run_dir):
            run_status, summary = get_run_status(run_dir)
            suffix = f" – {summary}" if summary else ""
            mark = {"passed": "✓", "failed": "✗"}.get(run_status, "?")
            status_key = run_status if run_status in ("passed", "failed") else "unknown"
            tree_item.setData(0, Qt.UserRole + 1, status_key)
            display_text = f"{mark} {run_number}{suffix}"
            self._apply_status_color(tree_item, status_key)
        tree_item.setText(0, display_text)

    def _apply_status_color(self, item, status):
        """Color a tree item by a passed/failed/other status."""
        key = {"passed": "passed", "failed": "failed"}.get(status, "unknown")
        bg, fg = self.get_theme_colors(key)
        item.setBackground(0, QBrush(QColor(bg)))
        item.setForeground(0, QBrush(QColor(fg)))

    @staticmethod
    def _decorate_status(label, status):
        mark = {"passed": "✓", "failed": "✗", "mixed": "~"}.get(status, "?")
        return f"{mark} {label}"

    @staticmethod
    def _campaign_stats(entry):
        """Count a campaign's units by aggregate status (config-level)."""
        stats = {"passed": 0, "failed": 0, "mixed": 0, "total": 0}
        for batch in entry["batches"]:
            for unit in batch["units"]:
                stats["total"] += 1
                status = unit["status"]
                if status in ("passed", "failed", "mixed"):
                    stats[status] += 1
        return stats

    @staticmethod
    def _item_run_type(item):
        """The :class:`RunType` stored on a tree item (None if absent)."""
        return item.data(0, _RUN_TYPE_ROLE) if item is not None else None

    def get_run_type(self, data_path):
        """Fallback run-level check from a path (a ``test.xml`` directly present).

        Node levels are read from the item via :meth:`_item_run_type`; this is only
        a fallback for callers that have a path but no item.
        """
        try:
            if os.path.exists(Path(data_path) / "test.xml"):
                return RunType.RUN
        except Exception:
            return None
        return None

    def on_tree_selection_changed(self):
        """Handle tree selection changes"""
        current_item = self.tree.currentItem()
        if not current_item:
            return

        item_path_str = current_item.data(0, Qt.UserRole)
        if not item_path_str:
            return

        directory_path = Path(item_path_str)

        if not directory_path.is_dir():
            print("Invalid selection: not a directory")
            QTimer.singleShot(0, self.refresh_tree)
            return

        # Show loading status in status bar instead of loading widget
        self.worker_progress_bar.show()
        self.status_label.setText("Starting analysis...")
        # self.worker_progress_bar.setFormat("Initializing...")
        self.worker_progress_bar.setRange(0, 0)  # Indeterminate

        # If the selected item belongs to a different campaign, update tabs & workloads
        campaign_name = self._get_campaign_for_path(directory_path)
        if campaign_name and campaign_name != self._current_campaign:
            self._update_analysis_tabs_for_campaign(campaign_name)

        # self.data_analysis_widget.clear_output()
        for _, widget in self.analysis_tabs.items():
            widget.clear_output()
            widget.show_execution_no_progress("Waiting for data...")

        # Level comes from the clicked item (campaign and batch nodes share a path).
        # Note RunType.RUN == 0 is falsy, so compare against None explicitly.
        run_type = self._item_run_type(current_item)
        if run_type is None:
            run_type = self.get_run_type(directory_path)

        # For a batch node, tell the notebook which batch it is (configs are flat
        # under the campaign root, so DATA_DIR alone can't identify the batch).
        inject = None
        if run_type == RunType.BATCH:
            batch_idx = current_item.data(0, _BATCH_IDX_ROLE)
            if batch_idx is not None:
                inject = {"BATCH": batch_idx}

        # Add task to worker (this will discard any pending tasks)
        self.worker.add_task(data=directory_path, run_type=run_type, inject=inject)

        # Update local execution widget
        self.local_execution_widget.setDisabled(run_type != RunType.RUN)
        self.local_execution_widget.set_run_directory(directory_path)

        # Update log files
        self.log_viewer.setDisabled(run_type != RunType.RUN)
        logs_dir = directory_path / "logs"
        if logs_dir.exists():
            self.log_viewer.set_logs_directory(logs_dir)
        else:
            self.log_viewer.clear()

    @Slot(int, str, str)
    def _on_worker_progress_updated(self, percentage, workload_name, status):
        """Handle worker progress updates"""
        # Show progress bar when there's active progress
        self.worker_progress_bar.show()

        # Update status bar message
        self.status_label.setText(f"{workload_name}: {status}")

        # Update progress bar with text and percentage
        self.worker_progress_bar.setRange(0, 100)
        self.worker_progress_bar.setValue(percentage)
        # self.worker_progress_bar.setFormat(f"{workload_name}: {percentage}%")

        print(f"Rosbag analysis progress: {percentage}% ({workload_name}): {status}")

    @Slot(str, str)
    def _on_worker_error(self, error_message, workload_name):
        """Handle rosbag analysis errors"""
        print(f"Worker error ({workload_name}): {error_message}")

        # Show progress bar to display error state
        self.worker_progress_bar.show()

        # Update status bar and progress bar to show error
        self.status_label.setText(f"Error: {error_message}")
        # self.worker_progress_bar.setFormat(f"Error: {workload_name}")
        self.worker_progress_bar.setRange(0, 0)  # Indeterminate to show error state

        # Reset to ready state after 5 seconds
        QTimer.singleShot(5000, self._reset_status_to_ready)

    def _reset_status_to_ready(self):
        """Reset status bar and progress bar to ready state"""
        self.status_label.setText("Ready")
        # self.worker_progress_bar.setFormat("Ready")
        self.worker_progress_bar.setRange(0, 0)
        self.worker_progress_bar.setValue(0)
        # Hide progress bar when not actively loading
        self.worker_progress_bar.hide()

    @Slot(object, str, str)
    def _on_worker_finished(self, result, data, task_id, workload_name):
        """Handle analysis completion from different workloads"""

        print(f"Received result from {workload_name}. {result}")
        if not result:
            print(f"  Result: {data}")

        try:
            if workload_name in self.analysis_tabs:
                analysis_tab = self.analysis_tabs[workload_name]

                # Handle Jupyter notebook execution results
                if result:
                    print(f"Jupyter notebook execution completed successfully.")
                    self.worker_progress_bar.show()  # Show briefly to indicate completion
                    self.status_label.setText(f"Jupyter analysis completed successfully")
                    # self.worker_progress_bar.setFormat("Analysis Complete")
                else:
                    print(f"Jupyter notebook execution failed.")
                    self.worker_progress_bar.show()  # Show briefly to indicate failure
                    self.status_label.setText(f"Jupyter analysis failed")
                    # self.worker_progress_bar.setFormat("Analysis Failed")
                analysis_tab.display_html(data)

            # If this is the last analysis tab, trigger the timer
            tab_names = list(self.analysis_tabs.keys())
            if workload_name == tab_names[-1]:
                QTimer.singleShot(100, self._reset_status_to_ready)

        except Exception as e:
            print(f"Error processing analysis results from {workload_name}: {e}")
            self.worker_progress_bar.show()  # Show to indicate error state
            self.status_label.setText(f"Error processing results from {workload_name}: {e}")
            # self.worker_progress_bar.setFormat(f"Error: {workload_name}")
            QTimer.singleShot(3000, self._reset_status_to_ready)

    def is_run_directory(self, directory_path):
        """Check if directory is a run directory"""
        return (directory_path / "test.xml").exists()

    @staticmethod
    def format_size(size_bytes):
        """Format file size"""
        if size_bytes == 0:
            return "0 B"

        size_names = ["B", "KB", "MB", "GB", "TB"]
        i = int(math.floor(math.log(size_bytes, 1024)))
        p = math.pow(1024, i)
        s = round(size_bytes / p, 2)
        return f"{s} {size_names[i]}"

    def closeEvent(self, event):
        """Unified clean shutdown and cleanup.

        Actions performed:
        - save window state
        - stop local execution widget (if present)
        - cleanup UI widgets (tree, log viewer, analysis tabs)
        - stop background worker and shutdown worker thread safely
        - accept the close event
        """
        print("Shutting down...")

        try:
            # Save persistent window state
            try:
                self.save_window_state()
            except Exception:
                pass

            self.local_execution_widget.stop_execution()

            # Clear and teardown UI widgets to avoid segfaults on exit
            try:
                if hasattr(self, 'tree') and self.tree:
                    self.tree.clear()
                if hasattr(self, 'log_viewer') and self.log_viewer:
                    try:
                        self.log_viewer.clear()
                    except Exception:
                        pass

                # Explicitly delete the local execution widget
                if hasattr(self, 'local_execution_widget') and self.local_execution_widget:
                    try:
                        self.local_execution_widget.setParent(None)
                        self.local_execution_widget.deleteLater()
                    except Exception:
                        pass
                    self.local_execution_widget = None

                # Cleanup analysis tabs (Jupyter widgets etc.)
                for analysis_tab in list(self.analysis_tabs.values()):
                    try:
                        if analysis_tab:
                            cleanup = getattr(analysis_tab, 'cleanup', None)
                            if callable(cleanup):
                                cleanup()
                            analysis_tab.setParent(None)
                            analysis_tab.deleteLater()
                    except Exception:
                        pass
                self.analysis_tabs.clear()
            except Exception as e:
                print(f"Error during UI teardown: {e}")

            # Stop background worker if present
            try:
                if hasattr(self, 'worker') and self.worker:
                    stop = getattr(self.worker, 'stop', None)
                    if callable(stop):
                        self.worker.stop()
            except Exception as e:
                print(f"Error stopping worker: {e}")

            # Shutdown worker thread
            try:
                if hasattr(self, 'worker_thread') and self.worker_thread:
                    # Ask thread to quit and wait a short while
                    try:
                        self.worker_thread.quit()
                    except Exception:
                        pass
                    if not self.worker_thread.wait(3000):  # Wait up to 3 seconds
                        print("Warning: Worker thread didn't stop gracefully")
                        try:
                            self.worker_thread.terminate()
                            self.worker_thread.wait()
                        except Exception:
                            pass
            except Exception as e:
                print(f"Error shutting down worker thread: {e}")

        except Exception as e:
            print(f"Error during shutdown: {e}")
        finally:
            # Ensure the event is accepted so the application can close
            try:
                event.accept()
            except Exception:
                pass
