#!/usr/bin/env python3
"""
Test Results Analyzer GUI - Qt Version
A Qt-based GUI application for analyzing test results stored in downloaded_files directory.
"""

from .widgets.worker_thread import LatestOnlyWorker
from .widgets.settings_dialog import SettingsDialog
from .widgets.rosbag_parser import RosbagParser
from .widgets.log_viewer_widget import LogViewerWidget
from .widgets.local_execution_widget import LocalExecutionWidget
from .widgets.jupyter_widget import DataAnalysisWidget, JupyterNotebookRunner
from .widgets.common import RUN_TYPE
from .widgets.command_execution_worker import CommandLineExecutionWorker
from .widgets.chat_widget import ChatWidget
from robovast_common import load_scenario_config
import argparse
import math
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from PySide2.QtCore import QSettings, Qt, QThread, QTimer
from PySide2.QtCore import Slot as pyqtSlot
from PySide2.QtGui import QBrush, QColor
from PySide2.QtWidgets import (QApplication, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QMainWindow, QProgressBar,
                               QPushButton, QSplitter, QStatusBar, QTabWidget,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                               QWidget)

QT_SUPPORT = True


try:
    OPENCV_SUPPORT = True
except ImportError:
    OPENCV_SUPPORT = False


class TestResultsAnalyzer(QMainWindow):
    def __init__(self, base_dir=None, config_file=None):
        super().__init__()

        # Initialize QSettings for local/system-specific settings (window state, etc.)
        self.settings = QSettings("TestResultsAnalyzer", "Settings")

        # Initialize configuration for shared settings
        self.parameters = load_scenario_config(config_file, "analysis")

        # Initialize variables to None first
        self.local_execution_widget = None
        # self.data_analysis_widget = None
        self.tree = None
        # self.overview_text = None
        self.log_viewer = None
        self.chat_widget = None
        self.details_tabs = None
        self.analysis_tabs = {}
        # Initialize rosbag parser and file cache
        self.rosbag_parser = RosbagParser()

        # Worker thread setup
        self.worker_thread = QThread()
        self.overview_topics_required = ["/gazebo/real_time_factor", "/tf", "/tf_static", "/system/cpu_usage", "/system/memory_usage"]

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
        workloads.append(
            CommandLineExecutionWorker("execution/rosbag_2_csv_conversion_command"),
        )
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

        # Base directory for downloaded files
        if base_dir:
            self.base_dir = Path(base_dir)
        else:
            # Try to get from config first, then fall back to default
            results_dir = self.config.get("directories", "results_dir")
            self.base_dir = Path(__file__).parent.parent.parent / results_dir

        self.setWindowTitle(f"Test Results Analyzer - {self.base_dir}")
        self.resize(1400, 900)

        self.setup_ui()
        self.load_window_state()
        self.populate_tree()

    def __del__(self):
        """Destructor to ensure proper cleanup"""
        try:
            if hasattr(self, 'local_execution_widget') and self.local_execution_widget:
                self.local_execution_widget.stop_execution()
            # if hasattr(self, 'data_analysis_widget') and self.data_analysis_widget:
            #     self.data_analysis_widget.cleanup()
        except:
            pass

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
        # self.worker_progress_bar.setFormat("Ready")  # Default text
        # self.worker_progress_bar.setStyleSheet("""
        #     QProgressBar {
        #         border: 1px solid #bbdefb;
        #         border-radius: 3px;
        #         background-color: #f5f5f5;
        #         text-align: center;
        #         font-size: 11px;
        #         color: #333;
        #     }
        #     QProgressBar::chunk {
        #         background-color: #1976d2;
        #         border-radius: 2px;
        #     }
        # """)

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

        # Settings button with theme-aware icon
        settings_button = QPushButton("⚙")
        settings_button.setToolTip("Open Settings")
        settings_button.setFixedSize(24, 24)
        settings_button.clicked.connect(self.open_settings_dialog)
        header_layout.addWidget(settings_button)

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

    def open_settings_dialog(self):
        """Open the settings dialog"""
        dialog = SettingsDialog(self, None)
        if dialog.exec_() == dialog.Accepted:
            # Settings were applied, refresh UI if needed
            self.apply_settings_changes()

    @pyqtSlot()
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

    def apply_settings_changes(self):
        """Apply settings changes to the UI"""
        # This will be expanded as we add more settings
        auto_expand = True  # self.config.get_bool("general", "auto_expand_tree")
        if auto_expand and self.tree:
            self.tree.expandAll()
        else:
            self.tree.collapseAll()

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
        bg_color = palette.color(palette.Window)
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

        # # Overview tab
        # self.overview_text = TestOverviewWidget()
        # self.details_tabs.addTab(self.overview_text, "Overview")

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

        # Chat tab
        try:
            self.chat_widget = ChatWidget()
        except Exception as e:
            print(f"Failed to create chat widget: {e}")
            self.chat_widget = QLabel("Chat widget initialization failed")
            self.chat_widget.setAlignment(Qt.AlignCenter)
            self.chat_widget.setStyleSheet("background-color: #f8f9fa; color: red; border: 1px solid gray;")

        self.details_tabs.addTab(self.chat_widget, "AI Chat")

        # Local execution tab
        try:
            self.local_execution_widget = LocalExecutionWidget()
        except Exception as e:
            print(f"Failed to create local execution widget: {e}")
            self.local_execution_widget = QLabel("Local execution widget initialization failed")
            self.local_execution_widget.setAlignment(Qt.AlignCenter)
            self.local_execution_widget.setStyleSheet("background-color: #f8f9fa; color: red; border: 1px solid gray;")

        self.details_tabs.addTab(self.local_execution_widget, "Local Execution")

        # # data analysis tab
        # try:
        #     self.data_analysis_widget = DataAnalysisWidget(mode=DataAnalysisMode.ANALYSIS)
        # except Exception as e:
        #     print(f"Failed to create Data Analysis widget: {e}")
        #     self.data_analysis_widget = QLabel("Data Analysis widget initialization failed")
        #     self.data_analysis_widget.setAlignment(Qt.AlignCenter)
        #     self.data_analysis_widget.setStyleSheet("background-color: #f8f9fa; color: red; border: 1px solid gray;")

        # self.details_tabs.addTab(self.data_analysis_widget, "Details")

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
        auto_expand = True  # self.config.get_bool("general", "auto_expand_tree")
        if auto_expand:
            self.tree.expandAll()
        else:
            # Only expand the root level (run- directories)
            for i in range(self.tree.topLevelItemCount()):
                item = self.tree.topLevelItem(i)
                if item and item.text(0).startswith("run-"):
                    self.tree.expandItem(item)

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
                return RUN_TYPE.SINGLE_TEST

            # 2. Check if run.yamls exist in subfolders (folder run)
            run_files = list(data_path.glob("*/run.yaml"))
            if run_files:
                return RUN_TYPE.SINGLE_VARIANT

            # 3. Check if CSV files exist in subfolders of subfolders (whole run)
            run_files = list(data_path.glob("*/*/run.yaml"))
            if run_files:
                return RUN_TYPE.RUN

        except Exception as e:
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

        # Update local execution widget
        try:
            self.local_execution_widget.set_test_directory(directory_path)
        except Exception as e:
            print(f"Error updating local execution widget: {e}")

        # Update log files
        logs_dir = directory_path / "logs"
        if logs_dir.exists():
            self.log_viewer.set_logs_directory(logs_dir)
            # Update chat widget with logs directory
            try:
                self.chat_widget.set_logs_directory(logs_dir)
            except Exception as e:
                print(f"Error updating chat widget logs directory: {e}")
        else:
            self.log_viewer.clear()
            try:
                self.chat_widget.set_logs_directory(None)
            except Exception as e:
                print(f"Error clearing chat widget logs directory: {e}")

        # self.overview_text.update_overview(directory_path)

    @pyqtSlot(int, str, str)
    def _on_worker_progress_updated(self, percentage, workload_name, status):
        """Handle rosbag analysis progress updates"""
        # Show progress bar when there's active progress
        self.worker_progress_bar.show()

        # Update status bar message
        self.status_label.setText(f"{workload_name}: {status}")

        # Update progress bar with text and percentage
        self.worker_progress_bar.setRange(0, 100)
        self.worker_progress_bar.setValue(percentage)
        # self.worker_progress_bar.setFormat(f"{workload_name}: {percentage}%")

        print(f"Rosbag analysis progress: {percentage}% ({workload_name}): {status}")

    @pyqtSlot(str, str)
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

    @pyqtSlot(object, str, str)
    def _on_worker_finished(self, result, data, task_id, workload_name):
        """Handle analysis completion from different workloads"""

        print(f"Received result from {workload_name}. {result}")
        if not result:
            print(f"  Result: {data}")

        try:
            # Handle different types of workload results
            if workload_name == "RosbagAnalyzerWorker":
                # Update overview widget with rosbag data
                if hasattr(self.overview_text, 'update_rosbag_data'):
                    if result:
                        self.overview_text.update_rosbag_data(data)
                        print(f"Rosbag analysis completed successfully")
                    else:
                        print(f"Rosbag analysis failed.")
                        self.overview_text.clear()

            elif workload_name == "RosbagConversion":
                # Handle external command execution results
                if result:
                    if data and isinstance(data, dict):
                        success = data.get('success', False)
                        command = data.get('command', 'Unknown command')
                        return_code = data.get('return_code', -1)

                        if success:
                            print(f"External command completed successfully: {command}")
                        else:
                            print(f"External command failed (code {return_code}): {command}")
                            if 'error' in data:
                                print(f"Error: {data['error']}")
                            if data.get('stderr'):
                                print(f"STDERR: {data['stderr']}")
                    else:
                        print(f"External command execution completed.")
                else:
                    print(f"External command execution failed.")

            elif workload_name in self.analysis_tabs:
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
            # elif workload_name == "AnalysisCreation":
            #     # Handle Jupyter notebook execution results
            #     if hasattr(self.data_analysis_widget, 'display_html'):
            #         if result:
            #             print(f"Jupyter notebook execution completed successfully.")
            #             self.worker_progress_bar.show()  # Show briefly to indicate completion
            #             self.status_label.setText(f"Jupyter analysis completed successfully")
            #             # self.worker_progress_bar.setFormat("Analysis Complete")
            #         else:
            #             print(f"Jupyter notebook execution failed.")
            #             self.worker_progress_bar.show()  # Show briefly to indicate failure
            #             self.status_label.setText(f"Jupyter analysis failed")
            #             # self.worker_progress_bar.setFormat("Analysis Failed")
            #         self.data_analysis_widget.display_html(data)

            #     # as this is the last workload, we can reset to ready state after a short delay
            #     QTimer.singleShot(2000, self._reset_status_to_ready)

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
        """Handle application closing"""
        try:
            # Save window state
            self.save_window_state()

            self.local_execution_widget.stop_execution()

            # Cleanup Jupyter console widget
            # if hasattr(self, 'data_analysis_widget') and self.data_analysis_widget:
            #     self.data_analysis_widget.cleanup()

            # Clear all widgets and their contents to prevent segfaults
            self.tree.clear()
            # self.overview_text.clear()
            self.log_viewer.clear()
            if hasattr(self, 'chat_widget') and self.chat_widget:
                self.chat_widget.clear()

            # Explicitly delete the local execution widget
            if hasattr(self, 'local_execution_widget') and self.local_execution_widget:
                self.local_execution_widget.setParent(None)
                self.local_execution_widget.deleteLater()
                self.local_execution_widget = None

            # Explicitly delete the chat widget
            if hasattr(self, 'chat_widget') and self.chat_widget:
                self.chat_widget.setParent(None)
                self.chat_widget.deleteLater()
                self.chat_widget = None

            # Explicitly delete the jupyter console widget
            # if hasattr(self, 'data_analysis_widget') and self.data_analysis_widget:
            #     self.data_analysis_widget.cleanup()
            #     self.data_analysis_widget.setParent(None)
            #     self.data_analysis_widget.deleteLater()
            #     self.data_analysis_widget = None

            # Explicitly delete the jupyter console widget
            for analysis_tab in self.analysis_tabs.values():
                if analysis_tab:
                    analysis_tab.cleanup()
                    analysis_tab.setParent(None)
                    analysis_tab.deleteLater()
            self.analysis_tabs.clear()

        except Exception as e:
            print(f"Error during cleanup: {e}")
        finally:
            event.accept()

    def closeEvent(self, event):
        """Clean shutdown"""
        print("Shutting down...")

        # Stop worker
        self.worker.stop()

        # Wait for thread to finish
        self.worker_thread.quit()
        if not self.worker_thread.wait(3000):  # Wait up to 3 seconds
            print("Warning: Worker thread didn't stop gracefully")
            self.worker_thread.terminate()
            self.worker_thread.wait()

        event.accept()


def main():
    """Main function"""
    parser = argparse.ArgumentParser(description="Test Results Analyzer GUI - Qt Version")
    parser.add_argument("--results-dir", type=str,
                        help="Directory containing test results (default: downloaded_files)")
    parser.add_argument("--config", type=str,
                        help="Configuration file path (default: result_analyzer.cfg in project root)")

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
