#!/usr/bin/env python3
"""
Sensor noise configuration page for the Variant Creation Wizard.
"""

import copy

from PySide2.QtCore import Qt, Signal
from PySide2.QtWidgets import (QCheckBox, QDoubleSpinBox, QFormLayout,
                               QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                               QListWidget, QListWidgetItem, QMessageBox,
                               QPushButton, QSizePolicy, QSpinBox,
                               QTableWidget, QVBoxLayout, QWidget)
from variant_editor.wizard_base_page import WizardBasePage


class SensorNoiseConfigurationPage(WizardBasePage):
    """Fourth page: Configure sensor noise parameters and generate variants."""

    variant_count_changed = Signal()  # Signal for when variant count changes

    def __init__(self):
        super().__init__()

        self.setTitle("Sensor Noise Configuration")
        self.setSubTitle(
            "Configure sensor noise parameters for navigation variants.")

        self.input_variants = []  # Variants from obstacle placement
        self.output_variants = []  # Final variants with sensor noise configuration

        # Track previous configurations to detect changes
        self._prev_configs = []

        self.setup_ui()

    def setup_ui(self):
        """Set up the user interface."""
        main_layout = QHBoxLayout()

        # Left panel - sensor noise configuration
        left_panel = QVBoxLayout()

        # Input variants count
        self.input_variants_label = QLabel(
            "0 variants from obstacle placement")
        left_panel.addWidget(self.input_variants_label)

        # Sensor noise configuration table
        config_group = QGroupBox("Sensor Noise Configuration")
        config_layout = QVBoxLayout()

        # Table for sensor noise configurations
        self.config_table = QTableWidget()
        self.config_table.setColumnCount(2)
        self.config_table.setHorizontalHeaderLabels(
            ["Drop Rate %", "G. Noise Std Dev"])

        # Set column widths
        header = self.config_table.horizontalHeader()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Stretch)

        config_layout.addWidget(self.config_table)

        # Buttons for table management
        table_buttons_layout = QHBoxLayout()

        self.add_config_button = QPushButton("Add Configuration")
        self.add_config_button.clicked.connect(self.add_noise_config)
        table_buttons_layout.addWidget(self.add_config_button)

        self.remove_config_button = QPushButton("Remove Selected")
        self.remove_config_button.clicked.connect(self.remove_noise_config)
        table_buttons_layout.addWidget(self.remove_config_button)

        table_buttons_layout.addStretch()
        config_layout.addLayout(table_buttons_layout)

        config_group.setLayout(config_layout)
        left_panel.addWidget(config_group)

        # Sensor Noise Seed Configuration
        seed_group = QGroupBox("Sensor Noise Seed")
        seed_layout = QFormLayout()

        self.sensor_noise_seed_spin = QSpinBox()
        self.sensor_noise_seed_spin.setRange(0, 2**31 - 1)
        self.sensor_noise_seed_spin.setValue(123456)  # Default value
        self.sensor_noise_seed_spin.setToolTip(
            "Random seed for sensor noise generation"
        )
        seed_layout.addRow("Sensor Noise Seed:", self.sensor_noise_seed_spin)

        seed_group.setLayout(seed_layout)
        left_panel.addWidget(seed_group)

        # Variant count label
        self.variant_count_label = QLabel("0 variants will be generated")
        left_panel.addWidget(self.variant_count_label)

        # Generate button
        self.generate_button = QPushButton("Generate Variants")
        self.generate_button.clicked.connect(self.generate_variants)
        self.generate_button.setEnabled(False)
        left_panel.addWidget(self.generate_button)

        # Skip sensor noise option
        skip_group = QGroupBox("Skip Sensor Noise Configuration")
        skip_layout = QVBoxLayout()

        self.skip_sensor_noise_checkbox = QCheckBox(
            "Skip sensor noise configuration and use obstacle placement results only"
        )
        self.skip_sensor_noise_checkbox.stateChanged.connect(
            self.on_skip_sensor_noise_changed
        )
        skip_layout.addWidget(self.skip_sensor_noise_checkbox)

        skip_group.setLayout(skip_layout)
        left_panel.addWidget(skip_group)

        left_panel.addStretch()

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
        variants_layout.addWidget(self.variants_list)
        variants_group.setLayout(variants_layout)
        right_panel.addWidget(variants_group)

        # Create layout with left and right panels
        left_widget = QWidget()
        left_widget.setLayout(left_panel)
        left_widget.setMaximumWidth(350)
        main_layout.addWidget(left_widget)

        right_widget = QWidget()
        right_widget.setLayout(right_panel)
        right_widget.setSizePolicy(
            QSizePolicy.Expanding,
            QSizePolicy.Expanding)
        main_layout.addWidget(right_widget)

        self.setLayout(main_layout)

        # Load saved configurations or add defaults
        self.load_configurations()

    def load_configurations(self):
        """Load configurations from VariationData or use defaults if none exist."""
        if (
            self.variation_data
            and self.variation_data.sensor_noise_settings
            and self.variation_data.sensor_noise_settings.noise_configs
        ):
            # Load configurations from VariationData
            for config in self.variation_data.sensor_noise_settings.noise_configs:
                self.add_noise_config_row(config, save=False)
        else:
            # Use default configurations if none in VariationData
            self.add_default_configs()

        # Initialize previous configs tracking
        self._prev_configs = self.get_noise_configs().copy()
        self.update_variant_count()

    def save_configurations(self):
        """Check for configuration changes and potentially clear variants."""
        configs = self.get_noise_configs()

        # Check if configuration has changed and we have generated variants
        if self.output_variants and configs != self._prev_configs:
            res = QMessageBox.question(
                self,
                "Clear Variants?",
                "Changing sensor noise configuration will remove previously generated variants. Continue?",
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
        # Clear current table
        self.config_table.setRowCount(0)

        # Restore previous configurations
        for config in self._prev_configs:
            self.add_noise_config_row(config, save=False)

        self.update_variant_count()

    def clear_generated_variants(self):
        """Clear all generated variants."""
        self.output_variants.clear()
        self.variants_list.clear()
        self.emit_variant_count_update(0)
        self.completeChanged.emit()

    def add_default_configs(self):
        """Add some default sensor noise configurations."""
        default_configs = [
            {"drop_rate": 0.0, "noise_std": 0.0},
            {"drop_rate": 1.0, "noise_std": 0.05},
            {"drop_rate": 1.0, "noise_std": 0.1},
            {"drop_rate": 5.0, "noise_std": 0.1},
        ]

        for config in default_configs:
            self.add_noise_config_row(config, save=False)

        self.update_variant_count()

    def add_noise_config(self):
        """Add a new sensor noise configuration row."""
        config = {"drop_rate": 0.0, "noise_std": 0.0}
        self.add_noise_config_row(config, save=True)
        self.update_variant_count()

    def add_noise_config_row(self, config, save=True):
        """Add a configuration row to the table."""
        row = self.config_table.rowCount()
        self.config_table.insertRow(row)

        # Drop Rate % (DoubleSpinBox)
        drop_rate_spin = QDoubleSpinBox()
        drop_rate_spin.setRange(0.0, 100.0)
        drop_rate_spin.setValue(config["drop_rate"])
        drop_rate_spin.setSuffix(" %")
        drop_rate_spin.setSingleStep(0.1)
        drop_rate_spin.setDecimals(1)
        drop_rate_spin.valueChanged.connect(self.update_variant_count)
        if save:
            drop_rate_spin.valueChanged.connect(self.save_configurations)
        self.config_table.setCellWidget(row, 0, drop_rate_spin)

        # Gaussian Noise Std Dev (DoubleSpinBox)
        noise_spin = QDoubleSpinBox()
        noise_spin.setRange(0.0, 10.0)
        noise_spin.setValue(config["noise_std"])
        noise_spin.setSingleStep(0.01)
        noise_spin.setDecimals(2)
        if save:
            noise_spin.valueChanged.connect(self.save_configurations)
        self.config_table.setCellWidget(row, 1, noise_spin)

    def remove_noise_config(self):
        """Remove the selected sensor noise configuration."""
        current_row = self.config_table.currentRow()
        if current_row >= 0:
            self.config_table.removeRow(current_row)
            self.update_variant_count()
            self.save_configurations()

    def get_noise_configs(self):
        """Get all sensor noise configurations from the table."""
        configs = []
        for row in range(self.config_table.rowCount()):
            drop_rate_widget = self.config_table.cellWidget(row, 0)
            noise_widget = self.config_table.cellWidget(row, 1)

            if drop_rate_widget and noise_widget:
                config = {
                    "drop_rate": drop_rate_widget.value(),
                    "noise_std": noise_widget.value(),
                }
                configs.append(config)

        return configs

    def set_input_variants(self, variants):
        """Set the input variants from the obstacle placement page."""
        self.input_variants = variants
        count = len(variants)
        self.input_variants_label.setText(
            f"{count} variant{'s' if count != 1 else ''} from obstacle placement"
        )
        self.generate_button.setEnabled(
            count > 0
            and self.config_table.rowCount() > 0
            and not self.skip_sensor_noise_checkbox.isChecked()
        )
        self.update_variant_count()

        # If skipping sensor noise, update output variants immediately
        if self.skip_sensor_noise_checkbox.isChecked():
            self.output_variants = []
            self.variants_list.clear()

            # Create copies and assign sensor noise seeds for tracking
            for variant_data in self.input_variants:
                new_variant_data = copy.deepcopy(variant_data)
                # Use seed from widget even when skipping (for consistency)
                if self.variation_data.sensor_noise_settings.sensor_noise_seed is None:
                    self.variation_data.sensor_noise_settings.sensor_noise_seed = (
                        self.sensor_noise_seed_spin.value()
                    )
                self.output_variants.append(new_variant_data)

                item = QListWidgetItem(new_variant_data.name)
                item.setData(Qt.UserRole, new_variant_data)
                self.variants_list.addItem(item)

            # Select first variant if available
            if self.variants_list.count() > 0:
                self.variants_list.setCurrentRow(0)

            # Update status
            self.emit_status_message(
                f"Skipping sensor noise configuration - using "
                f"{len(self.output_variants)} variants from obstacle placement"
            )
            self.emit_variant_count_update(len(self.output_variants))

        # Emit completion changed signal to update Next/Finish button
        self.completeChanged.emit()

    def update_variant_count(self):
        """Update the variant count label."""
        num_input_variants = len(self.input_variants)
        num_configs = self.config_table.rowCount()
        total_variants = num_input_variants * num_configs

        self.variant_count_label.setText(
            f"{total_variants} variants will be generated")
        self.variant_count_changed.emit()
        self.emit_variant_count_update(len(self.output_variants))

    def generate_variants(self):
        """Generate variants with sensor noise configurations."""
        if not self.input_variants:
            QMessageBox.warning(
                self, "No Variants", "No variants from obstacle placement."
            )
            return

        noise_configs = self.get_noise_configs()
        if not noise_configs:
            QMessageBox.warning(
                self, "No Configurations", "No sensor noise configurations defined."
            )
            return

        # Clear previous results
        self.variants_list.clear()
        self.output_variants = []

        # Generate variants for each input variant and each noise configuration
        for variant_data in self.input_variants:
            for config in noise_configs:
                # Create a copy of the variant
                new_variant_data = copy.deepcopy(variant_data)

                # Update variant name to include sensor noise info
                drop_rate_str = f"{config['drop_rate']:.2f}".replace(".", "")
                noise_str = f"{config['noise_std']:.2f}".replace(".", "")
                new_variant_data.name = (
                    f"{variant_data.name}-sd{drop_rate_str}n{noise_str}"
                )

                # Store sensor noise parameters in the variant
                new_variant_data.variant.laserscan_random_drop_percentage = config[
                    "drop_rate"
                ]
                new_variant_data.variant.laserscan_gaussian_noise_std_deviation = (
                    config["noise_std"]
                )

                # Use seed from widget
                if self.variation_data.sensor_noise_settings.sensor_noise_seed is None:
                    self.variation_data.sensor_noise_settings.sensor_noise_seed = (
                        self.sensor_noise_seed_spin.value()
                    )

                # Add to output variants
                self.output_variants.append(new_variant_data)

                # Add to list widget
                item = QListWidgetItem(new_variant_data.name)
                item.setData(Qt.UserRole, new_variant_data)
                self.variants_list.addItem(item)

        # Update status and count
        count = len(self.output_variants)
        self.emit_status_message(
            f"Sensor noise configuration complete - {count} variants generated"
        )
        self.emit_variant_count_update(count)

        # Update previous configs to current state after successful generation
        self._prev_configs = noise_configs.copy()

        # Select first variant if available
        if self.variants_list.count() > 0:
            self.variants_list.setCurrentRow(0)

        # Emit completion changed signal to update Next button
        self.completeChanged.emit()

    def get_output_variants(self):
        """Get the list of generated variants with sensor noise configurations."""
        return self.output_variants

    def isComplete(self):
        """Check if the page is complete."""
        # Page is complete if we have generated variants OR if skipping sensor
        # noise with input variants
        return len(self.output_variants) > 0 or (
            self.skip_sensor_noise_checkbox.isChecked() and len(self.input_variants) > 0
        )

    def on_skip_sensor_noise_changed(self, state):
        """Handle skip sensor noise checkbox state change."""
        skip_enabled = state == Qt.Checked

        # Enable/disable sensor noise configuration
        self.config_table.setEnabled(not skip_enabled)
        self.add_config_button.setEnabled(not skip_enabled)
        self.remove_config_button.setEnabled(not skip_enabled)
        self.generate_button.setEnabled(
            not skip_enabled and len(self.input_variants) > 0
        )

        if skip_enabled:
            # If skipping sensor noise, create copies and assign seeds
            self.output_variants = []
            self.variants_list.clear()

            # Create copies and assign sensor noise seeds for tracking
            for variant_data in self.input_variants:
                new_variant_data = copy.deepcopy(variant_data)
                # Use seed from widget even when skipping (for consistency)
                if self.variation_data.sensor_noise_settings.sensor_noise_seed is None:
                    self.variation_data.sensor_noise_settings.sensor_noise_seed = (
                        self.sensor_noise_seed_spin.value()
                    )
                self.output_variants.append(new_variant_data)

                item = QListWidgetItem(new_variant_data.name)
                item.setData(Qt.UserRole, new_variant_data)
                self.variants_list.addItem(item)

            # Select first variant if available
            if self.variants_list.count() > 0:
                self.variants_list.setCurrentRow(0)

            # Update status
            self.emit_status_message(
                f"Skipping sensor noise configuration - using {
                    len(self.output_variants)} variants from obstacle placement"
            )
            self.emit_variant_count_update(len(self.output_variants))
        else:
            # Clear output variants when not skipping
            self.output_variants = []
            self.variants_list.clear()
            self.emit_variant_count_update(0)

        # Emit completion changed signal to update Next/Finish button
        self.completeChanged.emit()

    def set_variation_data(self, variation_data):
        """Override to reload parameters when variation_data is set."""
        super().set_variation_data(variation_data)
        # Load parameters from variation_data after setting it
        if hasattr(
                self, "sensor_noise_seed_spin"):  # Check if UI is already set up
            self.load_parameters()

    def load_parameters(self):
        """Load parameters from variation_data or use defaults if none exist."""
        if self.variation_data and self.variation_data.sensor_noise_settings:
            settings = self.variation_data.sensor_noise_settings
            sensor_noise_seed = settings.sensor_noise_seed or 123456  # Default if None
            skip_sensor_noise = settings.skip_sensor_noise
        else:
            # Use default values if variation_data is not available
            sensor_noise_seed = 123456
            skip_sensor_noise = False

        # Apply loaded values to widgets
        self.sensor_noise_seed_spin.setValue(sensor_noise_seed)
        self.skip_sensor_noise_checkbox.setChecked(skip_sensor_noise)

    def save_parameters(self):
        """Save current parameters to variation_data."""
        if self.variation_data and self.variation_data.sensor_noise_settings:
            settings = self.variation_data.sensor_noise_settings
            settings.sensor_noise_seed = self.sensor_noise_seed_spin.value()
            settings.skip_sensor_noise = self.skip_sensor_noise_checkbox.isChecked()
