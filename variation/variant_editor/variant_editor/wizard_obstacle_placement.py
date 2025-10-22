#!/usr/bin/env python3
"""
Obstacle placement page and thread for the Variant Creation Wizard.
"""

import os

from PySide2.QtCore import Qt, Signal
from PySide2.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox,
                               QFormLayout, QGroupBox, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMessageBox, QProgressBar,
                               QPushButton, QSizePolicy, QSpinBox, QSplitter,
                               QTableWidget, QVBoxLayout, QWidget)
from robovast_common import ObstacleVariation
from robovast_common.navigation import PathGenerator
from variant_editor.map_widget import MapWidget
from variant_editor.wizard_base_page import WizardBasePage

from .variation_thread import VariationThread


def get_default_xacro_args_for_type(object_type: str) -> str:
    """
    Get default xacro arguments for an object type.

    Args:
        object_type: Type of object

    Returns:
        Default xacro arguments string
    """
    defaults = {
        "box": "width:=0.5, length:=0.5, height:=1.0",
        "cylinder": "diameter:=0.5, height:=1.0",
    }

    return defaults.get(object_type, "width:=0.5, length:=0.5")


class ObstaclePlacementPage(WizardBasePage):
    """Third page: Configure obstacle placement parameters and generate obstacles."""

    variant_count_changed = Signal()  # Signal for when variant count changes

    def __init__(self):
        super().__init__()

        self.setTitle("Obstacle Placement")
        self.setSubTitle(
            "Configure obstacle placement parameters and add obstacles to navigation variants."
        )

        self.input_variants = []  # Variants from path generation
        self.generated_variants = []  # Final variants with obstacles
        self.generation_thread = None

        # Robot diameter (set from global parameter)
        self.robot_diameter = 0.354  # Default value, will be updated from global settings

        # Track previous configurations to detect changes
        self._prev_configs = []
        self.maps_dir = None

        self.setup_ui()
        # Load default configurations
        self.add_default_configs()
        self._prev_configs = self.get_obstacle_configs().copy()
        self.update_variant_count()

    def set_maps_dir(self, maps_dir: str):
        self.maps_dir = maps_dir

    def setup_ui(self):
        """Set up the user interface."""
        main_layout = QHBoxLayout()

        # Left panel - obstacle configuration
        left_panel = QVBoxLayout()

        # Input variants count
        self.input_variants_label = QLabel("0 variants from path generation")
        left_panel.addWidget(self.input_variants_label)

        # Obstacle configuration table
        config_group = QGroupBox("Obstacle Configuration")
        config_layout = QVBoxLayout()

        # Table for obstacle configurations
        self.config_table = QTableWidget()
        self.config_table.setColumnCount(4)
        self.config_table.setHorizontalHeaderLabels(
            ["Count", "Max Dist", "Model", "Xacro Args"]
        )

        # Set column widths
        header = self.config_table.horizontalHeader()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.setSectionResizeMode(1, QHeaderView.Fixed)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        self.config_table.setColumnWidth(0, 50)
        self.config_table.setColumnWidth(1, 80)

        config_layout.addWidget(self.config_table)

        # Buttons for table management
        table_buttons_layout = QHBoxLayout()

        self.add_config_button = QPushButton("Add Configuration")
        self.add_config_button.clicked.connect(self.add_obstacle_config)
        table_buttons_layout.addWidget(self.add_config_button)

        self.remove_config_button = QPushButton("Remove Selected")
        self.remove_config_button.clicked.connect(self.remove_obstacle_config)
        table_buttons_layout.addWidget(self.remove_config_button)

        table_buttons_layout.addStretch()
        config_layout.addLayout(table_buttons_layout)

        config_group.setLayout(config_layout)
        left_panel.addWidget(config_group)

        # Generate button
        self.generate_button = QPushButton("Generate Obstacles")
        self.generate_button.clicked.connect(self.start_generation)
        self.generate_button.setEnabled(False)
        left_panel.addWidget(self.generate_button)

        # Stop button
        self.stop_button = QPushButton("Stop Generation")
        self.stop_button.clicked.connect(self.stop_generation)
        self.stop_button.setEnabled(False)
        left_panel.addWidget(self.stop_button)

        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        left_panel.addWidget(self.progress_bar)

        # Obstacle Placement Seed Configuration
        seed_group = QGroupBox("Obstacle Placement Seed")
        seed_layout = QFormLayout()

        self.obstacle_placement_seed_spin = QSpinBox()
        self.obstacle_placement_seed_spin.setRange(0, 2**31 - 1)
        self.obstacle_placement_seed_spin.setValue(789012)  # Default value
        self.obstacle_placement_seed_spin.setToolTip(
            "Random seed for obstacle placement generation"
        )
        self.obstacle_placement_seed_spin.valueChanged.connect(
            self.on_seed_changed)
        seed_layout.addRow(
            "Obstacle Placement Seed:", self.obstacle_placement_seed_spin
        )

        seed_group.setLayout(seed_layout)
        left_panel.addWidget(seed_group)

        # Skip obstacles option
        skip_group = QGroupBox("Skip Obstacle Placement")
        skip_layout = QVBoxLayout()

        self.skip_obstacles_checkbox = QCheckBox(
            "Skip obstacle placement and use path generation results only"
        )
        self.skip_obstacles_checkbox.stateChanged.connect(
            self.on_skip_obstacles_changed
        )
        skip_layout.addWidget(self.skip_obstacles_checkbox)

        skip_group.setLayout(skip_layout)
        left_panel.addWidget(skip_group)

        left_panel.addStretch()

        # Middle panel - Map visualization
        middle_panel = QVBoxLayout()
        map_group = QGroupBox("Obstacle Visualization")
        map_group.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        map_layout = QVBoxLayout()

        # Costmap toggle
        costmap_layout = QHBoxLayout()
        self.show_costmap_checkbox = QCheckBox("Show Costmap Overlay")
        self.show_costmap_checkbox.setChecked(True)
        self.show_costmap_checkbox.stateChanged.connect(self.on_costmap_toggle)
        costmap_layout.addWidget(self.show_costmap_checkbox)
        costmap_layout.addStretch()
        map_layout.addLayout(costmap_layout)

        self.map_widget = MapWidget(waypoint_movement_enabled=False)
        self.map_widget.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Expanding)
        map_layout.addWidget(self.map_widget)
        map_group.setLayout(map_layout)
        middle_panel.addWidget(map_group)

        # Right panel - Generated variants
        right_panel = QVBoxLayout()
        variants_group = QGroupBox("Generated Variants")
        variants_group.setSizePolicy(
            QSizePolicy.Expanding,
            QSizePolicy.Expanding)
        variants_layout = QVBoxLayout()

        self.variants_list = QListWidget()
        self.variants_list.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.variants_list.currentItemChanged.connect(self.on_variant_selected)
        variants_layout.addWidget(self.variants_list)
        variants_group.setLayout(variants_layout)
        right_panel.addWidget(variants_group)

        # Create splitter for 3 panels
        splitter = QSplitter(Qt.Horizontal)

        left_widget = QWidget()
        left_widget.setLayout(left_panel)
        splitter.addWidget(left_widget)

        middle_widget = QWidget()
        middle_widget.setLayout(middle_panel)
        middle_widget.setSizePolicy(
            QSizePolicy.Expanding,
            QSizePolicy.Expanding)
        splitter.addWidget(middle_widget)

        right_widget = QWidget()
        right_widget.setLayout(right_panel)
        right_widget.setSizePolicy(
            QSizePolicy.Expanding,
            QSizePolicy.Expanding)
        splitter.addWidget(right_widget)

        splitter.setSizes([250, 600, 250])
        splitter.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        main_layout.addWidget(splitter)
        self.setLayout(main_layout)

    def set_robot_diameter(self, diameter):
        """Set the robot diameter from global parameter."""
        self.robot_diameter = diameter

    def save_configurations(self):
        """Check for configuration changes and potentially clear variants."""
        configs = self.get_obstacle_configs()

        # Check if configuration has changed and we have generated variants
        if self.generated_variants and configs != self._prev_configs:
            res = QMessageBox.question(
                self,
                "Clear Variants?",
                "Changing obstacle configuration will remove previously generated variants. Continue?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if res == QMessageBox.No:
                # Revert to previous configurations
                self.revert_to_previous_configs()
                return
            # Clear previous results
            self.clear_generated_variants()

        # Update the tracked configuration
        self._prev_configs = configs.copy()

    def revert_to_previous_configs(self):
        """Revert the table to the previous configuration."""
        # Restore previous configurations
        self.config_table.setRowCount(0)
        for config in self._prev_configs:
            self.add_obstacle_config_row(config, save=False)
        self.update_variant_count()

    def clear_generated_variants(self):
        """Clear all generated variants."""
        self.generated_variants.clear()
        self.output_variants.clear()
        self.variants_list.clear()
        self.emit_variant_count_update(0)
        self.completeChanged.emit()

    def on_seed_changed(self, value):
        """Handle changes to the obstacle placement seed spinbox."""
        self.parameters['obstacle_placement_seed'] = value

    def add_default_configs(self):
        """Add some default obstacle configurations."""
        default_configs = [
            {
                "amount": 0,
                "max_distance": 0.0,
                "model": "box",
                "xacro_arguments": get_default_xacro_args_for_type("box"),
            },
            {
                "amount": 5,
                "max_distance": 2.0,
                "model": "box",
                "xacro_arguments": get_default_xacro_args_for_type("box"),
            },
            {
                "amount": 10,
                "max_distance": 1.5,
                "model": "cylinder",
                "xacro_arguments": get_default_xacro_args_for_type("cylinder"),
            },
        ]

        for config in default_configs:
            self.add_obstacle_config_row(config, save=False)

        self.update_variant_count()

    def add_obstacle_config(self):
        """Add a new obstacle configuration row."""
        config = {
            "amount": 5,
            "max_distance": 2.0,
            "model": "box",
            "xacro_arguments": get_default_xacro_args_for_type("box"),
        }
        self.add_obstacle_config_row(config, save=True)
        self.update_variant_count()

    def add_obstacle_config_row(self, config, save=True):
        """Add a configuration row to the table."""
        row = self.config_table.rowCount()
        self.config_table.insertRow(row)

        # Amount (SpinBox)
        amount_spin = QSpinBox()
        amount_spin.setRange(0, 100)
        amount_spin.setValue(config["amount"])
        amount_spin.valueChanged.connect(self.update_variant_count)
        if save:
            amount_spin.valueChanged.connect(self.save_configurations)
        self.config_table.setCellWidget(row, 0, amount_spin)

        # Max Distance (DoubleSpinBox)
        distance_spin = QDoubleSpinBox()
        distance_spin.setRange(0.1, 10.0)
        distance_spin.setValue(config["max_distance"])
        distance_spin.setSuffix(" m")
        distance_spin.setSingleStep(0.1)
        if save:
            distance_spin.valueChanged.connect(self.save_configurations)
        self.config_table.setCellWidget(row, 1, distance_spin)

        # Model (ComboBox)
        model_combo = QComboBox()
        model_combo.addItems(["box", "cylinder"])
        model_combo.setCurrentText(config["model"])
        if save:
            model_combo.currentTextChanged.connect(self.save_configurations)

        # Connect model change to update xacro arguments
        model_combo.currentTextChanged.connect(
            lambda model, r=row: self.update_xacro_args_for_model(r, model)
        )
        self.config_table.setCellWidget(row, 2, model_combo)

        # Xacro Arguments (LineEdit)
        xacro_edit = QLineEdit()
        xacro_edit.setText(config["xacro_arguments"])
        if save:
            xacro_edit.textChanged.connect(self.save_configurations)
        self.config_table.setCellWidget(row, 3, xacro_edit)

    def update_xacro_args_for_model(self, row, model):
        """Update xacro arguments when model is changed."""
        xacro_widget = self.config_table.cellWidget(row, 3)
        if xacro_widget:
            # Use default arguments for the selected model type
            default_args = get_default_xacro_args_for_type(model)
            xacro_widget.setText(default_args)

    def remove_obstacle_config(self):
        """Remove the selected obstacle configuration."""
        current_row = self.config_table.currentRow()
        if current_row >= 0:
            self.config_table.removeRow(current_row)
            self.update_variant_count()
            self.save_configurations()

    def get_obstacle_configs(self):
        """Get all obstacle configurations from the table."""
        # Object type to model path lookup table
        model_lookup = {
            "box": "gazebo_scenario_models://models/box.sdf.xacro",
            "cylinder": "gazebo_scenario_models://models/cylinder.sdf.xacro",
        }

        configs = []
        for row in range(self.config_table.rowCount()):
            amount_widget = self.config_table.cellWidget(row, 0)
            distance_widget = self.config_table.cellWidget(row, 1)
            model_widget = self.config_table.cellWidget(row, 2)
            xacro_widget = self.config_table.cellWidget(row, 3)

            if amount_widget and distance_widget and model_widget and xacro_widget:
                object_type = model_widget.currentText()
                model_path = model_lookup.get(object_type, object_type)

                config = {
                    "amount": amount_widget.value(),
                    "max_distance": distance_widget.value(),
                    "model": model_path,
                    "xacro_arguments": xacro_widget.text(),
                }
                configs.append(config)

        # Update self.parameters
        self.parameters['obstacle_configs'] = configs
        return configs

    def apply_parameters(self, parameters):
        """Apply parameters from a dictionary to the UI widgets."""
        super().apply_parameters(parameters)

        # Set the obstacle placement seed
        if 'obstacle_placement_seed' in parameters:
            self.obstacle_placement_seed_spin.setValue(parameters['obstacle_placement_seed'])

        # Load obstacle configurations
        obstacle_configs = parameters.get('obstacle_configs', [])
        if obstacle_configs:
            # Clear existing configurations
            self.config_table.setRowCount(0)
            # Load saved configurations
            for config in obstacle_configs:
                self.add_obstacle_config_row(config, save=False)
            # Update tracking
            self._prev_configs = obstacle_configs.copy()

    def apply_global_parameters(self, parameters):
        """Apply global parameters like robot diameter."""
        super().apply_global_parameters(parameters)

        if 'robot_diameter' in parameters:
            self.set_robot_diameter(parameters['robot_diameter'])

    def set_input_variants(self, variants):
        """Set the input variants from the path generation page."""
        self.input_variants = variants
        count = len(variants)
        self.input_variants_label.setText(
            f"{count} variant{'s' if count != 1 else ''} from path generation"
        )
        self.generate_button.setEnabled(
            count > 0 and self.config_table.rowCount() > 0)
        self.update_variant_count()

        # Show first variant in map if available
        if variants:
            self.display_variant_in_map(variants[0])

        # Emit completion changed signal to update Next button
        self.completeChanged.emit()

    def display_variant_in_map(self, variant):
        """Display a variant in the map widget."""
        if variant and "floorplan_variant_path" in variant and "map_file" in variant:
            map_path = os.path.join(variant["floorplan_variant_path"], variant["map_file"])
            # Load map
            if os.path.exists(map_path):
                self.map_widget.load_map(map_path)

                # Show path
                if "path" in variant:
                    self.map_widget.set_path(variant["path"])
                else:
                    self.map_widget.set_path([])

                # Show start and goal poses
                if "start_pose" in variant:
                    self.map_widget.start_pose = variant["start_pose"]

                if "goal_poses" in variant:
                    self.map_widget.goal_poses = variant["goal_poses"]

                # Always update static objects (obstacles)
                self.map_widget.static_objects = variant.get("static_objects", [])

                # Generate and show costmap with obstacles
                try:
                    generator = PathGenerator(
                        map_path, self.robot_diameter
                    )  # Use global robot diameter
                    costmap = generator.get_costmap_with_obstacles(
                        variant.get("static_objects", [])
                    )
                    if costmap is not None:
                        show_overlay = self.show_costmap_checkbox.isChecked()
                        self.map_widget.set_costmap(
                            costmap, show_overlay=show_overlay)
                except Exception as e:
                    print(f"Warning: Could not generate costmap: {e}")

                self.map_widget.update()

    def update_variant_count(self):
        """Update the variant count label."""
        self.emit_variant_count_update(len(self.generated_variants))

    def start_generation(self):
        """Start the obstacle placement generation process."""
        # Check if obstacle placement is being skipped
        if self.skip_obstacles_checkbox.isChecked():
            QMessageBox.information(
                self,
                "Skipping Obstacles",
                "Obstacle placement is currently skipped. Uncheck 'Skip obstacle placement' to generate obstacles.",
            )
            return

        if not self.input_variants:
            QMessageBox.warning(
                self, "No Variants", "No variants from path generation."
            )
            return

        obstacle_configs = self.get_obstacle_configs()
        if not obstacle_configs:
            QMessageBox.warning(
                self, "No Configurations", "No obstacle configurations defined."
            )
            return

        # Disable generate button and show progress
        self.generate_button.setEnabled(False)
        self.config_table.setEnabled(False)
        self.add_config_button.setEnabled(False)
        self.remove_config_button.setEnabled(False)
        self.stop_button.setEnabled(True)

        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)  # Indeterminate progress

        # Clear previous results
        self.clear_generated_variants()

        # Clear map display
        self.map_widget.set_path([])
        self.map_widget.static_objects = []
        self.map_widget.update()

        # Build settings dict for ObstacleVariation
        parameters = {
            'general': self.global_parameters,
            'ObstacleVariation': self.parameters
        }

        # Start generation in background thread
        self.generation_thread = VariationThread(
            ObstacleVariation,
            parameters
        )
        self.generation_thread.set_input_variants(self.input_variants)

        self.generation_thread.generation_complete.connect(
            self.on_generation_complete)
        self.generation_thread.progress_update.connect(self.emit_status_message)
        self.generation_thread.start()

    def stop_generation(self):
        """Stop the obstacle placement generation."""
        if self.generation_thread and self.generation_thread.isRunning():
            self.generation_thread.stop()
            self.generation_thread.wait()

    # def on_variant_generated(self, variant_data):
    #     """Handle a newly generated variant."""
    #     self.generated_variants.append(variant_data)

    #     # Add to list widget
    #     item = QListWidgetItem(variant_data.name)
    #     item.setData(Qt.UserRole, variant_data)
    #     self.variants_list.addItem(item)

    #     # Update output_variants for base class
    #     self.output_variants = self.generated_variants.copy()

    #     # Update count
    #     self.emit_variant_count_update(len(self.generated_variants))

    def on_generation_complete(self, variants):
        """Handle generation completion."""
        self.progress_bar.setVisible(False)

        # Re-enable controls
        self.generate_button.setEnabled(True)
        self.config_table.setEnabled(True)
        self.add_config_button.setEnabled(True)
        self.remove_config_button.setEnabled(True)
        self.stop_button.setEnabled(False)

        # Populate the variants list
        for variant in variants:
            item = QListWidgetItem(variant['name'])
            item.setData(Qt.UserRole, variant)
            self.variants_list.addItem(item)

        self.set_output_variants(variants)
        # Update status
        count = len(self.output_variants)
        self.emit_status_message(
            f"Obstacle placement complete - {count} variants generated"
        )

        # Update previous configs to current state after successful generation
        self._prev_configs = self.get_obstacle_configs().copy()

        # Select first variant if available
        if self.variants_list.count() > 0:
            self.variants_list.setCurrentRow(0)

        # Emit completion changed signal to update Next button
        self.completeChanged.emit()

    def on_status_update(self, message):
        """Handle status updates from the generation thread."""
        self.emit_status_message(message)

    def on_variant_selected(self, current, previous):
        """Handle variant selection in the list."""
        if current:
            variant = current.data(Qt.UserRole)
            if variant:
                self.display_variant_in_map(variant)

    def get_generated_variants(self):
        """Get the list of generated variants."""
        return self.generated_variants

    def isComplete(self):
        """Check if the page is complete."""
        # Page is complete if we have generated variants OR if skipping
        # obstacles with input variants
        return len(self.output_variants) > 0 or (
            self.skip_obstacles_checkbox.isChecked() and len(self.output_variants) > 0
        )

    def on_skip_obstacles_changed(self, state):
        """Handle skip obstacles checkbox state change."""
        skip_enabled = state == Qt.Checked

        # Enable/disable obstacle configuration
        self.config_table.setEnabled(not skip_enabled)
        self.add_config_button.setEnabled(not skip_enabled)
        self.remove_config_button.setEnabled(not skip_enabled)
        self.generate_button.setEnabled(
            not skip_enabled and len(self.input_variants) > 0
        )

        if skip_enabled:
            # If skipping obstacles, use input variants as output variants
            self.output_variants = self.input_variants.copy()
            self.variants_list.clear()

            # Add all input variants to the list
            for variant in self.input_variants:
                item = QListWidgetItem(variant['name'])
                item.setData(Qt.UserRole, variant)
                self.variants_list.addItem(item)

            # Select first variant if available
            if self.variants_list.count() > 0:
                self.variants_list.setCurrentRow(0)

            # Update status
            self.emit_status_message(
                f"Skipping obstacle placement - using {
                    len(self.output_variants)} variants from path generation"
            )
            self.emit_variant_count_update(len(self.output_variants))
        else:
            # Clear generated variants when not skipping
            self.output_variants = []
            self.variants_list.clear()
            self.emit_variant_count_update(0)

        # Emit completion changed signal to update Next button
        self.completeChanged.emit()

    def on_costmap_toggle(self, state):
        """Handle costmap overlay toggle."""
        show_costmap = state == Qt.Checked
        if hasattr(self.map_widget, "show_costmap"):
            self.map_widget.show_costmap = show_costmap
            self.map_widget.update()
