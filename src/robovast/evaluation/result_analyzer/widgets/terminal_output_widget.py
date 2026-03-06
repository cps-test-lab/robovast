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

from PySide6.QtGui import QColor, QFont, QPalette, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QTextEdit,
                               QVBoxLayout, QWidget)


class TerminalOutputWidget(QWidget):
    """Widget for displaying terminal output with syntax highlighting and paging.

    Lines are stored in memory and rendered PAGE_SIZE lines at a time so that
    opening very large log files does not block the UI.
    """

    PAGE_SIZE = 500  # lines per page

    def __init__(self, parent=None):
        super().__init__(parent)
        self._all_lines: list[str] = []
        self._current_page = 0
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

        # Paging controls
        paging_layout = QHBoxLayout()
        paging_layout.setContentsMargins(0, 0, 0, 0)

        self._first_btn = QPushButton("|<")
        self._prev_btn = QPushButton("< Prev")
        self._next_btn = QPushButton("Next >")
        self._last_btn = QPushButton(">|")
        self._page_label = QLabel()

        for btn in (self._first_btn, self._prev_btn, self._next_btn, self._last_btn):
            btn.setFixedWidth(70)

        self._first_btn.clicked.connect(self._goto_first_page)
        self._prev_btn.clicked.connect(self._goto_prev_page)
        self._next_btn.clicked.connect(self._goto_next_page)
        self._last_btn.clicked.connect(self._goto_last_page)

        paging_layout.addStretch()
        paging_layout.addWidget(self._first_btn)
        paging_layout.addWidget(self._prev_btn)
        paging_layout.addWidget(self._page_label)
        paging_layout.addWidget(self._next_btn)
        paging_layout.addWidget(self._last_btn)
        paging_layout.addStretch()
        layout.addLayout(paging_layout)

        self._update_paging_controls()

    # ------------------------------------------------------------------
    # Paging helpers
    # ------------------------------------------------------------------

    def _total_pages(self) -> int:
        if not self._all_lines:
            return 0
        return (len(self._all_lines) + self.PAGE_SIZE - 1) // self.PAGE_SIZE

    def _update_paging_controls(self):
        total = self._total_pages()
        show = total > 1
        for w in (self._first_btn, self._prev_btn, self._page_label,
                  self._next_btn, self._last_btn):
            w.setVisible(show)

        if show:
            self._page_label.setText(
                f"Page {self._current_page + 1} / {total}  "
                f"(lines {self._current_page * self.PAGE_SIZE + 1}–"
                f"{min((self._current_page + 1) * self.PAGE_SIZE, len(self._all_lines))}"
                f" of {len(self._all_lines)})"
            )
            self._first_btn.setEnabled(self._current_page > 0)
            self._prev_btn.setEnabled(self._current_page > 0)
            self._next_btn.setEnabled(self._current_page < total - 1)
            self._last_btn.setEnabled(self._current_page < total - 1)

    def _goto_first_page(self):
        self._current_page = 0
        self._render_page()

    def _goto_prev_page(self):
        if self._current_page > 0:
            self._current_page -= 1
            self._render_page()

    def _goto_next_page(self):
        if self._current_page < self._total_pages() - 1:
            self._current_page += 1
            self._render_page()

    def _goto_last_page(self):
        self._current_page = max(0, self._total_pages() - 1)
        self._render_page()

    def _render_page(self):
        """Render the current page of lines into the text edit."""
        self._text_edit.clear()
        start = self._current_page * self.PAGE_SIZE
        end = min(start + self.PAGE_SIZE, len(self._all_lines))

        for line in self._all_lines[start:end]:
            if line.strip():
                self._append_line_to_edit(line.rstrip())
            else:
                cursor = self._text_edit.textCursor()
                cursor.movePosition(QTextCursor.End)
                self._text_edit.setTextCursor(cursor)
                self._text_edit.insertPlainText('\n')

        # Scroll to top of the newly rendered page
        self._text_edit.verticalScrollBar().setValue(0)
        self._update_paging_controls()

    # ------------------------------------------------------------------
    # Syntax highlighting (operates on self._text_edit)
    # ------------------------------------------------------------------

    def clean_ros_logging(self, text):
        """Remove ROS logging prefixes from text"""
        ros_prefix_pattern = r'^\[(?:INFO|WARN|ERROR|DEBUG|FATAL)\]\s*\[\d+\.\d+\]\s*\[[^\]]+\]:\s*'
        lines = text.split('\n')
        return '\n'.join(re.sub(ros_prefix_pattern, '', line) for line in lines)

    def _append_line_to_edit(self, text):
        """Append one line to the text edit with syntax highlighting."""
        cursor = self._text_edit.textCursor()
        cursor.movePosition(QTextCursor.End)
        self._text_edit.setTextCursor(cursor)

        cleaned = self.clean_ros_logging(text)
        self._apply_syntax_highlighting(cleaned)

    def _apply_syntax_highlighting(self, text):
        """Apply syntax highlighting to a single line of text."""
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

        palette = self._text_edit.palette()

        if any(re.search(p, text, re.IGNORECASE) for p in error_patterns):
            self._text_edit.setTextColor(palette.color(QPalette.Text))
            html_text = text
            for p in error_patterns:
                html_text = re.sub(p, r'<span style="color: #ff6b6b; font-weight: bold;">\1</span>',
                                   html_text, flags=re.IGNORECASE)
            self._text_edit.insertHtml(html_text + '<br>')
        elif any(re.search(p, text, re.IGNORECASE) for p in warning_patterns):
            self._text_edit.setTextColor(palette.color(QPalette.Text))
            html_text = text
            for p in warning_patterns:
                html_text = re.sub(p, r'<span style="color: #ffd93d; font-weight: bold;">\1</span>',
                                   html_text, flags=re.IGNORECASE)
            self._text_edit.insertHtml(html_text + '<br>')
        elif any(re.search(p, text, re.IGNORECASE) for p in success_patterns):
            self._text_edit.setTextColor(palette.color(QPalette.Text))
            html_text = text
            for p in success_patterns:
                html_text = re.sub(p, r'<span style="color: #50fa7b; font-weight: bold;">\1</span>',
                                   html_text, flags=re.IGNORECASE)
            self._text_edit.insertHtml(html_text + '<br>')
        elif any(re.search(p, text, re.IGNORECASE) for p in info_patterns):
            self._text_edit.setTextColor(palette.color(QPalette.Text))
            html_text = text
            for p in info_patterns:
                html_text = re.sub(p, r'<span style="color: #8be9fd; font-weight: bold;">\1</span>',
                                   html_text, flags=re.IGNORECASE)
            self._text_edit.insertHtml(html_text + '<br>')
        else:
            self._text_edit.setTextColor(palette.color(QPalette.Text))
            self._text_edit.insertPlainText(text + '\n')

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def append_output(self, text):
        """Append text (may be multi-line) with syntax highlighting."""
        new_lines = text.split('\n')
        was_on_last = self._current_page == max(0, self._total_pages() - 1)
        self._all_lines.extend(new_lines)

        if was_on_last or self._total_pages() == 1:
            # Stay on the last page so the user sees new output
            self._current_page = max(0, self._total_pages() - 1)
            self._render_page()
        else:
            self._update_paging_controls()

    def set_content(self, content):
        """Set the entire content; only renders the first page immediately."""
        self._all_lines = content.split('\n')
        self._current_page = 0
        self._render_page()

    def append_plain_text(self, text):
        """Append plain text without syntax highlighting."""
        new_lines = text.split('\n')
        was_on_last = self._current_page == max(0, self._total_pages() - 1)
        self._all_lines.extend(new_lines)

        if was_on_last or self._total_pages() == 1:
            self._current_page = max(0, self._total_pages() - 1)
            self._render_page()
        else:
            self._update_paging_controls()

    def clear(self):
        """Clear all content."""
        self._all_lines = []
        self._current_page = 0
        self._text_edit.clear()
        self._update_paging_controls()

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
