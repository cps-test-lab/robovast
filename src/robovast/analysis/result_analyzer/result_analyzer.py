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

import math
import os
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QThread, QTimer, Slot
from PySide6.QtGui import QBrush, QColor, QIcon, QPalette
from PySide6.QtWidgets import (QApplication, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QMainWindow, QMenu, QProgressBar,
                               QSplitter, QStatusBar, QTabWidget, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from robovast.common import load_config
from robovast.common.results_utils import iter_run_folders

from .widgets.common import RunType
from .widgets.jupyter_widget import DataAnalysisWidget, JupyterNotebookRunner
from .widgets.local_execution_widget import LocalExecutionWidget
from .widgets.log_viewer_widget import LogViewerWidget
from .widgets.worker_thread import LatestOnlyWorker


class RunResultsAnalyzer(QMainWindow):
    def __init__(self, base_dir=None, override_vast=None):
        super().__init__()

        # Initialize QSettings for local/system-specific settings (window state, etc.)
        self.settings = QSettings("RunResultsAnalyzer", "Settings")

        # Resolve override_vast to an absolute path once so it can be compared/logged consistently
        self._override_vast = str(Path(override_vast).resolve()) if override_vast else None

        # Discover notebooks from every campaign under base_dir.
        # self.campaign_notebooks maps campaign_name -> {"workloads": [...], "config_file": str|None}
        self.campaign_notebooks = {}
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

        self.setWindowTitle(f"Run Results Analyzer - {self.base_dir}")

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
        """Scan all campaigns under *base_dir* and return per-campaign notebook info.

        When ``self._override_vast`` is set the override file (and its parent
        directory) is used for *every* campaign instead of each campaign's own
        ``_config/*.vast``.

        Returns:
            dict: ``{campaign_name: {"workloads": [...], "config_file": str|None}}``
        """
        root = Path(base_dir)
        result = {}

        # When an override is given, load it once and reuse for all campaigns.
        override_parameters = None
        override_config_dir = None
        if self._override_vast:
            override_vast_path = Path(self._override_vast)
            override_config_dir = str(override_vast_path.parent)
            try:
                override_parameters = load_config(
                    str(override_vast_path), "evaluation", allow_missing=True
                )
                print(f"Using override .vast for notebook discovery: {self._override_vast}")
            except Exception as e:
                print(f"Warning: could not load override config from {self._override_vast}: {e}")

        for campaign_item in sorted(root.iterdir()):
            if not campaign_item.is_dir() or not campaign_item.name.startswith("campaign-"):
                continue

            if self._override_vast and override_parameters is not None:
                # Use the override file for this campaign
                parameters = override_parameters
                cd = override_config_dir
                vast_path_str = self._override_vast
            else:
                # Normal path: look for a .vast file inside campaign-<id>/_config/
                config_dir = campaign_item / "_config"
                if not config_dir.is_dir():
                    continue
                vast_files = [
                    f for f in sorted(config_dir.iterdir())
                    if f.is_file() and f.suffix == ".vast"
                ]
                if not vast_files:
                    continue
                if len(vast_files) > 1:
                    names = ", ".join(f.name for f in vast_files)
                    print(f"Warning: multiple .vast files in {config_dir}: {names}. "
                          f"Using {vast_files[0].name}.")
                vast_path_str = str(vast_files[0])
                cd = str(config_dir)
                try:
                    parameters = load_config(vast_path_str, "evaluation", allow_missing=True)
                except Exception as e:
                    print(f"Warning: could not load config from {vast_path_str}: {e}")
                    continue

            workloads = []
            if "visualization" in parameters:
                for view in parameters["visualization"]:
                    for name, values in view.items():
                        try:
                            if not isinstance(values, dict):
                                continue
                            run_val = values.get("run")
                            run_nb = os.path.join(cd, run_val) if run_val else None
                            config_val = values.get("config")
                            config_nb = os.path.join(cd, config_val) if config_val else None
                            campaign_val = values.get("campaign")
                            campaign_nb = os.path.join(cd, campaign_val) if campaign_val else None
                            workloads.append(
                                JupyterNotebookRunner(
                                    name,
                                    run_nb=run_nb,
                                    config_nb=config_nb,
                                    campaign_nb=campaign_nb,
                                )
                            )
                        except Exception as e:
                            print(f"Warning: could not add notebook workload '{name}' "
                                  f"for {campaign_item.name}: {e}")

            result[campaign_item.name] = {
                "workloads": workloads,
                "config_file": vast_path_str,
            }
            print(f"Discovered campaign {campaign_item.name}: {len(workloads)} workload(s)")

        return result

    def _get_campaign_for_path(self, path):
        """Return the ``campaign-<id>`` folder name that contains *path*, or ``None``."""
        try:
            rel = Path(path).relative_to(self.base_dir)
            first = rel.parts[0] if rel.parts else None
            if first and first.startswith("campaign-"):
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
        run_type = self.get_run_type(directory_path) if directory_path.is_dir() else None

        # Create context menu
        menu = QMenu(self)
        copy_path_action = menu.addAction("Copy Path")

        # Config-level: add Copy vast cluster/local run command actions
        copy_cluster_action = None
        copy_local_action = None
        config_name = None
        if run_type == RunType.CONFIG:
            config_name = directory_path.name
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
        elif action == copy_cluster_action and run_type == RunType.CONFIG:
            clipboard = QApplication.clipboard()
            clipboard.setText(f"vast execution cluster run -r 1 -c {config_name}")
        elif action == copy_local_action and run_type == RunType.CONFIG:
            clipboard = QApplication.clipboard()
            clipboard.setText(f"vast execution local run -r 1 -c {config_name}")
        elif action == open_notebook_action:
            self.open_notebook_in_vscode(directory_path)

    def open_notebook_in_vscode(self, directory_path):
        """Open the corresponding Jupyter notebook in VS Code"""

        # Determine the run type
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
        """Populate tree from common run-folder discovery (campaign-<id>/<config>/<run-number>)."""
        try:
            base_dir = Path(directory_path)
            # Build structure from shared iterator: campaign -> config_name -> [(run_number, folder_path)]
            structure = {}
            for campaign, config_name, run_number, folder_path in iter_run_folders(str(base_dir)):
                if campaign not in structure:
                    structure[campaign] = {}
                if config_name not in structure[campaign]:
                    structure[campaign][config_name] = []
                structure[campaign][config_name].append((run_number, folder_path))

            for campaign in sorted(structure.keys()):
                campaign_path = base_dir / campaign
                campaign_item = QTreeWidgetItem(parent_item)
                campaign_item.setData(0, Qt.UserRole, str(campaign_path))
                stats = self.calculate_run_statistics(campaign_path)
                display_text = campaign
                if stats['total'] > 0:
                    unknown_str = f" ?{stats['unknown']}" if stats['unknown'] > 0 else ""
                    display_text = f"{campaign} (✓{stats['passed']} ✗{stats['failed']}{unknown_str})"
                campaign_item.setText(0, display_text)

                if max_depth < 1:
                    continue
                for config_name in sorted(structure[campaign].keys()):
                    config_path = campaign_path / config_name
                    config_item = QTreeWidgetItem(campaign_item)
                    config_item.setData(0, Qt.UserRole, str(config_path))
                    config_item.setText(0, config_name)

                    if max_depth < 2:
                        continue
                    for run_number, folder_path in sorted(structure[campaign][config_name], key=lambda x: x[0]):
                        tree_item = QTreeWidgetItem(config_item)
                        tree_item.setData(0, Qt.UserRole, str(folder_path))
                        display_text = run_number
                        if self.is_run_directory(folder_path):
                            run_status, summary = self.get_run_status(folder_path)
                            passed_bg, passed_fg = self.get_theme_colors("passed")
                            failed_bg, failed_fg = self.get_theme_colors("failed")
                            unknown_bg, unknown_fg = self.get_theme_colors("unknown")
                            suffix = f" – {summary}" if summary else ""
                            if run_status == "passed":
                                tree_item.setData(0, Qt.UserRole + 1, "passed")
                                display_text = f"✓ {run_number}{suffix}"
                                tree_item.setBackground(0, QBrush(QColor(passed_bg)))
                                tree_item.setForeground(0, QBrush(QColor(passed_fg)))
                            elif run_status == "failed":
                                tree_item.setData(0, Qt.UserRole + 1, "failed")
                                display_text = f"✗ {run_number}{suffix}"
                                tree_item.setBackground(0, QBrush(QColor(failed_bg)))
                                tree_item.setForeground(0, QBrush(QColor(failed_fg)))
                            else:
                                tree_item.setData(0, Qt.UserRole + 1, "unknown")
                                display_text = f"? {run_number}{suffix}"
                                tree_item.setBackground(0, QBrush(QColor(unknown_bg)))
                                tree_item.setForeground(0, QBrush(QColor(unknown_fg)))
                        tree_item.setText(0, display_text)

        except PermissionError:
            error_item = QTreeWidgetItem(parent_item)
            error_item.setText(0, "[Permission Denied]")
        except Exception as e:
            error_item = QTreeWidgetItem(parent_item)
            error_item.setText(0, f"[Error: {str(e)}]")

    def get_run_type(self, data_path):
        """Determine analysis type based on directory structure"""
        try:
            # Check for test.xml files in different locations to determine analysis type

            # 1. Check if test.xml exists directly in the path (single run)
            if os.path.exists(data_path / "test.xml"):
                return RunType.RUN

            # 2. Check if test.xml exist in subfolders (config level)
            run_files = list(data_path.glob("*/test.xml"))
            if run_files:
                return RunType.CONFIG

            # 3. Check if test.xml files exist in subfolders of subfolders (campaign level)
            run_files = list(data_path.glob("*/*/test.xml"))
            if run_files:
                return RunType.CAMPAIGN

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

        # Add task to worker (this will discard any pending tasks)
        self.worker.add_task(data=directory_path, run_type=self.get_run_type(directory_path))

        run_type = self.get_run_type(directory_path)

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

    def calculate_run_statistics(self, base_path):
        """Calculate run statistics for a campaign directory using common run-folder discovery."""
        stats = {'passed': 0, 'failed': 0, 'unknown': 0, 'total': 0}
        base_path = Path(base_path)
        results_dir = base_path.parent
        campaign = base_path.name

        try:
            for rid, _config, _num, folder_path in iter_run_folders(str(results_dir)):
                if rid != campaign:
                    continue
                if self.is_run_directory(folder_path):
                    run_status, _ = self.get_run_status(folder_path)
                    stats[run_status] = stats.get(run_status, 0) + 1
                    stats['total'] += 1
        except Exception as e:
            print(f"Error calculating run statistics: {e}")

        return stats

    def get_run_status(self, directory_path):
        """Get run status and optional short summary from test.xml.
        Returns (status, summary) where status is 'passed', 'failed', or 'unknown',
        and summary is a short descriptive string or None.
        """
        run_xml_path = directory_path / "test.xml"

        if not run_xml_path.exists():
            return "unknown", None

        try:
            tree = ET.parse(run_xml_path)
            root = tree.getroot()

            testsuite = root if root.tag == 'testsuite' else root.find('testsuite')
            if testsuite is not None:
                errors = int(testsuite.get('errors', 0))
                failures = int(testsuite.get('failures', 0))
                status = "passed" if (errors == 0 and failures == 0) else "failed"

                summary = None
                if status == "failed":
                    failure_text = self._get_failure_text(root)
                    summary = self._extract_failure_summary(failure_text)

                return status, summary

        except Exception as e:
            print(f"Error parsing test.xml in {directory_path}: {e}")

        return "unknown", None

    def _get_failure_text(self, root):
        """Get failure element text from parsed test.xml root."""
        for testcase in root.iter('testcase'):
            failure = testcase.find('failure')
            if failure is not None:
                return failure.text or failure.get('message', '') or ''
        return ''

    def _extract_failure_summary(self, failure_text):
        """Extract short summary from failure message text.
        Algorithm: last '[✕] -- ' -> text after on that line;
        else last '[✓] -- ' -> text after on that line;
        if single-line message and no marker found -> take it completely.
        """
        if not failure_text:
            return None
        for marker in ("[✕] -- ", "[✓] -- "):
            idx = failure_text.rfind(marker)
            if idx >= 0:
                start = idx + len(marker)
                end = failure_text.find("\n", start)
                rest = failure_text[start:end] if end >= 0 else failure_text[start:]
                s = rest.strip()
                return s if s else None
        if "\n" not in failure_text:
            s = failure_text.strip()
            return s if s else None
        return None

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
