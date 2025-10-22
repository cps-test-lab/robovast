#!/usr/bin/env python3
"""
Path generation page and thread for the Variant Creation Wizard.
"""
import math  # Added for path length calculation
import os
from functools import partial

from PySide2.QtCore import Qt, Signal
from PySide2.QtWidgets import QDoubleSpinBox  # Added QSizePolicy
from PySide2.QtWidgets import (QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem,
                               QMessageBox, QProgressBar, QPushButton,
                               QSizePolicy, QSpinBox, QSplitter, QVBoxLayout,
                               QWidget)
from robovast_common import PathVariation
from robovast_common.navigation import PathGenerator
from variant_editor.map_widget import MapWidget
from variant_editor.wizard_base_page import WizardBasePage

from .variation_thread import VariationThread


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
        self.path_generator = None
        self.current_robot_diameter = 0.35  # Default, will be updated from global settings

        self._prev_parameters = {}
        self.setup_ui()
        # Initialize previous parameter values
        self.init_parameters()

    def set_input_variants(self, variants):
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

    def init_parameters(self):
        """Initialize parameter tracking."""
        # Initialize previous parameter tracking
        for sb in [
            self.path_length_spin,
            self.num_paths_spin,
            self.path_length_tolerance_spin,
            self.min_distance_spin,
            self.path_generation_seed_spin,
        ]:
            self._prev_parameters[sb] = sb.value()

        # Apply robot diameter to map widget
        self.map_widget.set_robot_diameter(self.current_robot_diameter)

    def set_global_robot_diameter(self, diameter):
        """Set the robot diameter from the global setting."""
        self.current_robot_diameter = diameter
        self.robot_diameter_display.setText(f"{diameter:.2f} m")
        self.map_widget.set_robot_diameter(diameter)
        # Trigger regeneration if parameters have been modified
        self.mark_parameters_changed()

    def mark_parameters_changed(self):
        """Mark that parameters have changed and may need regeneration."""

    def apply_parameters(self, parameters):
        """Apply parameters from a dictionary to the UI widgets."""
        super().apply_parameters(parameters)

        if 'path_length' in parameters:
            self.path_length_spin.setValue(parameters['path_length'])
            self._prev_parameters[self.path_length_spin] = parameters['path_length']

        if 'num_paths' in parameters:
            self.num_paths_spin.setValue(parameters['num_paths'])
            self._prev_parameters[self.num_paths_spin] = parameters['num_paths']

        if 'path_length_tolerance' in parameters:
            self.path_length_tolerance_spin.setValue(parameters['path_length_tolerance'])
            self._prev_parameters[self.path_length_tolerance_spin] = parameters['path_length_tolerance']

        if 'min_distance' in parameters:
            self.min_distance_spin.setValue(parameters['min_distance'])
            self._prev_parameters[self.min_distance_spin] = parameters['min_distance']

        if 'path_generation_seed' in parameters:
            self.path_generation_seed_spin.setValue(parameters['path_generation_seed'])
            self._prev_parameters[self.path_generation_seed_spin] = parameters['path_generation_seed']

    def apply_global_parameters(self, parameters):
        """Apply global parameters like robot diameter."""
        super().apply_global_parameters(parameters)

        if 'robot_diameter' in parameters:
            diameter = parameters['robot_diameter']
            self.current_robot_diameter = diameter
            self.robot_diameter_display.setText(f"{diameter:.2f} m")
            self.map_widget.set_robot_diameter(diameter)

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
        self.output_variants = []

        # Build settings dict for PathVariation
        settings = {
            'general': self.global_parameters,
            'PathVariation': self.parameters
        }

        # Start generation in background thread
        self.generation_thread = VariationThread(
            PathVariation,
            settings
        )
        self.generation_thread.set_input_variants(self.input_variants)

        self.generation_thread.generation_failed.connect(
            self.on_generation_failed)
        self.generation_thread.generation_complete.connect(
            self.on_generation_completed)
        self.generation_thread.progress_update.connect(self.on_status_update)
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

    def on_generation_failed(self, error_message):
        """Handle generation failure."""
        QMessageBox.critical(self, "Generation Failed", error_message)

    # TODO live update
        # item.setData(Qt.UserRole, variant_data)
        # self.variants_list.addItem(item)
        # # Auto-visualize variant
        # self.variants_list.setCurrentItem(item)

        # # Update progress (show current count)
        # current_count = len(self.generated_variants)
        # total_count = len(self.input_variants) * self.num_paths_spin.value()
        # self.progress_bar.setRange(0, total_count)
        # self.progress_bar.setValue(current_count)

        # # Update the base class variant system
        # self.apply_variant_adaptations()

    def on_generation_completed(self, variants):
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

        # Update previous parameters to current state after successful generation
        for sb in [
            self.path_length_spin,
            self.num_paths_spin,
            self.path_length_tolerance_spin,
            self.min_distance_spin,
            self.path_generation_seed_spin,
        ]:
            self._prev_parameters[sb] = sb.value()

        # Populate the variants list
        for variant in variants:
            item = QListWidgetItem(variant['name'])
            item.setData(Qt.UserRole, variant)
            self.variants_list.addItem(item)

        self.set_output_variants(variants)
        # Update wizard completion state
        self.completeChanged.emit()
        # Emit signal to clear status message
        self.emit_status_message("")

    def on_variant_selected(self, current_item, previous_item):
        """Handle variant selection to show map and path."""
        if not current_item:
            return

        variant = current_item.data(Qt.UserRole)
        if not variant:
            return

        # Load the map and show the path
        try:
            # Load map in map widget
            map_path = os.path.join(variant["floorplan_variant_path"], variant["map_file"])

            self.map_widget.load_map(map_path)

            # Create path generator for this map
            path_generator = PathGenerator(
                map_path, self.current_robot_diameter
            )  # Use global robot diameter

            # Set waypoints
            waypoints = [variant.get("start_pose")] + variant.get("goal_poses", [])
            self.map_widget.start_pose = variant.get("start_pose")
            self.map_widget.goal_poses = variant.get("goal_poses", [])
            self.map_widget.static_objects = variant.get("static_objects", [])

            # Set the path
            if "path" in variant:
                self.map_widget.set_path(variant["path"])
            else:
                self.map_widget.set_path([])

            # Generate and set costmap
            try:
                costmap = path_generator.get_costmap_with_obstacles(
                    variant.get("static_objects", [])
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

    def on_parameter_changed(self, spinbox, apply_fn, new_value):
        """Handle changes to any parameter spinbox, confirming clearing of variants."""
        if self.output_variants:
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
            self.output_variants.clear()
            self.variants_list.clear()
            self.map_widget.clear()
            self.map_widget.load_map(None)
            # Clear the base class variants as well
            self.set_output_variants([])
        # Accept new value
        self._prev_parameters[spinbox] = new_value

        # Update self.parameters based on which spinbox changed
        if spinbox == self.path_length_spin:
            self.parameters['path_length'] = new_value
        elif spinbox == self.num_paths_spin:
            self.parameters['num_paths'] = new_value
        elif spinbox == self.path_length_tolerance_spin:
            self.parameters['path_length_tolerance'] = new_value
        elif spinbox == self.min_distance_spin:
            self.parameters['min_distance'] = new_value
        elif spinbox == self.path_generation_seed_spin:
            self.parameters['path_generation_seed'] = new_value

        # Apply function if provided
        if apply_fn:
            apply_fn(new_value)

    def get_generated_variants(self):
        """Return list of generated variants."""
        return self.output_variants
