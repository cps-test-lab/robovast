#!/usr/bin/env python3
"""
Main wizard class for the Variant Creation Wizard.

This module provides the VariantWizard class which coordinates the step-by-step
process of creating navigation variants.
"""

import os
import sys

from PySide2.QtWidgets import (QApplication, QLabel, QMainWindow, QMessageBox,
                               QStatusBar, QWizard)
from robovast_common import get_scenario_base_path
from variant_editor.data_models import (ObstaclePlacementSettings,
                                        PathGenerationSettings,
                                        SensorNoiseSettings, VariationData,
                                        load_variation_data_from_file,
                                        save_variation_data_to_file)
from variant_editor.wizard_floorplan_variation import FloorplanVariationPage
from variant_editor.wizard_global_settings import GlobalSettingsPage
from variant_editor.wizard_map_selection import MapSelectionPage
from variant_editor.wizard_obstacle_placement import ObstaclePlacementPage
from variant_editor.wizard_path_generation import PathGenerationPage
from variant_editor.wizard_sensor_noise import SensorNoiseConfigurationPage


class VariantWizard(QMainWindow):
    """Main wizard for creating navigation variants step-by-step."""

    def __init__(self, scenario_variants_path):
        super().__init__()

        self.setWindowTitle("Variant Creation Wizard")
        self.setGeometry(100, 100, 1200, 800)

        self.scenario_variants_path = scenario_variants_path
        self.selected_maps = []
        self.generated_variants = []

        # Initialize VariationData to store all data and settings
        # Try to load existing data first, otherwise use defaults
        if os.path.exists(scenario_variants_path):
            self.variation_data, info = load_variation_data_from_file(
                scenario_variants_path)
            if self.variation_data is None or info:
                QMessageBox.warning(
                    self, "Warning", f"Loading variation data failed. {info}"
                )
                self.variation_data = VariationData()
        else:
            QMessageBox.warning(
                self,
                "Warning",
                f"Scenario variants file not found at {scenario_variants_path}. "
                "Starting with empty variation data.",
            )
            self.variation_data = VariationData()
        # Create the wizard widget
        self.wizard = QWizard()
        self.wizard.setWizardStyle(QWizard.ModernStyle)

        # Initialize pages
        self.global_settings_page = GlobalSettingsPage()
        self.floorplan_variation_page = FloorplanVariationPage()
        self.map_selection_page = MapSelectionPage()
        self.path_generation_page = PathGenerationPage(scenario_variants_path)
        self.obstacle_placement_page = ObstaclePlacementPage()
        self.sensor_noise_page = SensorNoiseConfigurationPage()

        # Set variation data on all pages
        self.global_settings_page.set_variation_data(self.variation_data)
        self.floorplan_variation_page.set_variation_data(self.variation_data)
        self.map_selection_page.set_variation_data(self.variation_data)
        self.path_generation_page.set_variation_data(self.variation_data)
        self.obstacle_placement_page.set_variation_data(self.variation_data)
        self.sensor_noise_page.set_variation_data(self.variation_data)

        # Connect global settings signals
        self.global_settings_page.robot_diameter_changed.connect(
            self.update_robot_diameter_on_all_pages
        )

        # Set initial robot diameter on all pages
        self.update_robot_diameter_on_all_pages()

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

    def update_robot_diameter_on_all_pages(self):
        """Update robot diameter setting on all wizard pages."""
        diameter = self.variation_data.general_parameters.robot_diameter
        #TODO: generalize
        # Update map selection page
        if hasattr(self.map_selection_page, "set_robot_diameter"):
            self.map_selection_page.set_robot_diameter(diameter)

        # Update path generation page
        if hasattr(self.path_generation_page, "set_global_robot_diameter"):
            self.path_generation_page.set_global_robot_diameter(diameter)

        # Update obstacle placement page
        if hasattr(self.obstacle_placement_page, "set_robot_diameter"):
            self.obstacle_placement_page.set_robot_diameter(diameter)

        # Update sensor noise page if needed
        if hasattr(self.sensor_noise_page, "set_robot_diameter"):
            self.sensor_noise_page.set_robot_diameter(diameter)

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
            diameter = self.global_settings_page.get_robot_diameter()
            self.status_message.setText(
                f"Global settings configured - Robot diameter: {diameter:.3f}m"
            )
            if hasattr(self, "variant_count_status"):
                self.variant_count_status.setText("0 variants")
        elif current_page == self.floorplan_variation_page:
            if hasattr(self, "floorplan_variation_page"):
                if self.floorplan_variation_page.generated_output_dir:
                    self.status_message.setText(
                        f"Floorplan variations generated: {self.floorplan_variation_page.generated_output_dir}"
                    )
                else:
                    selected_count = len(self.floorplan_variation_page.selected_variation_files)
                    self.status_message.setText(
                        f"{selected_count} variation file(s) selected - click Generate to proceed"
                    )
            if hasattr(self, "variant_count_status"):
                self.variant_count_status.setText("0 variants")
        elif current_page == self.map_selection_page:
            selected_count = len(self.map_selection_page.selected_maps)
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
        # Get final variants from sensor noise page if available, otherwise
        # obstacle placement, otherwise path generation
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

        # Collect settings from all pages and create complete VariationData
        self._collect_page_settings()
        self.variation_data.variants = variants

        # Write scenario.variants file using the new VariationData model
        try:
            save_variation_data_to_file(
                self.variation_data, self.scenario_variants_path
            )
            QMessageBox.information(
                self,
                "Success",
                f"Successfully created {
                    len(variants)} variants with settings in scenario.variants file.",
            )
            self.close()  # Close the main window instead of calling super().accept()
        except Exception as e:
            QMessageBox.critical(
                self, "Error", f"Failed to write scenario.variants file: {
                    str(e)}"
            )

    def _collect_page_settings(self):
        """Collect settings from all wizard pages into VariationData."""
        # Collect path generation settings (robot_diameter is now global)
        if hasattr(self.path_generation_page, "path_length_spin"):
            self.variation_data.path_generation_settings = PathGenerationSettings(
                path_length=self.path_generation_page.path_length_spin.value(),
                num_paths=self.path_generation_page.num_paths_spin.value(),
                robot_diameter=self.variation_data.general_parameters.robot_diameter,  # Use global setting
                path_length_tolerance=self.path_generation_page.path_length_tolerance_spin.value(),
                min_distance=self.path_generation_page.min_distance_spin.value(),
                path_generation_seed=self.path_generation_page.path_generation_seed_spin.value(),
            )

        # Collect obstacle placement settings
        if hasattr(self.obstacle_placement_page, "get_obstacle_configs"):
            obstacle_configs = self.obstacle_placement_page.get_obstacle_configs()
            # Preserve existing seed if it exists
            existing_seed = None
            if (
                self.variation_data.obstacle_placement_settings
                and self.variation_data.obstacle_placement_settings.obstacle_placement_seed
                is not None
            ):
                existing_seed = (
                    self.variation_data.obstacle_placement_settings.obstacle_placement_seed
                )

            self.variation_data.obstacle_placement_settings = ObstaclePlacementSettings(
                obstacle_configs=obstacle_configs, obstacle_placement_seed=existing_seed
            )

        # Collect sensor noise settings
        if hasattr(self.sensor_noise_page, "get_noise_configs"):
            noise_configs = self.sensor_noise_page.get_noise_configs()
            skip_sensor_noise = getattr(
                self.sensor_noise_page, "skip_sensor_noise_checkbox", None
            )
            skip_value = skip_sensor_noise.isChecked() if skip_sensor_noise else False

            # Get seed from UI widget if available, otherwise preserve existing
            # seed
            sensor_noise_seed = None
            if hasattr(self.sensor_noise_page, "sensor_noise_seed_spin"):
                sensor_noise_seed = (
                    self.sensor_noise_page.sensor_noise_seed_spin.value()
                )
            elif (
                self.variation_data.sensor_noise_settings
                and self.variation_data.sensor_noise_settings.sensor_noise_seed
                is not None
            ):
                sensor_noise_seed = (
                    self.variation_data.sensor_noise_settings.sensor_noise_seed
                )

            self.variation_data.sensor_noise_settings = SensorNoiseSettings(
                noise_configs=noise_configs,
                skip_sensor_noise=skip_value,
                sensor_noise_seed=sensor_noise_seed,
            )

        # General parameters are already updated by the global settings page
        # but ensure it's set correctly
        if hasattr(self.global_settings_page, "get_robot_diameter"):
            self.variation_data.general_parameters.robot_diameter = (
                self.global_settings_page.get_robot_diameter()
            )

    def handle_page_changed(self, new_id):
        """On page change, pass output_variants from previous page to next page."""
        prev_page = self.wizard.page(self._prev_page_id)
        next_page = self.wizard.page(new_id)

        # Special handling: pass floorplan variation output to map selection
        if (
            prev_page == self.floorplan_variation_page
            and next_page == self.map_selection_page
        ):
            print("Setting maps dir to", self.floorplan_variation_page.get_generated_output_dir())
            self.map_selection_page.set_maps_dir(self.floorplan_variation_page.get_generated_output_dir())
            self.path_generation_page.set_maps_dir(self.floorplan_variation_page.get_generated_output_dir())
            self.obstacle_placement_page.set_maps_dir(self.floorplan_variation_page.get_generated_output_dir())
        # If previous page has output_variants and next page can accept
        # input_variants
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
            get_scenario_base_path(),
            "scenario.variants"))
    wizard.show()

    # Run application
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
