#!/usr/bin/env python3
"""
Path generation page and thread for the Variant Creation Wizard.
"""
import os
import math  # Added for path length calculation
from copy import deepcopy
from functools import partial

from PySide2.QtCore import Qt, QThread, Signal
from PySide2.QtWidgets import QDoubleSpinBox  # Added QSizePolicy
from PySide2.QtWidgets import (QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem,
                               QMessageBox, QProgressBar, QPushButton,
                               QSizePolicy, QSpinBox, QSplitter, QVBoxLayout,
                               QWidget)
from variant_editor.common import get_variant_map_path
from variant_editor.map_widget import MapWidget
from variant_editor.path_generator import PathGenerator
from variant_editor.waypoint_generator import WaypointGenerator
from variant_editor.wizard_base_page import WizardBasePage


class PathGenerationPage(WizardBasePage):
    """Second page: Configure path generation parameters and generate paths."""

    variant_count_changed = Signal()  # Signal for when variant count changes

    def __init__(self, scenario_variants_path):
        super().__init__()

        self.setTitle("Path Generation")
        self.setSubTitle(
            "Configure path generation parameters and generate navigation variants."
        )

        self.scenario_variants_path = scenario_variants_path
        self.generated_variants = []
        self.path_generator = None

        self._prev_parameters = {}
        self.maps_dir = None
        self.setup_ui()
        # Initialize previous parameter values and load saved parameters
        self.load_parameters()

    def set_maps_dir(self, maps_dir: str):
        self.maps_dir = maps_dir

    def set_input_variants(self, variants):
        from pprint import pprint
        pprint(variants)
        """Override to update UI when input variants change."""
        if self.input_variants != variants:
            self.output_variants = []  # Reset output variants when input changes
            self.variants_list.clear()  # Clear previous variants in the list
        super().set_input_variants(variants)
        self.on_input_variants_changed()

    def on_input_variants_changed(self):
        self.selected_maps_label.setText(
            f"{len(self.input_variants)} input variants")
        # Enable generate button if we have input variants
        if hasattr(self, "generate_button"):
            self.generate_button.setEnabled(len(self.input_variants) > 0)

    def setup_ui(self):
        self.map_widget = MapWidget(waypoint_movement_enabled=False)
        # Set map_widget to expand maximally
        self.map_widget.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Expanding)

        # Set up the user interface
        main_layout = QHBoxLayout()

        # Left panel - show count of selected maps
        left_panel = QVBoxLayout()
        self.selected_maps_label = QLabel("0 input variants")
        left_panel.addWidget(self.selected_maps_label)

        # Path generation parameters
        parameters_group = QGroupBox("Path Generation Parameters")
        parameters_layout = QFormLayout()

        # Overall Path Length
        self.path_length_spin = QDoubleSpinBox()
        self.path_length_spin.setRange(1.0, 100.0)
        self.path_length_spin.setValue(10.0)
        self.path_length_spin.setSuffix(" m")
        self.path_length_spin.valueChanged.connect(
            partial(self.on_parameter_changed, self.path_length_spin, None)
        )
        parameters_layout.addRow("Overall Path Length:", self.path_length_spin)

        # Number of Paths
        self.num_paths_spin = QSpinBox()
        self.num_paths_spin.setRange(1, 100)
        self.num_paths_spin.setValue(1)
        self.num_paths_spin.valueChanged.connect(
            partial(self.on_parameter_changed, self.num_paths_spin, None)
        )
        parameters_layout.addRow("Number of Paths:", self.num_paths_spin)

        # Robot Diameter - now set globally (display only)
        self.robot_diameter_display = QLineEdit("0.50 m")
        self.robot_diameter_display.setReadOnly(True)
        self.robot_diameter_display.setToolTip(
            "Robot diameter is set globally at the wizard level"
        )
        parameters_layout.addRow(
            "Robot Diameter (Global):", self.robot_diameter_display
        )

        # Path Length Tolerance
        self.path_length_tolerance_spin = QDoubleSpinBox()
        self.path_length_tolerance_spin.setRange(0.0, 10.0)
        self.path_length_tolerance_spin.setValue(0.5)
        self.path_length_tolerance_spin.setSuffix(" m")
        self.path_length_tolerance_spin.valueChanged.connect(
            partial(
                self.on_parameter_changed,
                self.path_length_tolerance_spin,
                None)
        )
        parameters_layout.addRow(
            "Path Length Tolerance:", self.path_length_tolerance_spin
        )

        # Min Waypoint Distance
        self.min_distance_spin = QDoubleSpinBox()
        self.min_distance_spin.setRange(0.0, 50.0)
        self.min_distance_spin.setValue(1.0)
        self.min_distance_spin.setSuffix(" m")
        self.min_distance_spin.valueChanged.connect(
            partial(self.on_parameter_changed, self.min_distance_spin, None)
        )
        parameters_layout.addRow(
            "Min Waypoint Distance:",
            self.min_distance_spin)

        # Path Generation Seed
        self.path_generation_seed_spin = QSpinBox()
        self.path_generation_seed_spin.setRange(0, 2147483647)  # 2^31-1
        self.path_generation_seed_spin.setValue(42)  # Default seed
        self.path_generation_seed_spin.valueChanged.connect(
            partial(
                self.on_parameter_changed,
                self.path_generation_seed_spin,
                None)
        )
        parameters_layout.addRow(
            "Path Generation Seed:", self.path_generation_seed_spin
        )

        parameters_group.setLayout(parameters_layout)
        left_panel.addWidget(parameters_group)

        # Generate button
        self.generate_button = QPushButton("Generate Paths")
        self.generate_button.clicked.connect(self.start_generation)
        self.generate_button.setEnabled(False)
        left_panel.addWidget(self.generate_button)

        # Added stop button to abort generation
        self.stop_button = QPushButton("Stop Generation")
        self.stop_button.clicked.connect(self.stop_generation)
        self.stop_button.setEnabled(False)
        left_panel.addWidget(self.stop_button)

        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        left_panel.addWidget(self.progress_bar)

        left_panel.addStretch()

        # Middle panel - Map visualization
        middle_panel = QVBoxLayout()
        map_group = QGroupBox("Path Visualization")
        map_group.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        map_layout = QVBoxLayout()
        self.map_widget.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Expanding)
        map_layout.addWidget(self.map_widget)
        map_group.setLayout(map_layout)
        middle_panel.addWidget(map_group)

        # Right panel - Generated variants
        right_panel = QVBoxLayout()
        variants_group = QGroupBox("Variants")
        variants_group.setSizePolicy(
            QSizePolicy.Expanding,
            QSizePolicy.Expanding)
        variants_layout = QVBoxLayout()
        self.variants_list = QListWidget()
        self.variants_list.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.variants_list.currentItemChanged.connect(self.on_variant_selected)
        # Set variants_list to expand maximally
        self.variants_list.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Expanding)
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

        splitter.setSizes([200, 600, 200])
        splitter.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        main_layout.addWidget(splitter)
        self.setLayout(main_layout)
        main_layout.setStretch(0, 1)

    def load_parameters(self):
        """Load parameters from variation_data or use defaults if none exist."""
        # Load values from variation_data if available, otherwise use defaults
        if self.variation_data and self.variation_data.path_generation_settings:
            settings = self.variation_data.path_generation_settings
            path_length = settings.path_length
            num_paths = settings.num_paths
            path_length_tolerance = settings.path_length_tolerance
            min_distance = settings.min_distance
            path_generation_seed = (
                settings.path_generation_seed or 42
            )  # Default to 42 if None
        else:
            # Use default values if variation_data is not available
            path_length = 10.0
            num_paths = 1
            path_length_tolerance = 0.5
            min_distance = 1.0
            path_generation_seed = 42

        # Get robot diameter from global parameters
        robot_diameter = 10  # Default fallback
        if self.variation_data and self.variation_data.general_parameters:
            robot_diameter = self.variation_data.general_parameters.robot_diameter

        # Apply loaded values to spinboxes and display
        self.path_length_spin.setValue(path_length)
        self.num_paths_spin.setValue(num_paths)
        self.robot_diameter_display.setText(f"{robot_diameter:.2f} m")
        self.path_length_tolerance_spin.setValue(path_length_tolerance)
        self.min_distance_spin.setValue(min_distance)
        self.path_generation_seed_spin.setValue(path_generation_seed)

        # Initialize previous parameter tracking (excluding robot diameter
        # which is now global)
        for sb in [
            self.path_length_spin,
            self.num_paths_spin,
            self.path_length_tolerance_spin,
            self.min_distance_spin,
            self.path_generation_seed_spin,
        ]:
            self._prev_parameters[sb] = sb.value()

        # Apply robot diameter to map widget
        self.map_widget.set_robot_diameter(robot_diameter)

        # Store current robot diameter for internal use
        self.current_robot_diameter = robot_diameter

    def set_global_robot_diameter(self, diameter):
        """Set the robot diameter from the global setting."""
        self.current_robot_diameter = diameter
        self.robot_diameter_display.setText(f"{diameter:.2f} m")
        self.map_widget.set_robot_diameter(diameter)
        # Trigger regeneration if parameters have been modified
        self.mark_parameters_changed()

    def save_parameters(self):
        """Save current parameters to variation_data."""
        if self.variation_data and self.variation_data.path_generation_settings:
            settings = self.variation_data.path_generation_settings
            settings.path_length = self.path_length_spin.value()
            settings.num_paths = self.num_paths_spin.value()
            # Robot diameter is now stored in general_parameters, not here
            settings.path_length_tolerance = self.path_length_tolerance_spin.value()
            settings.min_distance = self.min_distance_spin.value()
            settings.path_generation_seed = self.path_generation_seed_spin.value()

    def mark_parameters_changed(self):
        """Mark that parameters have changed and may need regeneration."""

    def start_generation(self):
        """Start the path generation process."""
        if not self.input_variants:
            QMessageBox.warning(
                self, "No Variants", "No input variants available for path generation."
            )
            return

        # Disable generate button and show progress
        self.generate_button.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)  # Indeterminate progress

        # Clear previous results
        self.variants_list.clear()
        self.generated_variants = []

        # Use the seed from the UI
        seed_value = self.path_generation_seed_spin.value()

        # Start generation in background thread
        self.generation_thread = PathGenerationThread(
            self.input_variants,
            self.path_length_spin.value(),
            self.num_paths_spin.value(),
            self.scenario_variants_path,
            self.maps_dir,
            self.current_robot_diameter,  # Use global robot diameter
            self.path_length_tolerance_spin.value(),
            self.min_distance_spin.value(),
            seed_value,
        )

        self.generation_thread.variant_generated.connect(
            self.on_variant_generated)
        self.generation_thread.generation_finished.connect(
            self.on_generation_finished)
        self.generation_thread.status_update.connect(self.on_status_update)
        self.generation_thread.start()
        self.stop_button.setEnabled(True)
        # disable parameter fields during generation
        self.path_length_spin.setEnabled(False)
        self.num_paths_spin.setEnabled(False)
        self.path_length_tolerance_spin.setEnabled(False)
        self.min_distance_spin.setEnabled(False)
        self.path_generation_seed_spin.setEnabled(False)

    def on_status_update(self, status_message):
        """Handle status updates during generation."""
        # Emit generic signal for status message update
        self.emit_status_message(status_message)

    def on_variant_generated(self, variant_data):
        """Handle a new variant being generated."""
        self.generated_variants.append(variant_data)

        # Add to list
        item = QListWidgetItem(variant_data.name)
        item.setData(Qt.UserRole, variant_data)
        self.variants_list.addItem(item)
        # Auto-visualize variant
        self.variants_list.setCurrentItem(item)

        # Update progress (show current count)
        current_count = len(self.generated_variants)
        total_count = len(self.input_variants) * self.num_paths_spin.value()
        self.progress_bar.setRange(0, total_count)
        self.progress_bar.setValue(current_count)

        # Update the base class variant system
        self.apply_variant_adaptations()

    def on_generation_finished(self):
        """Handle generation completion."""
        self.progress_bar.setVisible(False)
        self.generate_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        # re-enable parameter fields after generation
        self.path_length_spin.setEnabled(True)
        self.num_paths_spin.setEnabled(True)
        self.path_length_tolerance_spin.setEnabled(True)
        self.min_distance_spin.setEnabled(True)
        self.path_generation_seed_spin.setEnabled(True)

        # Update previous parameters to current state after successful
        # generation
        for sb in [
            self.path_length_spin,
            self.num_paths_spin,
            self.path_length_tolerance_spin,
            self.min_distance_spin,
            self.path_generation_seed_spin,
        ]:
            self._prev_parameters[sb] = sb.value()

        # Update wizard completion state
        self.completeChanged.emit()
        # Emit signal to clear status message
        self.emit_status_message("")

        # Final update to base class variant system
        self.apply_variant_adaptations()

    def on_variant_selected(self, current_item, previous_item):
        """Handle variant selection to show map and path."""
        if not current_item:
            return

        variant_data = current_item.data(Qt.UserRole)
        if not variant_data:
            return

        # Load the map and show the path
        try:
            variant = variant_data.variant
            variant_file_path = os.path.join(self.maps_dir, variant_data.floorplan_variant_name)
            # Load map in map widget
            self.map_widget.load_map(get_variant_map_path(self.maps_dir, variant_data))

            # Create path generator for this map
            path_generator = PathGenerator(
                get_variant_map_path(self.maps_dir, variant_data), self.current_robot_diameter
            )  # Use global robot diameter

            # Set waypoints
            waypoints = [variant.start_pose] + variant.goal_poses
            self.map_widget.start_pose = variant.start_pose
            self.map_widget.goal_poses = variant.goal_poses
            self.map_widget.static_objects = variant.static_objects or []

            # Generate updated path considering any static objects as
            # obstacles
            updated_path = path_generator.generate_path(
                waypoints, variant.static_objects
            )
            if updated_path:
                # Update the variant data with the new path
                variant_data.planned_path = updated_path
                self.map_widget.set_path(updated_path)
            else:
                # No valid path found
                self.map_widget.set_path([])

            # Generate and set costmap
            try:
                costmap = path_generator.get_costmap_with_obstacles(
                    variant.static_objects
                )
                if costmap is not None:
                    # Always show costmap overlay when available
                    self.map_widget.set_costmap(costmap, show_overlay=True)
            except Exception as e:
                print(f"Error generating costmap: {e}")

            # Update display
            self.map_widget.update()

        except Exception as e:
            print(f"Error loading variant visualization: {e}")

    def get_generated_variants(self):
        """Get the list of generated variants."""
        return self.generated_variants

    def isComplete(self):
        """Check if this page is complete."""
        return len(self.generated_variants) > 0

    def stop_generation(self):
        """Stop the ongoing path generation."""
        if hasattr(
                self, "generation_thread") and self.generation_thread.isRunning():
            self.generation_thread.requestInterruption()
            self.stop_button.setEnabled(False)
            self.generate_button.setEnabled(True)
            # re-enable parameter fields when stopped
            self.path_length_spin.setEnabled(True)
            self.num_paths_spin.setEnabled(True)
            self.path_length_tolerance_spin.setEnabled(True)
            self.min_distance_spin.setEnabled(True)
            self.path_generation_seed_spin.setEnabled(True)

    def apply_variant_adaptations(self):
        """Apply variant adaptations and update output_variants."""
        # Update the base class output_variants with generated_variants
        self.set_output_variants(self.generated_variants)

    def on_parameter_changed(self, spinbox, apply_fn, new_value):
        """Handle changes to any parameter spinbox, confirming clearing of variants."""
        if self.generated_variants:
            res = QMessageBox.question(
                self,
                "Clear Variants?",
                "Changing parameters will remove previously generated variants. Continue?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if res == QMessageBox.No:
                # Revert
                spinbox.blockSignals(True)
                spinbox.setValue(self._prev_parameters[spinbox])
                spinbox.blockSignals(False)
                return
            # Clear previous results
            self.generated_variants.clear()
            self.variants_list.clear()
            self.map_widget.clear()
            self.map_widget.load_map(None)
            # Clear the base class variants as well
            self.set_output_variants([])
        # Accept new value
        self._prev_parameters[spinbox] = new_value
        # Apply function if provided
        if apply_fn:
            apply_fn(new_value)
        # Save parameters after accepting changes
        self.save_parameters()

    def set_variation_data(self, variation_data):
        """Override to reload parameters when variation_data is set."""
        super().set_variation_data(variation_data)
        # Reload parameters now that we have variation_data
        if hasattr(self, "path_length_spin"):  # Check if UI is already set up
            self.load_parameters()


class PathGenerationThread(QThread):
    """Background thread for generating navigation paths."""

    variant_generated = Signal(object)  # VariantData
    generation_finished = Signal()
    status_update = Signal(str)  # Status message

    def __init__(
        self,
        input_variants,
        path_length,
        num_paths,
        base_path,
        maps_dir,
        robot_diameter,
        length_tolerance,
        min_distance,
        path_generation_seed,
    ):
        super().__init__()
        self.input_variants = input_variants
        self.path_length = path_length
        self.num_paths = num_paths
        self.base_path = base_path
        self.maps_dir = maps_dir
        self.robot_diameter = robot_diameter  # New field
        self.length_tolerance = length_tolerance  # New field
        self.min_distance = min_distance  # New field
        self.path_generation_seed = path_generation_seed  # Store the seed

    def run(self):
        """Run the path generation process."""
        try:
            variant_counter = 0

            for variant_data in self.input_variants:
                if self.isInterruptionRequested():
                    break

                # Extract map information from the variant data
                map_path = get_variant_map_path(self.maps_dir, variant_data)
                # Extract map name from the file path
                waypoint_generator = WaypointGenerator(
                    map_file_path=map_path, seed=self.path_generation_seed
                )

                # Create path generator for this map
                path_generator = PathGenerator(map_path, self.robot_diameter)

                # Generate paths for this map
                for path_index in range(self.num_paths):
                    if self.isInterruptionRequested():
                        break
                    variant_name = f"{variant_data.name}-p{path_index + 1}"

                    max_attempts = 1000  # Maximum attempts to find a valid path
                    attempt = 0
                    path_found = False

                    while attempt < max_attempts and not path_found:
                        if self.isInterruptionRequested():
                            break

                        attempt += 1
                        self.status_update.emit(
                            f"Generating {variant_name} - Attempt {attempt}/{max_attempts}"
                        )

                        # Use user-defined robot diameter
                        waypoints = waypoint_generator.generate_waypoints(
                            num_waypoints=2,  # Generate 2 waypoints beyond start
                            robot_diameter=self.robot_diameter,
                            min_distance=self.min_distance,  # Minimum distance between waypoints
                        )
                        start_pose = waypoints[0] if waypoints else None
                        goal_poses = waypoints[1:] if len(
                            waypoints) > 1 else []

                        if len(waypoints) < 2:
                            continue

                        if start_pose and goal_poses:
                            # Generate path considering any existing static
                            # objects
                            static_objects = variant_data.variant.static_objects or []
                            path = path_generator.generate_path(
                                waypoints, static_objects
                            )

                            if not path:
                                continue

                            # Enforce path length tolerance
                            length = sum(
                                math.hypot(
                                    path[i].x - path[i - 1].x, path[i].y -
                                    path[i - 1].y
                                )
                                for i in range(1, len(path))
                            )
                            if abs(
                                    length - self.path_length) > self.length_tolerance:
                                continue

                            # Path found and valid
                            path_found = True

                    if not path_found:
                        self.status_update.emit(
                            f"Failed to generate {variant_name} after {
                                max_attempts} attempts"
                        )
                        continue
                    updated_variant_data = deepcopy(variant_data)
                    updated_variant_data.name = variant_name
                    updated_variant_data.planned_path = path
                    updated_variant_data.variant.start_pose = start_pose
                    updated_variant_data.variant.goal_poses = goal_poses
                    # Seed was already set earlier in the loop
                    # Emit signal
                    self.variant_generated.emit(updated_variant_data)

                    variant_counter += 1

                    # Small delay to allow UI updates
                    self.msleep(50)

        except Exception as e:
            print(f"Error in path generation: {e}")

        self.generation_finished.emit()
