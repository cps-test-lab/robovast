#!/usr/bin/env python3
"""
Main wizard class for the Variant Creation Wizard.

This module provides the VariantWizard class which coordinates the step-by-step
process of creating navigation variants.
"""

import os
import sys

import yaml
from PySide2.QtWidgets import (QApplication, QLabel, QMainWindow, QMessageBox,
                               QStatusBar, QWizard)
from robovast_common import load_scenario_config
from variant_editor.wizard_floorplan_variation import FloorplanVariationPage
from variant_editor.wizard_global_settings import GlobalSettingsPage
from variant_editor.wizard_map_selection import MapSelectionPage
from variant_editor.wizard_obstacle_placement import ObstaclePlacementPage
from variant_editor.wizard_path_generation import PathGenerationPage
from variant_editor.wizard_sensor_noise import SensorNoiseConfigurationPage


class VariantWizard(QMainWindow):
    """Main wizard for creating navigation variants step-by-step."""

    def __init__(self, variation_file):
        super().__init__()

        self.setWindowTitle("Variant Creation Wizard")
        self.setGeometry(100, 100, 1200, 800)

        self.variation_file = variation_file
        self.generated_variants = []

        # Store global parameters (like robot diameter)
        self.global_parameters = {
            'robot_diameter': 0.35  # Default value, will be updated by global settings page
        }

        # Create the wizard widget
        self.wizard = QWizard()
        self.wizard.setWizardStyle(QWizard.ModernStyle)

        # Initialize pages
        self.global_settings_page = GlobalSettingsPage()
        self.floorplan_variation_page = FloorplanVariationPage(variation_file)
        self.map_selection_page = MapSelectionPage()
        self.path_generation_page = PathGenerationPage(variation_file)
        self.obstacle_placement_page = ObstaclePlacementPage()
        self.sensor_noise_page = SensorNoiseConfigurationPage()

        # Add pages to wizard
        self.wizard.addPage(self.global_settings_page)
        self.wizard.addPage(self.floorplan_variation_page)
        self.wizard.addPage(self.map_selection_page)
        self.wizard.addPage(self.path_generation_page)
        self.wizard.addPage(self.obstacle_placement_page)
        self.wizard.addPage(self.sensor_noise_page)

        # Initialize previous page tracking
        self._prev_page_id = self.wizard.currentId()

        # Set wizard as central widget
        self.setCentralWidget(self.wizard)

        # Setup status bar
        self.setup_status_bar()

        # Load existing settings from file if it exists
        self._load_settings_from_file()

        # Connect signals
        self.path_generation_page.generation_thread = None  # Initialize

        # Connect path generation to obstacle placement
        self.path_generation_page.variant_count_changed.connect(
            self.update_obstacle_placement_variants
        )

        # Connect status updates
        self.floorplan_variation_page.status_message_update.connect(
            self.set_status_message
        )
        self.map_selection_page.maps_selected.connect(self.update_status_bar)

        # Connect page change signal to update status and pass variants
        self.wizard.currentIdChanged.connect(self.handle_page_changed)
        self.wizard.currentIdChanged.connect(self.update_status_bar)

        # Connect wizard finished signal
        self.wizard.finished.connect(self.wizard_finished)

        # Connect generic signals from base class for all pages
        self.map_selection_page.status_message_update.connect(
            self.set_status_message)
        self.map_selection_page.variant_count_update.connect(
            self.set_variant_count_status
        )

        self.path_generation_page.status_message_update.connect(
            self.set_status_message)
        self.path_generation_page.variant_count_update.connect(
            self.set_variant_count_status
        )

        self.obstacle_placement_page.status_message_update.connect(
            self.set_status_message
        )
        self.obstacle_placement_page.variant_count_update.connect(
            self.set_variant_count_status
        )

        self.sensor_noise_page.status_message_update.connect(
            self.set_status_message)
        self.sensor_noise_page.variant_count_update.connect(
            self.set_variant_count_status
        )

        # Keep existing specific signals for compatibility
        self.path_generation_page.variant_count_changed.connect(
            self.update_variant_count_status
        )
        self.obstacle_placement_page.variant_count_changed.connect(
            self.update_variant_count_status
        )
        self.sensor_noise_page.variant_count_changed.connect(
            self.update_variant_count_status
        )

        # Connect global settings robot diameter changes
        self.global_settings_page.robot_diameter_changed.connect(
            self.on_robot_diameter_changed
        )

    def on_robot_diameter_changed(self, diameter):
        """Handle robot diameter changes from global settings."""
        self.global_parameters['robot_diameter'] = diameter

        # Update all pages that use robot diameter
        if hasattr(self.map_selection_page, "set_robot_diameter"):
            self.map_selection_page.set_robot_diameter(diameter)
        if hasattr(self.path_generation_page, "set_global_robot_diameter"):
            self.path_generation_page.set_global_robot_diameter(diameter)
        if hasattr(self.obstacle_placement_page, "set_robot_diameter"):
            self.obstacle_placement_page.set_robot_diameter(diameter)

    def showEvent(self, event):
        """Override to connect path generation signals when the page is ready."""
        super().showEvent(event)
        # Connect generation status signals
        if hasattr(self.path_generation_page, "generation_thread"):
            # We'll connect these when the generation starts
            pass

    def setup_status_bar(self):
        """Setup the status bar at the bottom of the wizard."""
        self.status_bar = QStatusBar()

        # Status message (takes most of the space)
        self.status_message = QLabel("Ready to create variants...")
        self.status_bar.addWidget(self.status_message, 1)  # stretch factor 1

        # Variant count (right side)
        self.variant_count_status = QLabel("0 variants")
        self.status_bar.addPermanentWidget(self.variant_count_status)

        # Set the status bar for the main window
        self.setStatusBar(self.status_bar)

    def wizard_finished(self, result):
        """Handle wizard completion."""
        if result == QWizard.Accepted:
            self.accept()
        else:
            self.close()

    def update_obstacle_placement_variants(self):
        """Update the obstacle placement page with variants from path generation."""
        if hasattr(self, "obstacle_placement_page"):
            variants = self.path_generation_page.get_generated_variants()
            self.obstacle_placement_page.set_input_variants(variants)

    def update_status_bar(self, maps=None):
        """Update the status bar with current information."""
        current_page = self.wizard.currentPage()

        if current_page == self.global_settings_page:
            diameter = self.global_settings_page.get_parameters().get('robot_diameter', 0.35)
            self.status_message.setText(
                f"Global settings configured - Robot diameter: {diameter:.3f}m"
            )
            if hasattr(self, "variant_count_status"):
                self.variant_count_status.setText("0 variants")

        elif current_page == self.map_selection_page:
            selected_count = len(self.map_selection_page.output_variants)
            self.status_message.setText(f"{selected_count} maps selected")
            if hasattr(self, "variant_count_status"):
                self.variant_count_status.setText("0 variants")
        elif current_page == self.path_generation_page:
            if hasattr(self, "path_generation_page"):
                variant_count = len(
                    self.path_generation_page.get_generated_variants())
                if variant_count > 0:
                    self.status_message.setText(
                        f"Path generation complete - {
                            variant_count} variants generated"
                    )
                else:
                    self.status_message.setText(
                        "Configure parameters and generate navigation paths"
                    )
                if hasattr(self, "variant_count_status"):
                    self.variant_count_status.setText(
                        f"{variant_count} variants")
        elif current_page == self.obstacle_placement_page:
            if hasattr(self, "obstacle_placement_page"):
                variant_count = len(
                    self.obstacle_placement_page.get_generated_variants()
                )
                input_count = len(self.obstacle_placement_page.input_variants)
                if variant_count > 0:
                    self.status_message.setText(
                        f"Obstacle placement complete - {
                            variant_count} variants generated"
                    )
                elif input_count > 0:
                    self.status_message.setText(
                        "Configure obstacle parameters and generate variants"
                    )
                else:
                    self.status_message.setText(
                        "Waiting for path generation to complete"
                    )
                if hasattr(self, "variant_count_status"):
                    self.variant_count_status.setText(
                        f"{variant_count} variants")
        elif current_page == self.sensor_noise_page:
            if hasattr(self, "sensor_noise_page"):
                variant_count = len(
                    self.sensor_noise_page.get_output_variants())
                input_count = len(self.sensor_noise_page.input_variants)
                if variant_count > 0:
                    self.status_message.setText(
                        f"Sensor noise configuration complete - {
                            variant_count} variants generated"
                    )
                elif input_count > 0:
                    self.status_message.setText(
                        "Configure sensor noise parameters and generate variants"
                    )
                else:
                    self.status_message.setText(
                        "Waiting for obstacle placement to complete"
                    )
                if hasattr(self, "variant_count_status"):
                    self.variant_count_status.setText(
                        f"{variant_count} variants")

    def set_status_message(self, message):
        """Set the status message."""
        if hasattr(self, "status_message"):
            self.status_message.setText(message)

    def set_variant_count_status(self, count):
        """Set the variant count in the status bar."""
        if hasattr(self, "variant_count_status"):
            self.variant_count_status.setText(f"{count} variants")

    def update_variant_count_status(self):
        """Update just the variant count in the status bar."""
        current_page = self.wizard.currentPage()
        if current_page == self.sensor_noise_page and hasattr(
            self, "sensor_noise_page"
        ):
            variant_count = len(self.sensor_noise_page.get_output_variants())
            if hasattr(self, "variant_count_status"):
                self.variant_count_status.setText(f"{variant_count} variants")
        elif current_page == self.obstacle_placement_page and hasattr(
            self, "obstacle_placement_page"
        ):
            variant_count = len(
                self.obstacle_placement_page.get_generated_variants())
            if hasattr(self, "variant_count_status"):
                self.variant_count_status.setText(f"{variant_count} variants")
        elif current_page == self.path_generation_page and hasattr(
            self, "path_generation_page"
        ):
            variant_count = len(
                self.path_generation_page.get_generated_variants())
            if hasattr(self, "variant_count_status"):
                self.variant_count_status.setText(f"{variant_count} variants")

    def accept(self):
        """Handle wizard completion."""
        # Get final variants from the last page that generated them
        if (
            hasattr(self, "sensor_noise_page")
            and self.sensor_noise_page.get_output_variants()
        ):
            variants = self.sensor_noise_page.get_output_variants()
        elif (
            hasattr(self, "obstacle_placement_page")
            and self.obstacle_placement_page.get_generated_variants()
        ):
            variants = self.obstacle_placement_page.get_generated_variants()
        else:
            variants = self.path_generation_page.get_generated_variants()

        if not variants:
            QMessageBox.warning(
                self, "No Variants", "No variants were generated.")
            return

        # Collect settings from all wizard pages
        settings = self._collect_wizard_settings()

        # Save settings to scenario.variation file
        try:
            self._save_settings_to_file(settings)
            QMessageBox.information(
                self,
                "Success",
                f"Successfully saved variant generation settings to:\n{self.variation_file}\n\n"
                f"{len(variants)} variants were generated and can be regenerated using these settings.",
            )
            self.close()
        except Exception as e:
            QMessageBox.critical(
                self,
                "Error",
                f"Failed to save settings to scenario.variation file:\n{str(e)}"
            )

    def _collect_wizard_settings(self):
        """Collect settings from all wizard pages into a settings dictionary."""
        parameters = {
            'general': {
                'robot_diameter': self.global_parameters.get('robot_diameter', 0.35)
            }
        }

        # Collect FloorplanVariation settings
        parameters['FloorplanVariation'] = self.floorplan_variation_page.get_parameters()
        parameters['PathVariation'] = self.path_generation_page.get_parameters()
        parameters['ObstacleVariation'] = self.obstacle_placement_page.get_parameters()
        parameters['SensorNoise'] = self.sensor_noise_page.get_parameters()

        return parameters

    def _save_settings_to_file(self, settings):
        """Save settings to the scenario.variation file."""
        # Ensure the directory exists
        os.makedirs(os.path.dirname(self.variation_file), exist_ok=True)

        # Create the data structure with settings at the top
        data = {
            'settings': settings
        }

        # Write to file
        with open(self.variation_file, 'w') as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    def _load_settings_from_file(self):
        """Load settings from scenario.variation file if it exists."""
        if not os.path.exists(self.variation_file):
            print(f"No existing settings file found at {self.variation_file}, using defaults")
            return

        try:
            settings = load_scenario_config(self.variation_file)
            if not settings:
                print("No settings found in file, using defaults")
                return

            print(f"Loading settings from {self.variation_file}")
            self._apply_settings_to_pages(settings)
        except Exception as e:
            print(f"Error loading settings from file: {e}")
            QMessageBox.warning(
                self,
                "Settings Load Error",
                f"Could not load settings from {self.variation_file}:\n{str(e)}\n\nUsing default values."
            )

    def _apply_settings_to_pages(self, settings):
        """Apply loaded settings to wizard pages with defaults for missing values."""
        # Apply general settings
        general = settings.get('general', {})
        robot_diameter = general.get('robot_diameter', 0.35)
        self.global_parameters['robot_diameter'] = robot_diameter

        # Apply to global settings page
        self.global_settings_page.set_robot_diameter(robot_diameter)

        # Apply FloorplanVariation settings
        floorplan_parameters = settings.get('FloorplanVariation', {})
        self.floorplan_variation_page.apply_parameters(floorplan_parameters)
        self.floorplan_variation_page.apply_global_parameters(self.global_parameters)

        # Apply PathVariation settings
        path_settings = settings.get('PathVariation', {})
        self.path_generation_page.apply_parameters(path_settings)
        self.path_generation_page.apply_global_parameters(self.global_parameters)

        # Apply ObstacleVariation settings
        obstacle_settings = settings.get('ObstacleVariation', {})
        self.obstacle_placement_page.apply_parameters(obstacle_settings)
        self.obstacle_placement_page.apply_global_parameters(self.global_parameters)

        # Apply SensorNoise settings
        sensor_settings = settings.get('SensorNoise', {})
        self.sensor_noise_page.apply_parameters(sensor_settings)

    def handle_page_changed(self, new_id):
        """On page change, pass output_variants from previous page to next page."""
        prev_page = self.wizard.page(self._prev_page_id)
        next_page = self.wizard.page(new_id)

        if hasattr(prev_page, "output_variants") and hasattr(
            next_page, "set_input_variants"
        ):
            print(
                f"Passing {len(prev_page.output_variants)} variants from page {
                    self._prev_page_id} to page {new_id}"
            )
            next_page.set_input_variants(prev_page.output_variants)

        # Update previous page id
        self._prev_page_id = new_id


def main():
    """Main function to run the variant creation wizard."""
    app = QApplication(sys.argv)

    # Set application properties
    app.setApplicationName("Variant Creation Wizard")
    app.setApplicationVersion("1.0")
    app.setOrganizationName("Robot Navigation")

    # Create and show wizard
    wizard = VariantWizard(
        os.path.join(
            "Dataset", "scenario.variation"))
    wizard.show()

    # Run application
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
