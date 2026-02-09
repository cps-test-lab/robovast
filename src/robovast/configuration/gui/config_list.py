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


from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal
from PySide6.QtWidgets import (QHeaderView, QLabel, QTableView, QVBoxLayout,
                               QWidget)


class ConfigTableModel(QAbstractTableModel):
    """Table model for displaying config data."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.configs = []
        self.headers = ["Name"]

    def rowCount(self, parent=QModelIndex()):
        return len(self.configs)

    def columnCount(self, parent=QModelIndex()):
        return len(self.headers)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None

        if role == Qt.DisplayRole:
            config = self.configs[index.row()]
            return config.get("name", "Unknown")
        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.headers[section]
        return None

    def update_configs(self, configs):
        """Update the config list."""
        self.beginResetModel()
        self.configs = configs
        self.endResetModel()

    def get_config(self, row):
        """Get config data for a specific row."""
        if 0 <= row < len(self.configs):
            return self.configs[row]
        return None


class ConfigList(QWidget):
    """Custom view for displaying config information."""

    # Signal emitted when a config is selected
    config_selected = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.init_ui()

    def init_ui(self):
        """Initialize the user interface."""
        layout = QVBoxLayout()
        self.setLayout(layout)
        layout.setContentsMargins(0, 0, 0, 0)

        # Add label
        label = QLabel("Configs")
        layout.addWidget(label)

        # Create table view
        self.table_view = QTableView()
        self.model = ConfigTableModel()
        self.table_view.setModel(self.model)

        # Configure table appearance
        self.table_view.setAlternatingRowColors(True)
        self.table_view.setSelectionBehavior(QTableView.SelectRows)
        self.table_view.setSelectionMode(QTableView.SingleSelection)
        self.table_view.horizontalHeader().setVisible(False)
        self.table_view.horizontalHeader().setStretchLastSection(True)
        self.table_view.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table_view.verticalHeader().setVisible(False)

        # Apply dark theme styling
        self.table_view.setStyleSheet("""
            QTableView {
                background-color: #1e1e1e;
                color: #d4d4d4;
                border: 1px solid #555;
                gridline-color: #3e3e42;
            }
            QTableView::item:selected {
                background-color: #264f78;
            }
            QTableView::item:hover {
                background-color: #2a2d2e;
            }
            QTableView::item:selected:hover {
                background-color: #2d6fa8;
            }
            QHeaderView::section {
                background-color: #2d2d30;
                color: #cccccc;
                padding: 5px;
                border: 1px solid #555;
            }
        """)

        layout.addWidget(self.table_view)

        # Connect selection changed signal
        self.table_view.selectionModel().selectionChanged.connect(self.on_selection_changed)

    def on_selection_changed(self, selected, deselected):
        """Handle selection changes in the table."""
        indexes = selected.indexes()
        if indexes:
            row = indexes[0].row()
            config_data = self.model.get_config(row)
            if config_data:
                # Emit the full config data (which includes 'data' key with actual config)
                self.config_selected.emit(config_data.get("data", config_data))

    def update_configs(self, configs):
        """Update the configs list.

        Args:
            configs: List of config dictionaries with keys like 'name', 'status', etc.
        """
        self.model.update_configs(configs)

    def select_config(self, index):
        """Programmatically select a config by index.

        Args:
            index: The row index of the config to select (0-based).
        """
        if 0 <= index < self.model.rowCount():
            # Select the specified row using setCurrentIndex
            model_index = self.model.index(index, 0)
            self.table_view.setCurrentIndex(model_index)
            self.table_view.selectRow(index)
            # Ensure the selected row is visible
            self.table_view.scrollTo(model_index)

    def clear(self):
        """Clear all configs from the list."""
        self.model.update_configs([])
