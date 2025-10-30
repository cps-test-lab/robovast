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

import re
from PySide6.QtGui import QPalette, QColor, QFont, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (QHBoxLayout, QPushButton, QTextEdit,
                                QVBoxLayout, QWidget)

class TerminalOutputWidget(QTextEdit):
    """Widget for displaying terminal output with syntax highlighting"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setup_ui()

    def setup_ui(self):
        """Setup the terminal output widget UI"""
        self.setReadOnly(True)
        self.setFont(QFont("Courier", 9))
        self.setStyleSheet("""
            QTextEdit {
                background-color: #1e1e1e;
                color: #f8f8f2;
                border: 1px solid #444;
                border-radius: 4px;
                padding: 8px;
            }
        """)

    def append_output(self, text):
        """Append text to terminal output with syntax highlighting"""
        # Move cursor to end
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.setTextCursor(cursor)

        # Remove ROS logging prefix from each line
        cleaned_text = self.clean_ros_logging(text)

        # Apply syntax highlighting and insert text
        self.apply_syntax_highlighting(cleaned_text)

        # Auto-scroll to bottom
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.setTextCursor(cursor)

    def clean_ros_logging(self, text):
        """Remove ROS logging prefixes from text"""
        # Pattern to match ROS logging prefix like "[INFO] [1751195363.907544966] [scenario_execution_ros]: "
        ros_prefix_pattern = r'^\[(?:INFO|WARN|ERROR|DEBUG|FATAL)\]\s*\[\d+\.\d+\]\s*\[[^\]]+\]:\s*'

        # Process each line separately to remove the prefix
        lines = text.split('\n')
        cleaned_lines = []
        for line in lines:
            cleaned_line = re.sub(ros_prefix_pattern, '', line)
            cleaned_lines.append(cleaned_line)

        # Rejoin the lines
        return '\n'.join(cleaned_lines)

    def apply_syntax_highlighting(self, text):
        """Apply syntax highlighting to text based on content patterns"""
        # Define patterns for highlighting
        error_patterns = [
            r'\b(error|ERROR|Error|failed|FAILED|Failed|exception|EXCEPTION|Exception)\b',
            r'\b(fatal|FATAL|Fatal|critical|CRITICAL|Critical)\b',
            r'\b(abort|ABORT|Abort|crash|CRASH|Crash)\b'
        ]
        warning_patterns = [
            r'\b(warning|WARNING|Warning|warn|WARN|Warn)\b',
            r'\b(deprecated|DEPRECATED|Deprecated|caution|CAUTION|Caution)\b'
        ]
        success_patterns = [
            r'\b(success|SUCCESS|Success|passed|PASSED|Passed|complete|COMPLETE|Complete)\b',
            r'\b(ok|OK|Ok|done|DONE|Done|finished|FINISHED|Finished)\b'
        ]
        info_patterns = [
            r'\b(info|INFO|Info|debug|DEBUG|Debug|trace|TRACE|Trace)\b'
        ]

        # Check if the text contains different patterns
        has_error = any(re.search(pattern, text, re.IGNORECASE) for pattern in error_patterns)
        has_warning = any(re.search(pattern, text, re.IGNORECASE) for pattern in warning_patterns)
        has_success = any(re.search(pattern, text, re.IGNORECASE) for pattern in success_patterns)
        has_info = any(re.search(pattern, text, re.IGNORECASE) for pattern in info_patterns)

        if has_error:
            # Insert with red color for errors
            self.setTextColor(self.palette().color(QPalette.Text))
            html_text = text
            for pattern in error_patterns:
                html_text = re.sub(pattern, r'<span style="color: #ff6b6b; font-weight: bold;">\1</span>', html_text, flags=re.IGNORECASE)
            self.insertHtml(html_text + '<br>')
        elif has_warning:
            # Insert with yellow color for warnings
            self.setTextColor(self.palette().color(QPalette.Text))
            html_text = text
            for pattern in warning_patterns:
                html_text = re.sub(pattern, r'<span style="color: #ffd93d; font-weight: bold;">\1</span>', html_text, flags=re.IGNORECASE)
            self.insertHtml(html_text + '<br>')
        elif has_success:
            # Insert with green color for success
            self.setTextColor(self.palette().color(QPalette.Text))
            html_text = text
            for pattern in success_patterns:
                html_text = re.sub(pattern, r'<span style="color: #50fa7b; font-weight: bold;">\1</span>', html_text, flags=re.IGNORECASE)
            self.insertHtml(html_text + '<br>')
        elif has_info:
            # Insert with blue color for info
            self.setTextColor(self.palette().color(QPalette.Text))
            html_text = text
            for pattern in info_patterns:
                html_text = re.sub(pattern, r'<span style="color: #8be9fd; font-weight: bold;">\1</span>', html_text, flags=re.IGNORECASE)
            self.insertHtml(html_text + '<br>')
        else:
            # Insert normal text
            self.setTextColor(self.palette().color(QPalette.Text))
            self.insertPlainText(text + '\n')

    def set_content(self, content):
        """Set the entire content of the terminal output"""
        self.clear()

        # Split content into lines and apply highlighting to each line
        lines = content.split('\n')
        for line in lines:
            if line.strip():  # Only process non-empty lines
                self.append_output(line.rstrip())

    def append_plain_text(self, text):
        """Append plain text without highlighting"""
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.setTextCursor(cursor)

        self.setTextColor(self.palette().color(self.palette().text))
        self.insertPlainText(text + '\n')

        # Auto-scroll to bottom
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.setTextCursor(cursor)

    def highlight_search_term(self, search_term):
        """Highlight all occurrences of a search term"""
        if not search_term:
            return

        # Get the document
        document = self.document()

        # Create a cursor for searching
        cursor = QTextCursor(document)

        # Format for highlighting
        highlight_format = QTextCharFormat()
        highlight_format.setBackground(QColor("yellow"))
        highlight_format.setForeground(QColor("black"))

        # Find and highlight all occurrences
        while True:
            cursor = document.find(search_term, cursor)
            if cursor.isNull():
                break
            cursor.mergeCharFormat(highlight_format)

    def clear_highlighting(self):
        """Clear all text highlighting"""
        cursor = QTextCursor(self.document())
        cursor.select(QTextCursor.Document)

        text_format = QTextCharFormat()
        text_format.setBackground(QColor("transparent"))
        cursor.mergeCharFormat(text_format)


class TerminalOutputWidgetWithControls(QWidget):
    """Terminal output widget with additional controls"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setup_ui()

    def setup_ui(self):
        """Setup the terminal output widget with controls"""
        box_layout = QVBoxLayout(self)

        # Control buttons
        controls_layout = QHBoxLayout()

        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(self.clear_output)
        controls_layout.addWidget(self.clear_btn)

        controls_layout.addStretch()
        box_layout.addLayout(controls_layout)

        # Terminal output
        self.terminal_output = TerminalOutputWidget()
        box_layout.addWidget(self.terminal_output)

    def clear_output(self):
        """Clear the terminal output"""
        self.terminal_output.clear()

    def append_output(self, text):
        """Append text to terminal output"""
        self.terminal_output.append_output(text)

    def set_content(self, content):
        """Set the entire content"""
        self.terminal_output.set_content(content)

    def append_plain_text(self, text):
        """Append plain text without highlighting"""
        self.terminal_output.append_plain_text(text)
