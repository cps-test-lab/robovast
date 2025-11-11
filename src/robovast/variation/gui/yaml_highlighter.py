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

"""YAML syntax highlighter for PySide6 text editor."""

import re

from PySide6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat


class YamlHighlighter(QSyntaxHighlighter):
    """Syntax highlighter for YAML files."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.highlighting_rules = []

        # Keywords/special values
        keyword_format = QTextCharFormat()
        keyword_format.setForeground(QColor("#569CD6"))  # Blue
        keyword_format.setFontWeight(QFont.Bold)
        keywords = ["true", "false", "null", "True", "False", "None"]
        for word in keywords:
            pattern = rf"\b{word}\b"
            self.highlighting_rules.append((re.compile(pattern), keyword_format))

        # Keys (before colon)
        key_format = QTextCharFormat()
        key_format.setForeground(QColor("#9CDCFE"))  # Light blue
        self.highlighting_rules.append((re.compile(r"^[\s]*[\w-]+(?=:)"), key_format))
        self.highlighting_rules.append((re.compile(r"[\s][\w-]+(?=:)"), key_format))

        # Strings (single and double quoted)
        string_format = QTextCharFormat()
        string_format.setForeground(QColor("#CE9178"))  # Orange
        self.highlighting_rules.append((re.compile(r'"[^"\\]*(\\.[^"\\]*)*"'), string_format))
        self.highlighting_rules.append((re.compile(r"'[^'\\]*(\\.[^'\\]*)*'"), string_format))

        # Numbers
        number_format = QTextCharFormat()
        number_format.setForeground(QColor("#B5CEA8"))  # Light green
        self.highlighting_rules.append((re.compile(r"\b[+-]?\d+\.?\d*\b"), number_format))

        # Comments
        comment_format = QTextCharFormat()
        comment_format.setForeground(QColor("#6A9955"))  # Green
        comment_format.setFontItalic(True)
        self.highlighting_rules.append((re.compile(r"#[^\n]*"), comment_format))

        # List markers
        list_format = QTextCharFormat()
        list_format.setForeground(QColor("#C586C0"))  # Purple
        list_format.setFontWeight(QFont.Bold)
        self.highlighting_rules.append((re.compile(r"^[\s]*-(?=\s)"), list_format))

        # Anchors and aliases
        anchor_format = QTextCharFormat()
        anchor_format.setForeground(QColor("#DCDCAA"))  # Yellow
        self.highlighting_rules.append((re.compile(r"&[\w-]+"), anchor_format))
        self.highlighting_rules.append((re.compile(r"\*[\w-]+"), anchor_format))

    def highlightBlock(self, text):
        """Apply syntax highlighting to the given block of text."""
        for pattern, format_style in self.highlighting_rules:
            for match in pattern.finditer(text):
                start = match.start()
                length = match.end() - start
                self.setFormat(start, length, format_style)
