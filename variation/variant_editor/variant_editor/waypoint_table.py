#!/usr/bin/env python3
"""
Waypoint table widget for the Variation Editor.

This module provides the WaypointTableWidget class which handles:
- Tabular display of waypoints
- Interactive editing of waypoint coordinates
- Drag-and-drop reordering of waypoints
- Visual validity feedback
- Keyboard shortcuts for waypoint management
"""

from typing import List, Optional

from data_models import Pose
from PySide2.QtCore import Qt, Signal
from PySide2.QtGui import QColor
from PySide2.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView,
                               QPushButton, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)


class WaypointTableWidget(QWidget):
    """Container widget with buttons and table for editing waypoints with drag-and-drop support."""

    waypoint_changed = Signal(int, float, float, float)  # index, x, y, yaw
    waypoint_selected = Signal(int)
    waypoint_deleted = Signal(int)
    move_waypoint = Signal(int, int)  # from_index, to_index

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setup_ui()

        # Validity status for visual feedback
        self.waypoint_validity = []

        # Connect signals
        self.table.itemChanged.connect(self.on_item_changed)
        self.table.itemSelectionChanged.connect(self.on_selection_changed)

    def setup_ui(self):
        """Setup the complete UI with buttons and table."""
        # Create main layout
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)

        # Create button row
        button_layout = QHBoxLayout()

        self.delete_button = QPushButton("Delete Selected")
        self.move_up_button = QPushButton("Move Up")
        self.move_down_button = QPushButton("Move Down")

        button_layout.addWidget(self.delete_button)
        button_layout.addWidget(self.move_up_button)
        button_layout.addWidget(self.move_down_button)
        button_layout.addStretch()  # Push buttons to left

        # Connect button signals
        self.delete_button.clicked.connect(self.delete_selected_waypoint)
        self.move_up_button.clicked.connect(self.move_selected_up)
        self.move_down_button.clicked.connect(self.move_selected_down)

        # Add button row to main layout
        self.main_layout.addLayout(button_layout)

        # Create and setup table
        self.table = WaypointTable()
        self.setup_table()

        # Add table to main layout
        self.main_layout.addWidget(self.table)

    def setup_table(self):
        """Setup the table configuration."""
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(["X", "+/-", "Y", "+/-", "Yaw", "+/-"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setDragDropMode(QAbstractItemView.InternalMove)
        self.table.setDefaultDropAction(Qt.MoveAction)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)  # X
        header.setSectionResizeMode(1, QHeaderView.Fixed)  # +/-
        header.setSectionResizeMode(2, QHeaderView.Stretch)  # Y
        header.setSectionResizeMode(3, QHeaderView.Fixed)  # +/-
        header.setSectionResizeMode(4, QHeaderView.Stretch)  # Yaw
        header.setSectionResizeMode(5, QHeaderView.Fixed)  # +/-

        self.table.setColumnWidth(1, 40)  # +/-
        self.table.setColumnWidth(3, 40)  # +/-
        self.table.setColumnWidth(5, 40)  # +/-

    def delete_selected_waypoint(self):
        """Delete the currently selected waypoint."""
        selected_rows = [
            item.row() for item in self.table.selectionModel().selectedRows()
        ]
        if selected_rows:
            row = selected_rows[0]
            if row != 0:  # Don't delete start pose (first row)
                goal_index = row - 1  # Subtract 1 since first row is start
                self.waypoint_deleted.emit(goal_index)

    def move_selected_up(self):
        """Move the selected waypoint up in the list."""
        selected_rows = [
            item.row() for item in self.table.selectionModel().selectedRows()
        ]
        if selected_rows:
            row = selected_rows[0]
            if row > 1:  # Can't move start pose or first goal above start
                # Move from current position to one position up
                goal_index = row - 1  # Convert to goal index
                target_goal_index = goal_index - 1
                self.move_waypoint.emit(goal_index, target_goal_index)

    def move_selected_down(self):
        """Move the selected waypoint down in the list."""
        selected_rows = [
            item.row() for item in self.table.selectionModel().selectedRows()
        ]
        if selected_rows:
            row = selected_rows[0]
            if 0 < row < self.table.rowCount() - 1:  # Can't move start pose, and must not be last row
                # Move from current position to one position down
                goal_index = row - 1  # Convert to goal index
                target_goal_index = goal_index + 1
                self.move_waypoint.emit(goal_index, target_goal_index)

    def create_increment_buttons(self, row: int, column: int, value_type: str):
        """Create +/- buttons for incrementing/decrementing values."""
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(1)

        # Determine step size based on value type
        if value_type == "position":
            step = 0.1
        else:  # yaw
            step = 0.1  # 0.1 radians ≈ 5.7 degrees

        minus_btn = QPushButton("-")
        plus_btn = QPushButton("+")

        # Make buttons smaller
        minus_btn.setMaximumSize(20, 20)
        plus_btn.setMaximumSize(20, 20)
        minus_btn.setMinimumSize(20, 20)
        plus_btn.setMinimumSize(20, 20)

        layout.addWidget(minus_btn)
        layout.addWidget(plus_btn)

        # Connect button signals
        minus_btn.clicked.connect(lambda: self.increment_value(row, column, -step))
        plus_btn.clicked.connect(lambda: self.increment_value(row, column, step))

        return widget

    def increment_value(self, row: int, column: int, delta: float):
        """Increment/decrement a value in the table."""
        try:
            current_value = float(self.table.item(row, column).text())
            new_value = current_value + delta

            # Format the new value appropriately
            if column in [0, 2]:  # X, Y coordinates
                formatted_value = f"{new_value:.2f}"
            else:  # Yaw
                formatted_value = f"{new_value:.2f}"

            self.table.item(row, column).setText(formatted_value)
        except (ValueError, AttributeError):
            pass

    def set_waypoints(self, start_pose: Optional[Pose], goal_poses: List[Pose]):
        """Set the waypoints to display in the table."""
        self.table.blockSignals(True)

        self.table.setRowCount(len(goal_poses) + (1 if start_pose else 0))

        row = 0

        # Add start pose
        if start_pose:
            self.table.setItem(row, 0, QTableWidgetItem(f"{start_pose.position.x:.2f}"))
            self.table.setCellWidget(
                row, 1, self.create_increment_buttons(row, 0, "position")
            )
            self.table.setItem(row, 2, QTableWidgetItem(f"{start_pose.position.y:.2f}"))
            self.table.setCellWidget(
                row, 3, self.create_increment_buttons(row, 2, "position")
            )
            self.table.setItem(row, 4, QTableWidgetItem(f"{start_pose.yaw:.2f}"))
            self.table.setCellWidget(
                row, 5, self.create_increment_buttons(row, 4, "yaw")
            )
            row += 1

        # Add goal poses
        for _i, goal in enumerate(goal_poses):
            self.table.setItem(row, 0, QTableWidgetItem(f"{goal.position.x:.2f}"))
            self.table.setCellWidget(
                row, 1, self.create_increment_buttons(row, 0, "position")
            )
            self.table.setItem(row, 2, QTableWidgetItem(f"{goal.position.y:.2f}"))
            self.table.setCellWidget(
                row, 3, self.create_increment_buttons(row, 2, "position")
            )
            self.table.setItem(row, 4, QTableWidgetItem(f"{goal.yaw:.2f}"))
            self.table.setCellWidget(
                row, 5, self.create_increment_buttons(row, 4, "yaw")
            )
            row += 1

        self.table.blockSignals(False)
        self.update_visual_validity()

    def set_waypoint_validity(self, validity: List[bool]):
        """Set the validity status for waypoints and update visual feedback."""
        self.waypoint_validity = validity
        self.update_visual_validity()

    def update_visual_validity(self):
        """Update the visual representation of waypoint validity."""
        # Use default coloring for all waypoints (skip button columns)
        for row in range(self.table.rowCount()):
            for col in [0, 2, 4]:  # Skip button columns (1, 3, 5)
                item = self.table.item(row, col)
                if item:
                    item.setBackground(QColor(Qt.white))  # Reset to default

    def on_item_changed(self, item):
        """Handle item changes."""
        if item.column() in [0, 2, 4]:  # X, Y coordinate, or Yaw
            try:
                float(item.text())  # Validate it's a valid float
                row = item.row()

                # Get all values
                x = float(self.table.item(row, 0).text())
                y = float(self.table.item(row, 2).text())
                yaw_radians = float(self.table.item(row, 4).text())

                # Determine if this is start pose or goal pose
                if row == 0:  # First row is always start pose
                    # Start pose changed - emit signal with index -1
                    self.waypoint_changed.emit(-1, x, y, yaw_radians)
                else:
                    # Goal pose changed
                    goal_index = row - 1  # Subtract 1 since first row is start
                    self.waypoint_changed.emit(goal_index, x, y, yaw_radians)

            except ValueError:
                # Invalid input, revert
                self.table.blockSignals(True)
                if item.column() in [0, 2]:  # X or Y
                    item.setText("0.00")
                else:  # Yaw
                    item.setText("0.00")
                self.table.blockSignals(False)

    def on_selection_changed(self):
        """Handle selection changes."""
        selected_rows = [
            item.row() for item in self.table.selectionModel().selectedRows()
        ]
        if selected_rows:
            row = selected_rows[0]
            if row == 0:  # First row is always start pose
                self.waypoint_selected.emit(-1)
            else:
                goal_index = row - 1  # Subtract 1 since first row is start
                self.waypoint_selected.emit(goal_index)

    # Delegate methods to table widget for compatibility
    def selectRow(self, row: int):
        """Delegate selectRow to table."""
        self.table.selectRow(row)

    def selectionModel(self):
        """Delegate selectionModel to table."""
        return self.table.selectionModel()

    def item(self, row: int, column: int):
        """Delegate item access to table."""
        return self.table.item(row, column)

    def dropEvent(self, event):
        """Delegate drop events to table."""
        self.table.dropEvent(event)

    def keyPressEvent(self, event):
        """Delegate key events to table."""
        self.table.keyPressEvent(event)


class WaypointTable(QTableWidget):
    """Internal table widget that handles the actual table functionality."""

    waypoint_changed = Signal(int, float, float, float)  # index, x, y, yaw
    waypoint_selected = Signal(int)
    waypoint_deleted = Signal(int)
    move_waypoint = Signal(int, int)  # from_index, to_index

    def dropEvent(self, event):
        """Handle drop events for reordering."""
        if event.source() == self:
            # Get source and target rows
            source_row = self.currentRow()
            target_row = self.indexAt(event.pos()).row()

            if source_row != target_row and source_row >= 0 and target_row >= 0:
                # Only allow moving goal poses (not start pose at row 0)
                if source_row >= 1 and target_row >= 1:
                    source_goal_index = source_row - 1
                    target_goal_index = target_row - 1

                    self.parent().move_waypoint.emit(
                        source_goal_index, target_goal_index
                    )
                    event.accept()
                    return

        event.ignore()

    def keyPressEvent(self, event):
        """Handle key press events."""
        if event.key() == Qt.Key_Delete:
            selected_rows = [
                item.row() for item in self.selectionModel().selectedRows()
            ]
            if selected_rows:
                row = selected_rows[0]
                if row != 0:  # Don't delete start pose (first row)
                    goal_index = row - 1  # Subtract 1 since first row is start
                    self.parent().waypoint_deleted.emit(goal_index)
        else:
            super().keyPressEvent(event)
