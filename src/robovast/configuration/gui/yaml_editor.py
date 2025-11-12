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

from PySide6.QtCore import Qt
from PySide6.QtGui import (QColor, QFont, QKeyEvent, QPainter, QTextCursor,
                           QTextOption)
from PySide6.QtWidgets import QPlainTextEdit, QWidget

from robovast.configuration.gui.yaml_highlighter import YamlHighlighter


class LineNumberArea(QWidget):
    """Widget to display line numbers for the text editor."""

    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self):
        return self.editor.line_number_area_width()

    def paintEvent(self, event):
        self.editor.line_number_area_paint_event(event)


class YamlEditor(QPlainTextEdit):
    """Text editor with line numbers and YAML syntax highlighting."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.line_number_area = LineNumberArea(self)

        # Set up font
        font = QFont("Monospace")
        font.setStyleHint(QFont.TypeWriter)
        self.setFont(font)

        # Configure tab width (4 spaces)
        self.setTabStopDistance(4 * self.fontMetrics().horizontalAdvance(' '))

        # Enable word wrap
        self.setWordWrapMode(QTextOption.NoWrap)

        # Apply syntax highlighter
        self.highlighter = YamlHighlighter(self.document())

        # Connect signals
        self.blockCountChanged.connect(self.update_line_number_area_width)
        self.updateRequest.connect(self.update_line_number_area)

        self.update_line_number_area_width(0)

    def line_number_area_width(self):
        """Calculate the width needed for line numbers."""
        digits = len(str(max(1, self.blockCount())))
        space = 10 + self.fontMetrics().horizontalAdvance('9') * digits
        return space

    def update_line_number_area_width(self, _):
        """Update the viewport margins to accommodate line numbers."""
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)

    def update_line_number_area(self, rect, dy):
        """Update the line number area when scrolling."""
        if dy:
            self.line_number_area.scroll(0, dy)
        else:
            self.line_number_area.update(0, rect.y(), self.line_number_area.width(), rect.height())

        if rect.contains(self.viewport().rect()):
            self.update_line_number_area_width(0)

    def resizeEvent(self, event):
        """Handle resize events."""
        super().resizeEvent(event)
        cr = self.contentsRect()
        self.line_number_area.setGeometry(cr.left(), cr.top(), self.line_number_area_width(), cr.height())

    def line_number_area_paint_event(self, event):
        """Paint the line numbers."""
        painter = QPainter(self.line_number_area)
        painter.fillRect(event.rect(), QColor(53, 53, 53))

        block = self.firstVisibleBlock()
        block_number = block.blockNumber()
        top = self.blockBoundingGeometry(block).translated(self.contentOffset()).top()
        bottom = top + self.blockBoundingRect(block).height()

        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                number = str(block_number + 1)
                painter.setPen(QColor(180, 180, 180))
                painter.drawText(0, int(top), self.line_number_area.width() - 5,
                                 self.fontMetrics().height(), Qt.AlignRight, number)

            block = block.next()
            top = bottom
            bottom = top + self.blockBoundingRect(block).height()
            block_number += 1

    def keyPressEvent(self, event: QKeyEvent):
        """Handle key press events, converting tabs to spaces."""
        if event.key() == Qt.Key_Tab:
            # Insert 4 spaces instead of a tab
            cursor = self.textCursor()
            cursor.insertText("    ")
            return
        elif event.key() == Qt.Key_Backtab:
            # Handle Shift+Tab for unindent
            cursor = self.textCursor()
            if cursor.hasSelection():
                # Get selection range
                start = cursor.selectionStart()
                end = cursor.selectionEnd()
                cursor.setPosition(start)
                cursor.movePosition(QTextCursor.StartOfBlock)

                # Remove indentation from each selected line
                while cursor.position() < end:
                    cursor.movePosition(QTextCursor.StartOfBlock)
                    # Check if line starts with spaces
                    cursor.movePosition(QTextCursor.Right, QTextCursor.KeepAnchor, 4)
                    if cursor.selectedText() == "    ":
                        cursor.removeSelectedText()
                        end -= 4
                    else:
                        cursor.clearSelection()
                    if not cursor.movePosition(QTextCursor.Down):
                        break
            else:
                # Remove up to 4 spaces before cursor
                cursor.movePosition(QTextCursor.StartOfBlock, QTextCursor.KeepAnchor)
                text = cursor.selectedText()
                if text.endswith("    "):
                    cursor.setPosition(cursor.anchor())
                    cursor.movePosition(QTextCursor.Left, QTextCursor.KeepAnchor, 4)
                    cursor.removeSelectedText()
                elif text.endswith("   "):
                    cursor.setPosition(cursor.anchor())
                    cursor.movePosition(QTextCursor.Left, QTextCursor.KeepAnchor, 3)
                    cursor.removeSelectedText()
                elif text.endswith("  "):
                    cursor.setPosition(cursor.anchor())
                    cursor.movePosition(QTextCursor.Left, QTextCursor.KeepAnchor, 2)
                    cursor.removeSelectedText()
                elif text.endswith(" "):
                    cursor.setPosition(cursor.anchor())
                    cursor.movePosition(QTextCursor.Left, QTextCursor.KeepAnchor, 1)
                    cursor.removeSelectedText()
            return

        # Call parent's keyPressEvent for all other keys
        super().keyPressEvent(event)

    def insertFromMimeData(self, source):
        """Handle paste operations, converting tabs to spaces."""
        if source.hasText():
            text = source.text()
            # Replace all tabs with 4 spaces
            text = text.replace('\t', '    ')
            cursor = self.textCursor()
            cursor.insertText(text)
        else:
            super().insertFromMimeData(source)
