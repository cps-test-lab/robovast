#!/usr/bin/env python3
"""
Obstacle placement page and thread for the Variant Creation Wizard.
"""

import copy
import os
import random

from PySide2.QtCore import Qt, QThread, Signal
from PySide2.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox,
                               QFormLayout, QGroupBox, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMessageBox, QProgressBar,
                               QPushButton, QSizePolicy, QSpinBox, QSplitter,
                               QTableWidget, QVBoxLayout, QWidget)
from obovast_common import get_variant_map_path
from variant_editor.map_widget import MapWidget
from variant_editor.object_shapes import get_default_xacro_args_for_type
from variant_editor.obstacle_placer import ObstaclePlacer
from variant_editor.path_generator import PathGenerator
from variant_editor.wizard_base_page import WizardBasePage


class ObstaclePlacementThread(QThread):
    """Thread for generating obstacle placements in the background."""

    variant_generated = Signal(object)  # VariantData object
    generation_finished = Signal()
    status_update = Signal(str)

    def __init__(
        self,
        existing_variants,
        obstacle_configs,
        obstacle_placement_seed,
        robot_diameter,
        maps_dir,
    ):
        super().__init__()
        self.existing_variants = existing_variants
        self.obstacle_configs = obstacle_configs
        self.obstacle_placement_seed = obstacle_placement_seed
        self.robot_diameter = robot_diameter
        self.should_stop = False
        self.maps_dir = maps_dir

    def stop(self):
        """Request the thread to stop."""
        self.should_stop = True

    def on_status_update(self, message):
        """Handle status updates from the placer."""
        self.status_update.emit(message)

    def run(self):
        """Run the obstacle placement generation."""
        try:
            total_variants = len(self.existing_variants) * \
                len(self.obstacle_configs)
            current_variant = 0

            # Set the random seed once at the beginning for all obstacle
            # placement
            if self.obstacle_placement_seed is not None:
                random.seed(self.obstacle_placement_seed)

            for variant_data in self.existing_variants:
                if self.should_stop:
                    break

                for config in self.obstacle_configs:
                    if self.should_stop:
                        break

                    current_variant += 1
                    self.status_update.emit(
                        f"Placing obstacles for variant {current_variant}/{total_variants}"
                    )

                    # Create a copy of the variant
                    new_variant_data = copy.deepcopy(variant_data)

                    # Place obstacles with retry mechanism for navigation
                    # validation
                    if new_variant_data.planned_path and config["amount"] > 0:
                        max_attempts = 10  # Maximum number of retry attempts
                        attempt = 0
                        navigable_variant_found = False

                        while (
                            attempt < max_attempts
                            and not navigable_variant_found
                            and not self.should_stop
                        ):
                            attempt += 1

                            # Reset variant for this attempt
                            attempt_variant_data = copy.deepcopy(variant_data)

                            self.status_update.emit(
                                f"Placing obstacles for variant {
                                    current_variant}/{total_variants} (attempt {attempt}/{max_attempts})"
                            )

                            # Create obstacle placer without setting seed (use
                            # global random state)
                            placer = ObstaclePlacer()
                            placer.status_update.connect(
                                self.status_update.emit)

                            waypoints = [
                                attempt_variant_data.variant.start_pose]
                            waypoints.extend(
                                attempt_variant_data.variant.goal_poses)

                            obstacle_objects = placer.place_obstacles(
                                attempt_variant_data.planned_path,
                                config["max_distance"],
                                config["amount"],
                                config["model"],
                                config.get("xacro_arguments", ""),
                                robot_diameter=self.robot_diameter,  # Use global robot diameter
                                waypoints=waypoints,
                            )

                            # Add static objects to variant
                            attempt_variant_data.variant.static_objects.extend(
                                obstacle_objects
                            )

                            # Validate navigation with the placed obstacles
                            if attempt_variant_data.variant.map_file:
                                map_path = get_variant_map_path(self.maps_dir,
                                    attempt_variant_data)
                                if os.path.exists(map_path):
                                    try:
                                        generator = PathGenerator(
                                            map_path, self.robot_diameter
                                        )  # Use global robot diameter

                                        # Check if navigation is still possible
                                        # from start to any goal
                                        nav_waypoints = [
                                            attempt_variant_data.variant.start_pose
                                        ] + attempt_variant_data.variant.goal_poses
                                        path = generator.generate_path(
                                            nav_waypoints,
                                            attempt_variant_data.variant.static_objects,
                                        )

                                        if path:
                                            # Success! Navigation is still
                                            # possible
                                            attempt_variant_data.planned_path = path
                                            new_variant_data = attempt_variant_data
                                            navigable_variant_found = True
                                            self.status_update.emit(
                                                f"Successfully placed {config['amount']} obstacles for variant {
                                                    current_variant}/{total_variants} (attempt {attempt})"
                                            )
                                        else:
                                            self.status_update.emit(
                                                f"Attempt {
                                                    attempt}/{max_attempts}: obstacles block navigation, retrying..."
                                            )

                                    except Exception as e:
                                        self.status_update.emit(
                                            f"Attempt {
                                                attempt}/{max_attempts}: validation error: {str(e)}, retrying..."
                                        )
                                else:
                                    print(f"Warning: Map file not found: {map_path}")

                        # If we couldn't find a navigable configuration after
                        # all attempts
                        if not navigable_variant_found:
                            if config["amount"] > 0:
                                self.status_update.emit(
                                    f"Warning: Could not place {config['amount']} obstacles for variant {
                                        current_variant}/{total_variants} while maintaining navigation - using variant without obstacles"
                                )
                            # Use the original variant without obstacles
                            new_variant_data = copy.deepcopy(variant_data)

                    else:
                        # No obstacles to place (amount = 0 or no planned
                        # path), just validate existing navigation
                        if new_variant_data.variant.map_file:
                            map_path = get_variant_map_path(self.maps_dir, new_variant_data)
                            if os.path.exists(map_path):
                                try:
                                    generator = PathGenerator(
                                        map_path, self.robot_diameter
                                    )  # Use global robot diameter

                                    # Check if navigation is still possible
                                    # from start to any goal
                                    waypoints = [
                                        new_variant_data.variant.start_pose
                                    ] + new_variant_data.variant.goal_poses
                                    path = generator.generate_path(
                                        waypoints,
                                        new_variant_data.variant.static_objects,
                                    )

                                    if path:
                                        # Set the valid path in the variant
                                        # data
                                        new_variant_data.planned_path = path
                                    else:
                                        self.status_update.emit(
                                            f"Warning: variant {
                                                current_variant}/{total_variants} - no valid navigation path even without obstacles"
                                        )
                                        new_variant_data.planned_path = []

                                except Exception as e:
                                    self.status_update.emit(
                                        f"Warning: Could not validate navigation for variant {
                                            current_variant}/{total_variants}: {str(e)}"
                                    )
                                    # Continue with variant despite validation
                                    # error
                                    new_variant_data.planned_path = []
                            else:
                                print(f"File {map_path} does not exist.")

                    # Update variant name to include obstacle info
                    short_model_name = os.path.basename(config["model"]).replace(
                        ".sdf.xacro", ""
                    )
                    new_variant_data.name = (
                        f"{variant_data.name}-o{config['amount']
                                                }-{short_model_name}"
                    )

                    self.variant_generated.emit(new_variant_data)

        except Exception as e:
            self.status_update.emit(
                f"Error during obstacle placement: {
                    str(e)}")
        finally:
            self.generation_finished.emit()


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
        self.robot_diameter = (
            0.354  # Default value, will be updated from global settings
        )

        # Track previous configurations to detect changes
        self._prev_configs = []
        self.maps_dir = None

        self.setup_ui()

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

        # Load saved configurations or add defaults
        self.load_configurations()

    def load_configurations(self):
        """Load configurations from variation_data or use defaults if none exist."""
        saved_configs = []
        if self.variation_data and self.variation_data.obstacle_placement_settings:
            saved_configs = (
                self.variation_data.obstacle_placement_settings.obstacle_configs or []
            )
        if saved_configs:
            # Load saved configurations from variation_data
            for config in saved_configs:
                self.add_obstacle_config_row(config, save=False)
        else:
            # Use default configurations if none saved
            self.add_default_configs()

        # Initialize previous configs tracking and update count
        self._prev_configs = self.get_obstacle_configs().copy()
        self.update_variant_count()

    def set_robot_diameter(self, diameter):
        """Set the robot diameter from global parameter."""
        self.robot_diameter = diameter

    def save_configurations(self):
        """Save current obstacle configurations to QSettings."""
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

        # Save the new configuration to variation_data
        if self.variation_data and self.variation_data.obstacle_placement_settings:
            self.variation_data.obstacle_placement_settings.obstacle_configs = (
                configs.copy()
            )
        self._prev_configs = configs.copy()

    def revert_to_previous_configs(self):
        """Revert the table to the previous configuration."""
        # Restore previous configurations
        self.config_table.setRowCount(0)
        for config in self._prev_configs:
            self.add_obstacle_config_row(config, save=False)
        self.update_variant_count()

    def set_variation_data(self, variation_data):
        """Override to load parameters from VariationData after it's set."""
        super().set_variation_data(variation_data)
        self._load_parameters_from_variation_data()

    def _load_parameters_from_variation_data(self):
        """Load obstacle placement parameters from VariationData into UI widgets."""
        if (
            not self.variation_data
            or not self.variation_data.obstacle_placement_settings
        ):
            return

        settings = self.variation_data.obstacle_placement_settings

        # Load obstacle placement seed
        if settings.obstacle_placement_seed is not None:
            self.obstacle_placement_seed_spin.setValue(
                settings.obstacle_placement_seed)

        # Load obstacle configurations
        if settings.obstacle_configs:
            # Clear current table
            self.config_table.setRowCount(0)

            # Load configurations from VariationData
            for config in settings.obstacle_configs:
                self.add_obstacle_config_row(config, save=False)

            # Initialize previous configs tracking
            self._prev_configs = settings.obstacle_configs.copy()
        else:
            # Load default configurations if none in VariationData
            self.load_configurations()

    def clear_generated_variants(self):
        """Clear all generated variants."""
        self.generated_variants.clear()
        self.output_variants.clear()
        self.variants_list.clear()
        self.emit_variant_count_update(0)
        self.completeChanged.emit()

    def on_seed_changed(self, value):
        """Handle changes to the obstacle placement seed spinbox."""
        if self.variation_data and self.variation_data.obstacle_placement_settings:
            self.variation_data.obstacle_placement_settings.obstacle_placement_seed = (
                value
            )

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

        return configs

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

    def display_variant_in_map(self, variant_data):
        """Display a variant in the map widget."""
        if variant_data and variant_data.variant and variant_data.variant.map_file:
            map_path = get_variant_map_path(self.maps_dir, variant_data)
            # Load map
            if os.path.exists(map_path):
                self.map_widget.load_map(map_path)

                # Show path
                if variant_data.planned_path:
                    self.map_widget.set_path(variant_data.planned_path)
                else:
                    self.map_widget.set_path([])

                # Show start and goal poses
                if variant_data.variant.start_pose:
                    self.map_widget.start_pose = variant_data.variant.start_pose

                if variant_data.variant.goal_poses:
                    self.map_widget.goal_poses = variant_data.variant.goal_poses

                # Always update static objects (obstacles) - ensure they're
                # cleared if none exist
                self.map_widget.static_objects = (
                    variant_data.variant.static_objects or []
                )

                # Generate and show costmap with obstacles
                try:
                    generator = PathGenerator(
                        map_path, self.robot_diameter
                    )  # Use global robot diameter
                    costmap = generator.get_costmap_with_obstacles(
                        variant_data.variant.static_objects
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

        # Use the current seed value from the GUI
        current_seed = self.obstacle_placement_seed_spin.value()
        self.variation_data.obstacle_placement_settings.obstacle_placement_seed = (
            current_seed
        )

        # Start generation in background thread
        self.generation_thread = ObstaclePlacementThread(
            self.input_variants,
            obstacle_configs,
            self.variation_data.obstacle_placement_settings.obstacle_placement_seed,
            self.robot_diameter,  # Pass global robot diameter
            self.maps_dir,
        )

        self.generation_thread.variant_generated.connect(
            self.on_variant_generated)
        self.generation_thread.generation_finished.connect(
            self.on_generation_finished)
        self.generation_thread.status_update.connect(self.emit_status_message)
        self.generation_thread.start()

    def stop_generation(self):
        """Stop the obstacle placement generation."""
        if self.generation_thread and self.generation_thread.isRunning():
            self.generation_thread.stop()
            self.generation_thread.wait()

    def on_variant_generated(self, variant_data):
        """Handle a newly generated variant."""
        self.generated_variants.append(variant_data)

        # Add to list widget
        item = QListWidgetItem(variant_data.name)
        item.setData(Qt.UserRole, variant_data)
        self.variants_list.addItem(item)

        # Update output_variants for base class
        self.output_variants = self.generated_variants.copy()

        # Update count
        self.emit_variant_count_update(len(self.generated_variants))

    def on_generation_finished(self):
        """Handle generation completion."""
        self.progress_bar.setVisible(False)

        # Re-enable controls
        self.generate_button.setEnabled(True)
        self.config_table.setEnabled(True)
        self.add_config_button.setEnabled(True)
        self.remove_config_button.setEnabled(True)
        self.stop_button.setEnabled(False)

        # Update status
        count = len(self.generated_variants)
        self.emit_status_message(
            f"Obstacle placement complete - {count} variants generated"
        )

        # Update previous configs to current state after successful generation
        self._prev_configs = self.get_obstacle_configs().copy()

        # Update output_variants for base class
        self.output_variants = self.generated_variants.copy()

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
            variant_data = current.data(Qt.UserRole)
            if variant_data:
                self.display_variant_in_map(variant_data)

    def get_generated_variants(self):
        """Get the list of generated variants."""
        return self.generated_variants

    def isComplete(self):
        """Check if the page is complete."""
        # Page is complete if we have generated variants OR if skipping
        # obstacles with input variants
        return len(self.generated_variants) > 0 or (
            self.skip_obstacles_checkbox.isChecked() and len(self.input_variants) > 0
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
            # If skipping obstacles, use input variants as generated variants
            self.generated_variants = self.input_variants.copy()
            self.output_variants = self.generated_variants.copy()
            self.variants_list.clear()

            # Add all input variants to the list
            for variant_data in self.input_variants:
                item = QListWidgetItem(variant_data.name)
                item.setData(Qt.UserRole, variant_data)
                self.variants_list.addItem(item)

            # Select first variant if available
            if self.variants_list.count() > 0:
                self.variants_list.setCurrentRow(0)

            # Update status
            self.emit_status_message(
                f"Skipping obstacle placement - using {
                    len(self.generated_variants)} variants from path generation"
            )
            self.emit_variant_count_update(len(self.generated_variants))
        else:
            # Clear generated variants when not skipping
            self.generated_variants = []
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
