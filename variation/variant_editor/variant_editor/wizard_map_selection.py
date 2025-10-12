#!/usr/bin/env python3
"""
Map selection page for the Variant Creation Wizard.
"""

import os
import random

from PySide2.QtCore import QEvent, Qt, Signal
from PySide2.QtWidgets import (QAbstractItemView, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem, QPushButton,
                               QVBoxLayout)
from robovast_common import get_scenario_base_path
from variant_editor.data_models import Pose, Position, Variant, VariantData
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
        self.selected_maps = []
        self.robot_diameter = 0.3  # default robot diameter in meters
        self.custom_floorplan_dir = None  # Can be set by previous page
        self.maps_dir = None
        self.setup_ui()

    def set_maps_dir(self, maps_dir: str):
        self.maps_dir = maps_dir

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
        from pprint import pprint
        pprint(self.input_variants)
        maps_found = []
        if os.path.isdir(self.maps_dir):
            print(f"Loading maps from {self.maps_dir}")
            for map_name in os.listdir(self.maps_dir):
                map_dir = os.path.join(self.maps_dir, map_name, "maps")
                if os.path.isdir(map_dir):
                    for fname in os.listdir(map_dir):
                        if fname.lower().endswith(".yaml"):
                            map_file = fname
                            break
 
                    if map_file:
                        # split folder name like "rooms_6734" -> type "rooms", variation "6734"
                        parts = map_name.rsplit("_", 1)
                        map_type = parts[0]
                        maps_found.append(
                            {
                                "name": map_name,
                                "variation": map_type,
                                "location": os.path.join(map_name, "maps", map_file),
                            }
                        )

        from pprint import pprint
        pprint(maps_found)
        for map_info in maps_found:
            item = QListWidgetItem(f"{map_info['name']}")
            item.setData(Qt.UserRole, map_info)
            self.map_list.addItem(item)
        if not maps_found:
            item = QListWidgetItem("No map files found in workspace")
            item.setFlags(item.flags() & ~Qt.ItemIsSelectable)
            self.map_list.addItem(item)

    def on_selection_changed(self):
        selected_items = self.map_list.selectedItems()
        self.selected_maps = []
        for item in selected_items:
            map_info = item.data(Qt.UserRole)
            if map_info:
                self.selected_maps.append(map_info)
        count = len(self.selected_maps)
        if count == 0:
            self.selection_info.setText("No maps selected")
        elif count == 1:
            self.selection_info.setText("1 map selected")
        else:
            self.selection_info.setText(f"{count} maps selected")
        self.maps_selected.emit(self.selected_maps)
        self.completeChanged.emit()
        # Emit generic signal for status message update
        self.emit_status_message(f"{count} maps selected")
        # Prepare VariantData objects for downstream pages
        variants = []
        for map_info in self.selected_maps:
            variant = Variant(
                mesh_file=os.path.join("3d-mesh", f"{map_info['variation']}.stl"),
                map_file=os.path.join("maps", f"{map_info['variation']}.yaml"),
                start_pose=Pose(Position(0.0, 0.0)),
                goal_poses=[],
            )
            # Create VariantData with a default name and file path
            variant_data = VariantData(
                name=map_info["name"],
                floorplan_variant_name=map_info["name"],
                floorplan_variation=map_info["variation"],
                variant=variant,
            )

            variants.append(variant_data)
        self.set_output_variants(variants)

    def isComplete(self):
        return len(self.selected_maps) > 0

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
                    map_info = item.data(Qt.UserRole)
                    if map_info:
                        # load and preview hovered map
                        self.map_widget.load_map(os.path.join(self.maps_dir, map_info["location"]))
                else:
                    self.map_widget.clear()
                return False
            elif event.type() == QEvent.Leave:
                self.map_widget.clear()
                return False
        return super().eventFilter(source, event)
