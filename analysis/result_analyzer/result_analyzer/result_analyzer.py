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

import argparse
import math
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QThread, QTimer
from PySide6.QtCore import Slot
from PySide6.QtGui import QBrush, QColor, QPalette
from PySide6.QtWidgets import (QApplication, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QMainWindow, QProgressBar,
                               QPushButton, QSplitter, QStatusBar, QTabWidget,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                               QWidget)
from robovast_common import load_config

from .widgets.common import RunType
from .widgets.jupyter_widget import DataAnalysisWidget, JupyterNotebookRunner
from .widgets.local_execution_widget import LocalExecutionWidget
from .widgets.log_viewer_widget import LogViewerWidget
from .widgets.worker_thread import LatestOnlyWorker

class TestResultsAnalyzer(QMainWindow):
    def __init__(self, base_dir=None, config_file=None):
        super().__init__()

        # Initialize QSettings for local/system-specific settings (window state, etc.)
        self.settings = QSettings("TestResultsAnalyzer", "Settings")

        # Initialize configuration for shared settings
        self.config_file = config_file
        self.parameters = load_config(config_file, "analysis")

        # Initialize variables to None first
        self.local_execution_widget = None
        # self.data_analysis_widget = None
        self.tree = None
        # self.overview_text = None
        self.log_viewer = None
        self.details_tabs = None
        self.analysis_tabs = {}

        # Worker thread setup
        self.worker_thread = QThread()

        workloads = []
        for view in self.parameters:
            for name, values in view.items():
                try:
                    if not isinstance(values, dict):
                        continue
                    single_nb = os.path.join(os.path.dirname(config_file), values.get("single_test"))
                    variant_nb = os.path.join(os.path.dirname(config_file), values.get("variant"))
                    run_nb = os.path.join(os.path.dirname(config_file), values.get("run"))
                    workloads.append(
                        JupyterNotebookRunner(name,
                                              single_test_nb=single_nb,
                                              variant_nb=variant_nb,
                                              run_nb=run_nb)
                    )
                except Exception as e:
                    print(f"Error adding notebook workload for {name}: {e}")
                    sys.exit(1)

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

        self.setWindowTitle(f"Test Results Analyzer - {self.base_dir}")
        self.resize(1400, 900)

        self.setup_ui()
        self.load_window_state()
        self.populate_tree()

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
        tree_group = QGroupBox("Test Results Directory")
        tree_layout = QVBoxLayout(tree_group)

        # Add header with settings button
        header_layout = QHBoxLayout()

        # Title label
        title_label = QLabel("Test Results")
        title_label.setStyleSheet("font-weight: bold; font-size: 12px;")
        header_layout.addWidget(title_label)

        # Spacer to push buttons to the right
        header_layout.addStretch()

        # Refresh button
        refresh_button = QPushButton("🔄")
        refresh_button.setToolTip("Refresh Tree")
        refresh_button.setFixedSize(24, 24)
        refresh_button.clicked.connect(self.refresh_tree)
        header_layout.addWidget(refresh_button)

        tree_layout.addLayout(header_layout)

        # Add search bar for runs
        search_layout = QHBoxLayout()
        search_label = QLabel("Search runs:")
        search_layout.addWidget(search_label)
        self.run_search_input = QLineEdit()
        self.run_search_input.setPlaceholderText("Type to filter runs...")
        self.run_search_input.textChanged.connect(self.filter_tree_items)
        search_layout.addWidget(self.run_search_input)
        tree_layout.addLayout(search_layout)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Test Results"])
        self.tree.itemSelectionChanged.connect(self.on_tree_selection_changed)

        tree_layout.addWidget(self.tree)
        parent.addWidget(tree_group)

    def filter_tree_items(self):
        """Filter tree items based on search bar input"""
        search_text = self.run_search_input.text().strip().lower()

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

    @Slot()
    def refresh_tree(self):
        """Refresh the tree widget by clearing and repopulating it"""
        if self.tree:
            # Clear the search filter first
            self.run_search_input.clear()

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
        """Get theme-appropriate colors for test status"""
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
        details_group = QGroupBox("Test Run Details")
        details_layout = QVBoxLayout(details_group)

        # Tab widget
        self.details_tabs = QTabWidget()
        details_layout.addWidget(self.details_tabs)

        for view in self.parameters:
            for name, _ in view.items():
                try:
                    analysis_tab = DataAnalysisWidget()
                    self.analysis_tabs[name] = analysis_tab
                    self.details_tabs.addTab(analysis_tab, name)
                except Exception as e:
                    print(f"Error adding notebook workload for {name}: {e}")
                    sys.exit(1)

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
        """Recursively populate directory with depth limit"""
        try:
            items = sorted(directory_path.iterdir(), key=lambda x: (not x.is_dir(), x.name))

            for item_path in items:
                # Skip hidden files and directories
                if item_path.name.startswith('.'):
                    continue
                # At root level, only show run- directories
                if current_depth == 0 and item_path.is_dir() and not item_path.name.startswith('run-'):
                    continue
                # Create tree item for visible path
                tree_item = QTreeWidgetItem(parent_item)
                tree_item.setData(0, Qt.UserRole, str(item_path))

                if item_path.is_dir():
                    display_text = item_path.name

                    # Check if this is a run- folder and add statistics
                    if item_path.name.startswith("run-"):
                        stats = self.calculate_test_statistics(item_path)
                        if stats['total'] > 0:
                            unknown_str = f" ?{stats['unknown']}" if stats['unknown'] > 0 else ""
                            display_text = f"{item_path.name} (✓{stats['passed']} ✗{stats['failed']}{unknown_str})"
                    elif self.is_test_directory(item_path):
                        test_status = self.get_test_status(item_path)

                        # Get theme-aware colors
                        passed_bg, passed_fg = self.get_theme_colors("passed")
                        failed_bg, failed_fg = self.get_theme_colors("failed")
                        unknown_bg, unknown_fg = self.get_theme_colors("unknown")

                        if test_status == "passed":
                            tree_item.setData(0, Qt.UserRole + 1, "passed")
                            display_text = f"✓ {item_path.name}"
                            tree_item.setBackground(0, QBrush(QColor(passed_bg)))
                            tree_item.setForeground(0, QBrush(QColor(passed_fg)))
                        elif test_status == "failed":
                            tree_item.setData(0, Qt.UserRole + 1, "failed")
                            display_text = f"✗ {item_path.name}"
                            tree_item.setBackground(0, QBrush(QColor(failed_bg)))
                            tree_item.setForeground(0, QBrush(QColor(failed_fg)))
                        else:
                            tree_item.setData(0, Qt.UserRole + 1, "unknown")
                            display_text = f"? {item_path.name}"
                            tree_item.setBackground(0, QBrush(QColor(unknown_bg)))
                            tree_item.setForeground(0, QBrush(QColor(unknown_fg)))

                    tree_item.setText(0, display_text)

                    # Only recurse if we haven't reached max depth
                    if current_depth < max_depth:
                        self.populate_directory(tree_item, item_path, max_depth, current_depth + 1)

        except PermissionError:
            error_item = QTreeWidgetItem(parent_item)
            error_item.setText(0, "[Permission Denied]")
        except Exception as e:
            error_item = QTreeWidgetItem(parent_item)
            error_item.setText(0, f"[Error: {str(e)}]")

    def get_run_type(self, data_path):
        """Determine analysis type based on directory structure"""
        try:
            # Check for run.yaml files in different locations to determine analysis type

            # 1. Check if run.yaml exists directly in the path (single run)
            if os.path.exists(data_path / "run.yaml"):
                return RunType.SINGLE_TEST

            # 2. Check if run.yamls exist in subfolders (folder run)
            run_files = list(data_path.glob("*/run.yaml"))
            if run_files:
                return RunType.SINGLE_VARIANT

            # 3. Check if CSV files exist in subfolders of subfolders (whole run)
            run_files = list(data_path.glob("*/*/run.yaml"))
            if run_files:
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

        # self.data_analysis_widget.clear_output()
        for _, widget in self.analysis_tabs.items():
            widget.clear_output()
            widget.show_execution_no_progress("Waiting for data...")

        # Add task to worker (this will discard any pending tasks)
        self.worker.add_task(data=directory_path, run_type=self.get_run_type(directory_path))

        run_type = self.get_run_type(directory_path)

        # Update local execution widget
        self.local_execution_widget.setDisabled(run_type != RunType.SINGLE_TEST)
        self.local_execution_widget.set_test_directory(directory_path)

        # Update log files
        self.log_viewer.setDisabled(run_type != RunType.SINGLE_TEST)
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

    def calculate_test_statistics(self, base_path):
        """Calculate test statistics for a directory"""
        stats = {'passed': 0, 'failed': 0, 'unknown': 0, 'total': 0}

        try:
            for item in base_path.rglob("*"):
                if item.is_dir() and self.is_test_directory(item):
                    test_status = self.get_test_status(item)
                    stats[test_status] = stats.get(test_status, 0) + 1
                    stats['total'] += 1
        except Exception as e:
            print(f"Error calculating test statistics: {e}")

        return stats

    def get_test_status(self, directory_path):
        """Get test status from test.xml"""
        test_xml_path = directory_path / "test.xml"

        if not test_xml_path.exists():
            return "unknown"

        try:
            tree = ET.parse(test_xml_path)
            root = tree.getroot()

            testsuite = root if root.tag == 'testsuite' else root.find('testsuite')
            if testsuite is not None:
                errors = int(testsuite.get('errors', 0))
                failures = int(testsuite.get('failures', 0))

                return "passed" if (errors == 0 and failures == 0) else "failed"

        except Exception as e:
            print(f"Error parsing test.xml in {directory_path}: {e}")

        return "unknown"

    def is_test_directory(self, directory_path):
        """Check if directory is a test directory"""
        test_indicators = ["test.xml", "capture.mp4", "scenario.osc", "scenario.variant"]
        indicator_count = sum(1 for indicator in test_indicators
                              if (directory_path / indicator).exists())
        return indicator_count >= 2

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

    


def main():
    """Main function"""
    parser = argparse.ArgumentParser(description="Test Results Analyzer GUI")
    parser.add_argument("--results-dir", type=str, required=True,
                        help="Directory containing test results")
    parser.add_argument("--config", type=str, required=True,
                        help="Path to .vast configuration file")

    args = parser.parse_args()

    app = QApplication(sys.argv)
    app.setStyle('Fusion')  # Modern look

    try:
        window = TestResultsAnalyzer(base_dir=args.results_dir, config_file=args.config)
        window.show()

        exit_code = app.exec_()

        # Ensure proper cleanup
        window.deleteLater()

        sys.exit(exit_code)

    except Exception as e:
        print(f"Application error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
