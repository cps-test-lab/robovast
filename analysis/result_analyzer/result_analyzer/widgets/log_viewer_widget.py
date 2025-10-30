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

from pathlib import Path

from PySide6.QtCore import QTimer, Signal, Slot
from PySide6.QtWidgets import (QComboBox, QFormLayout, QLineEdit,
                                QVBoxLayout, QWidget)

from .common import check_preferred_log_file, get_log_files
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

        # Top controls using form layout
        controls_layout = QFormLayout()

        # Log file selector
        self.log_file_combo = QComboBox()
        self.log_file_combo.currentTextChanged.connect(self.on_log_file_changed)
        controls_layout.addRow("Select Log File:", self.log_file_combo)

        # Search input
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search in log content...")
        self.search_input.textChanged.connect(self.on_search_changed)
        controls_layout.addRow("Search:", self.search_input)

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

    def load_log_file(self, log_file_path):
        """Load and display a log file"""
        try:
            self.current_log_path = Path(log_file_path)

            with open(log_file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()

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

    def on_search_changed(self, search_text):
        """Handle search text changes"""
        self.apply_search_and_filters()

    def apply_search_and_filters(self):
        """Apply search and content filters to the displayed log"""
        search_text = self.search_input.text().strip()

        # Clear existing highlighting
        self.log_display.clear_highlighting()

        # Apply search highlighting if there's search text
        if search_text:
            self.log_display.highlight_search_term(search_text)

    def clear(self):
        """Clear the log viewer"""
        self.current_logs_dir = None
        self.current_log_path = None
        self.log_file_combo.clear()
        self.log_display.clear()
        self.search_input.clear()
