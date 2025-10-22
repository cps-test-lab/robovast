#!/usr/bin/env python3
"""
Map selection page for the Variant Creation Wizard.
"""

import os

from PySide2.QtCore import QEvent, Qt, Signal
from PySide2.QtWidgets import (QAbstractItemView, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem, QPushButton,
                               QVBoxLayout)
from variant_editor.map_widget import MapWidget
from variant_editor.wizard_base_page import WizardBasePage


class MapSelectionPage(WizardBasePage):
    """First page: Select which maps should be used for variant generation."""

    maps_selected = Signal(list)

    def __init__(self):
        super().__init__()
        self.setTitle("Map Selection")
        self.setSubTitle(
            "Select the maps that should be used for generating navigation variants."
        )
        self.robot_diameter = 0.3  # default robot diameter in meters
        self.custom_floorplan_dir = None  # Can be set by previous page
        self.setup_ui()

    def set_input_variants(self, variants):
        super().set_input_variants(variants)
        self.load_available_maps()

    def setup_ui(self):
        # Use horizontal layout: list on left, map preview on right
        main_layout = QHBoxLayout()
        # Left side: instructions, buttons, list, info
        left_layout = QVBoxLayout()
        instructions = QLabel(
            "Select one or more maps from the list below. Navigation paths will be "
            "generated for each selected map in the next step."
        )
        instructions.setWordWrap(True)
        left_layout.addWidget(instructions)
        # Add select/unselect all buttons
        button_layout = QHBoxLayout()
        select_all_btn = QPushButton("Select All")
        unselect_all_btn = QPushButton("Unselect All")
        select_all_btn.clicked.connect(self.select_all_maps)
        unselect_all_btn.clicked.connect(self.unselect_all_maps)
        button_layout.addWidget(select_all_btn)
        button_layout.addWidget(unselect_all_btn)
        left_layout.addLayout(button_layout)
        # Map list
        self.map_list = QListWidget()
        self.map_list.setSelectionMode(QAbstractItemView.MultiSelection)
        self.map_list.itemSelectionChanged.connect(self.on_selection_changed)
        # enable hover tracking
        self.map_list.setMouseTracking(True)
        self.map_list.viewport().setMouseTracking(True)
        self.map_list.viewport().installEventFilter(self)
        left_layout.addWidget(self.map_list)
        # Info label
        self.selection_info = QLabel("No maps selected")
        left_layout.addWidget(self.selection_info)
        # Right side: map preview widget
        self.map_widget = MapWidget()
        self.map_widget.set_robot_diameter(self.robot_diameter)
        # assemble layouts
        main_layout.addLayout(left_layout)
        main_layout.addWidget(self.map_widget)
        self.setLayout(main_layout)

    def load_available_maps(self):
        self.map_list.clear()
        found_variants = []
        for variant in self.input_variants:
            map_dir = os.path.join(variant["floorplan_variant_path"], "maps")
            if os.path.isdir(map_dir):
                map_file = None
                for fname in os.listdir(map_dir):
                    if fname.lower().endswith(".yaml"):
                        map_file = fname
                        break

                if map_file:
                    # split folder name like "rooms_6734" -> type "rooms", variation "6734"
                    parts = variant["name"].rsplit("_", 1)
                    floorplan_variation = parts[0]
                    variant['mesh_file'] = os.path.join("3d-mesh", f"{floorplan_variation}.stl")
                    variant['map_file'] = os.path.join("maps", f"{floorplan_variation}.yaml")
                    found_variants.append(variant)
                else:
                    raise FileNotFoundError(f"No map file found in {map_dir}")

        for variant in found_variants:
            item = QListWidgetItem(f"{variant['name']}")
            item.setData(Qt.UserRole, variant)
            self.map_list.addItem(item)
        if not found_variants:
            item = QListWidgetItem("No map files found in workspace")
            item.setFlags(item.flags() & ~Qt.ItemIsSelectable)
            self.map_list.addItem(item)

    def on_selection_changed(self):
        selected_items = self.map_list.selectedItems()
        self.selected_variants = []
        for item in selected_items:
            variant = item.data(Qt.UserRole)
            if variant:
                self.selected_variants.append(variant)
        count = len(self.selected_variants)
        if count == 0:
            self.selection_info.setText("No variants selected")
        elif count == 1:
            self.selection_info.setText("1 variant selected")
        else:
            self.selection_info.setText(f"{count} variants selected")
        self.maps_selected.emit(self.selected_variants)
        self.completeChanged.emit()
        # Emit generic signal for status message update
        self.emit_status_message(f"{count} variants selected")
        self.set_output_variants(self.selected_variants)

    def select_all_maps(self):
        for i in range(self.map_list.count()):
            item = self.map_list.item(i)
            if item.flags() & Qt.ItemIsSelectable:
                item.setSelected(True)

    def unselect_all_maps(self):
        for i in range(self.map_list.count()):
            item = self.map_list.item(i)
            item.setSelected(False)

    def set_robot_diameter(self, diameter: float):
        """Set the robot diameter for preview rendering."""
        self.robot_diameter = diameter
        self.map_widget.set_robot_diameter(diameter)

    def eventFilter(self, source, event):
        if source is self.map_list.viewport():
            if event.type() == QEvent.MouseMove:
                pos = event.pos()
                item = self.map_list.itemAt(pos)
                if item:
                    variant = item.data(Qt.UserRole)
                    if variant:
                        # load and preview hovered map
                        self.map_widget.load_map(os.path.join(variant["floorplan_variant_path"], variant.get('map_file', None)))
                else:
                    self.map_widget.clear()
                return False
            elif event.type() == QEvent.Leave:
                self.map_widget.clear()
                return False
        return super().eventFilter(source, event)
