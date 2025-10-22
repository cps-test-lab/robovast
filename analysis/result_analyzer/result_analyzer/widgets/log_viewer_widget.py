#!/usr/bin/env python3
"""
Log Viewer Widget - A Qt widget for displaying and managing log files with syntax highlighting
"""

from pathlib import Path

try:
    from PySide2.QtCore import QTimer, Signal, Slot
    from PySide2.QtWidgets import (QCheckBox, QComboBox, QHBoxLayout, QLabel,
                                   QLineEdit, QPushButton, QVBoxLayout,
                                   QWidget)
    QT_SUPPORT = True
except ImportError:
    print("Error: PySide2 is required for log_viewer_widget.py")
    QT_SUPPORT = False

from .common import (check_preferred_log_file, filter_nonrelevant_lines,
                     get_log_files)
from .terminal_output_widget import TerminalOutputWidget


class LogViewerWidget(QWidget):
    """Widget for viewing log files with syntax highlighting and search capabilities"""

    # Signal emitted when a log file is loaded
    log_loaded = Signal(str)  # file path

    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_logs_dir = None
        self.current_log_path = None
        self.setup_ui()

    def setup_ui(self):
        """Setup the log viewer UI"""
        layout = QVBoxLayout(self)

        # Top controls
        controls_layout = QVBoxLayout()

        # Log file selector row
        selector_layout = QHBoxLayout()
        selector_layout.addWidget(QLabel("Select Log File:"))

        self.log_file_combo = QComboBox()
        self.log_file_combo.currentTextChanged.connect(self.on_log_file_changed)
        selector_layout.addWidget(self.log_file_combo)

        # Refresh button
        self.refresh_btn = QPushButton("🔄 Refresh")
        self.refresh_btn.clicked.connect(self.refresh_log_files)
        self.refresh_btn.setMaximumWidth(80)
        selector_layout.addWidget(self.refresh_btn)

        controls_layout.addLayout(selector_layout)

        # Search and filter row
        search_layout = QHBoxLayout()

        search_layout.addWidget(QLabel("Search:"))
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search in log content...")
        self.search_input.textChanged.connect(self.on_search_changed)
        search_layout.addWidget(self.search_input)

        # Filter checkboxes
        self.show_errors_cb = QCheckBox("Errors")
        self.show_errors_cb.setChecked(True)
        self.show_errors_cb.toggled.connect(self.apply_filters)
        search_layout.addWidget(self.show_errors_cb)

        self.show_warnings_cb = QCheckBox("Warnings")
        self.show_warnings_cb.setChecked(True)
        self.show_warnings_cb.toggled.connect(self.apply_filters)
        search_layout.addWidget(self.show_warnings_cb)

        self.show_info_cb = QCheckBox("Info")
        self.show_info_cb.setChecked(True)
        self.show_info_cb.toggled.connect(self.apply_filters)
        search_layout.addWidget(self.show_info_cb)

        # Filter non-relevant lines checkbox
        self.filter_nonrelevant_cb = QCheckBox("Filter non-relevant")
        self.filter_nonrelevant_cb.setToolTip("Filter out rviz2 and sys_stats_publisher lines")
        self.filter_nonrelevant_cb.setChecked(True)
        self.filter_nonrelevant_cb.toggled.connect(self.on_filter_toggle)
        search_layout.addWidget(self.filter_nonrelevant_cb)

        controls_layout.addLayout(search_layout)
        layout.addLayout(controls_layout)

        # Terminal output widget
        self.log_display = TerminalOutputWidget()
        layout.addWidget(self.log_display)

    def set_logs_directory(self, logs_dir):
        """Set the directory containing log files"""
        self.current_logs_dir = Path(logs_dir) if logs_dir else None
        QTimer.singleShot(0, self.refresh_log_files)

    @Slot()
    def refresh_log_files(self):
        """Refresh the list of available log files"""
        self.log_file_combo.clear()
        self.log_display.clear()

        if not self.current_logs_dir or not self.current_logs_dir.exists():
            return

        try:
            log_files = get_log_files(self.current_logs_dir)

            # Find preferred log file and move it to the front
            preferred_file = None
            other_files = []

            for log_file in log_files:
                if check_preferred_log_file(log_file):
                    preferred_file = log_file
                else:
                    other_files.append(log_file)

            # Reorder: preferred file first, then others
            ordered_files = []
            if preferred_file:
                ordered_files.append(preferred_file)
            ordered_files.extend(other_files)

            # Convert to names for combo box
            log_file_names = [f.name for f in ordered_files]

            if log_file_names:
                self.log_file_combo.addItems(log_file_names)
                # Load the first log file by default (which will be the preferred one if found)
                self.load_log_file(ordered_files[0])
            else:
                self.log_display.append_output("No log files found in directory")

        except Exception as e:
            self.log_display.clear()
            self.log_display.append_output(f"Error loading log files: {str(e)}")

    def on_log_file_changed(self, filename):
        """Handle log file selection change"""
        if filename and self.current_logs_dir:
            log_file_path = self.current_logs_dir / filename
            self.load_log_file(log_file_path)

    def on_filter_toggle(self):
        """Handle filter toggle - reload current log file with/without filtering"""
        if self.current_log_path:
            self.load_log_file(self.current_log_path)

    def load_log_file(self, log_file_path):
        """Load and display a log file"""
        try:
            self.current_log_path = Path(log_file_path)

            with open(log_file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()

            # Apply non-relevant line filtering if enabled
            if self.filter_nonrelevant_cb.isChecked():
                content = filter_nonrelevant_lines(content)

            # Limit content for performance
            max_lines = 10000
            lines = content.split('\n')
            if len(lines) > max_lines:
                content = '\n'.join(lines[:max_lines])
                content += f"\n\n... [File truncated - showing first {max_lines} lines of {len(lines)} total] ..."

            self.log_display.set_content(content)
            self.log_loaded.emit(str(log_file_path))

            # Apply current search and filters
            self.apply_search_and_filters()

        except Exception as e:
            self.log_display.clear()
            self.log_display.append_output(f"Error loading log file: {str(e)}")

    def load_file_content(self, file_path):
        """Load content from any text file (not just logs)"""
        try:
            # Clear the combo box and add this single file
            self.log_file_combo.clear()
            self.log_file_combo.addItem(Path(file_path).name)

            # Load the file content
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()

            # Apply non-relevant line filtering if enabled
            if self.filter_nonrelevant_cb.isChecked():
                content = filter_nonrelevant_lines(content)

            # Limit content for performance
            max_lines = 2000
            lines = content.split('\n')
            if len(lines) > max_lines:
                content = '\n'.join(lines[:max_lines])
                content += f"\n\n... [File truncated - showing first {max_lines} lines of {len(lines)} total] ..."

            self.log_display.set_content(content)
            self.log_loaded.emit(str(file_path))

            # Apply current search and filters
            self.apply_search_and_filters()

        except Exception as e:
            self.log_display.clear()
            self.log_display.append_output(f"Error loading file: {str(e)}")

    def on_search_changed(self, search_text):
        """Handle search text changes"""
        self.apply_search_and_filters()

    def apply_filters(self):
        """Apply content filters"""
        self.apply_search_and_filters()

    def apply_search_and_filters(self):
        """Apply search and content filters to the displayed log"""
        search_text = self.search_input.text().strip()

        # Clear existing highlighting
        self.log_display.clear_highlighting()

        # Apply search highlighting if there's search text
        if search_text:
            self.log_display.highlight_search_term(search_text)

        # Note: Content filtering by log level would require re-processing the content
        # For now, we just highlight search terms. Full filtering could be added later.

    def clear(self):
        """Clear the log viewer"""
        self.current_logs_dir = None
        self.current_log_path = None
        self.log_file_combo.clear()
        self.log_display.clear()
        self.search_input.clear()

    def get_current_log_path(self):
        """Get the path of the currently loaded log file"""
        return self.current_log_path


if __name__ == "__main__":
    """Test the log viewer widget"""
    import sys

    from PySide2.QtWidgets import (QApplication, QMainWindow, QVBoxLayout,
                                   QWidget)

    app = QApplication(sys.argv)

    # Create main window
    window = QMainWindow()
    window.setWindowTitle("Log Viewer Widget Test")
    window.setGeometry(100, 100, 1000, 700)

    # Create central widget
    central_widget = QWidget()
    window.setCentralWidget(central_widget)
    layout = QVBoxLayout(central_widget)

    # Add log viewer widget
    log_viewer = LogViewerWidget()
    layout.addWidget(log_viewer)

    # Connect signals
    def on_log_loaded(file_path):
        print(f"Log loaded: {file_path}")

    log_viewer.log_loaded.connect(on_log_loaded)

    window.show()

    print("Log viewer widget test window created.")
    print("Use log_viewer.set_logs_directory(path) to load a directory with log files")

    sys.exit(app.exec_())
