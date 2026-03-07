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

import html as _html
import re

from PySide6.QtGui import (QColor, QFont, QTextCharFormat,  # noqa: F401
                           QTextCursor)
from PySide6.QtWidgets import (QHBoxLayout, QPushButton, QTextEdit,
                               QVBoxLayout, QWidget)

# Compiled once at module level
_ROS_PREFIX = re.compile(
    r'^\[(?:INFO|WARN|ERROR|DEBUG|FATAL)\]\s*\[\d+\.\d+\]\s*\[[^\]]+\]:\s*'
)
_HIGHLIGHT_RULES = [
    (re.compile(r'\b(error|failed|exception|fatal|critical|abort|crash)\b', re.IGNORECASE),
     '#ff6b6b'),
    (re.compile(r'\b(warning|warn|deprecated|caution)\b', re.IGNORECASE),
     '#ffd93d'),
    (re.compile(r'\b(success|passed|complete|ok|done|finished)\b', re.IGNORECASE),
     '#50fa7b'),
    (re.compile(r'\b(info|debug|trace)\b', re.IGNORECASE),
     '#8be9fd'),
]


def _line_to_html(raw_line: str) -> str:
    """Convert one log line to an HTML fragment with syntax highlighting."""
    line = _ROS_PREFIX.sub('', raw_line)
    escaped = _html.escape(line)
    for pattern, color in _HIGHLIGHT_RULES:
        if pattern.search(escaped):
            escaped = pattern.sub(
                rf'<span style="color:{color};font-weight:bold">\1</span>',
                escaped,
            )
            break  # only apply the first matching category
    return escaped


class TerminalOutputWidget(QWidget):
    """Widget for displaying terminal output with syntax highlighting.

    Lines are stored in memory and rendered CHUNK_SIZE lines at a time.
    Additional lines are loaded transparently as the user scrolls down.
    """

    CHUNK_SIZE = 500  # lines loaded per scroll trigger

    def __init__(self, parent=None):
        super().__init__(parent)
        self._all_lines: list[str] = []
        self._rendered_count = 0
        self._loading = False
        self.setup_ui()

    # ------------------------------------------------------------------
    # UI setup
    # ------------------------------------------------------------------

    def setup_ui(self):
        """Setup the terminal output widget UI"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        # Text area
        self._text_edit = QTextEdit()
        self._text_edit.setReadOnly(True)
        self._text_edit.setFont(QFont("Courier", 9))
        self._text_edit.setStyleSheet("""
            QTextEdit {
                background-color: #1e1e1e;
                color: #f8f8f2;
                border: 1px solid #444;
                border-radius: 4px;
                padding: 8px;
            }
        """)
        layout.addWidget(self._text_edit)

        self._text_edit.verticalScrollBar().valueChanged.connect(self._on_scroll)

    # ------------------------------------------------------------------
    # Infinite-scroll helpers
    # ------------------------------------------------------------------

    def _on_scroll(self, value):
        if self._loading:
            return
        progressbar = self._text_edit.verticalScrollBar()
        if value >= progressbar.maximum() - 50:
            self._render_more()

    def _render_more(self):
        """Append the next chunk of lines to the text edit (single insert)."""
        if self._loading or self._rendered_count >= len(self._all_lines):
            return
        self._loading = True
        try:
            end = min(self._rendered_count + self.CHUNK_SIZE, len(self._all_lines))
            html_parts = [_line_to_html(line) for line in self._all_lines[self._rendered_count:end]]
            chunk_html = '<br>'.join(html_parts) + '<br>'
            cursor = self._text_edit.textCursor()
            cursor.movePosition(QTextCursor.End)
            self._text_edit.setTextCursor(cursor)
            self._text_edit.insertHtml(chunk_html)
            self._rendered_count = end
        finally:
            self._loading = False

    # ------------------------------------------------------------------
    # Syntax highlighting (kept for public API compatibility)
    # ------------------------------------------------------------------

    def clean_ros_logging(self, text):
        """Remove ROS logging prefixes from text."""
        lines = text.split('\n')
        return '\n'.join(_ROS_PREFIX.sub('', line) for line in lines)

    def _append_line_to_edit(self, text):
        """Append one line to the text edit with syntax highlighting."""
        cursor = self._text_edit.textCursor()
        cursor.movePosition(QTextCursor.End)
        self._text_edit.setTextCursor(cursor)
        self._text_edit.insertHtml(_line_to_html(text) + '<br>')

    def _apply_syntax_highlighting(self, text):
        """Apply syntax highlighting to a single line of text."""
        self._append_line_to_edit(text)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def append_output(self, text):
        """Append text (may be multi-line) with syntax highlighting."""
        at_bottom = self._rendered_count >= len(self._all_lines)
        self._all_lines.extend(text.split('\n'))
        if at_bottom:
            self._render_more()

    def set_content(self, content):
        """Set the entire content; renders the first chunk immediately."""
        self._all_lines = content.split('\n')
        self._rendered_count = 0
        self._text_edit.clear()
        self._render_more()

    def append_plain_text(self, text):
        """Append plain text (with syntax highlighting)."""
        self.append_output(text)

    def clear(self):
        """Clear all content."""
        self._all_lines = []
        self._rendered_count = 0
        self._text_edit.clear()

    def highlight_search_term(self, search_term):
        """Highlight all occurrences of a search term on the current page."""
        if not search_term:
            return

        document = self._text_edit.document()
        cursor = QTextCursor(document)

        highlight_format = QTextCharFormat()
        highlight_format.setBackground(QColor("yellow"))
        highlight_format.setForeground(QColor("black"))

        while True:
            cursor = document.find(search_term, cursor)
            if cursor.isNull():
                break
            cursor.mergeCharFormat(highlight_format)

    def clear_highlighting(self):
        """Clear all text highlighting on the current page."""
        cursor = QTextCursor(self._text_edit.document())
        cursor.select(QTextCursor.Document)

        text_format = QTextCharFormat()
        text_format.setBackground(QColor("transparent"))
        cursor.mergeCharFormat(text_format)

    # ------------------------------------------------------------------
    # Legacy / compatibility: expose the old apply_syntax_highlighting name
    # ------------------------------------------------------------------

    def apply_syntax_highlighting(self, text):
        """Alias kept for backwards compatibility."""
        self._apply_syntax_highlighting(text)


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
