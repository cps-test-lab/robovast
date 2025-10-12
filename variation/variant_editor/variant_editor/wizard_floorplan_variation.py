#!/usr/bin/env python3
"""
Floorplan variation page for the Variant Creation Wizard.

This page allows selection of *.variation files and generates floorplan variations
by calling the scenery_builder.sh script multiple times with different parameters.
"""

import os
import subprocess
import tarfile
import tempfile
from robovast_common import FileCache, generate_floorplan_variations, get_scenario_base_path

from PySide2.QtCore import QThread, Qt, Signal
from PySide2.QtWidgets import (QAbstractItemView, QFormLayout, QHBoxLayout,
                               QLabel, QListWidget, QListWidgetItem,
                               QProgressBar, QPushButton, QSpinBox, QTextEdit,
                               QVBoxLayout)
from variant_editor.wizard_base_page import WizardBasePage
from variant_editor.data_models import FloorplanVariationSettings


class FloorplanGenerationThread(QThread):
    """Thread for running floorplan generation without blocking the UI."""

    progress_update = Signal(str)
    generation_complete = Signal(str)  # output directory path
    generation_failed = Signal(str)

    def __init__(self, variation_files, script_path, output_dir, num_variations, seed_value):
        super().__init__()
        self.variation_files = variation_files
        self.num_variations = num_variations
        self.seed_value = seed_value
        self.script_path = script_path
        self.output_dir = output_dir
        self.file_cache = FileCache()

    def run(self):
        """Execute the floorplan generation pipeline."""
        try:            
            all_map_dirs = generate_floorplan_variations(self.variation_files, 
                                                        self.num_variations,
                                                        self.seed_value,
                                                        self.output_dir,
                                                        self.progress_update.emit)
    
            # All generation complete
            self.progress_update.emit(
                f"\n✓ Generation complete! Output directory: {self.output_dir}"
            )
            self.progress_update.emit(
                f"Generated {len(all_map_dirs)} floorplan variations"
            )
            self.generation_complete.emit(self.output_dir)

        except subprocess.CalledProcessError as e:
            error_msg = f"Command failed: {" ".join(e.cmd)}\n"
            if e.stdout:
                error_msg += f"stdout: {e.stdout}\n"
            if e.stderr:
                error_msg += f"stderr: {e.stderr}"
            self.progress_update.emit(f"\n✗ Error: {error_msg}")
            self.generation_failed.emit(error_msg)
        except Exception as e:
            error_msg = f"Unexpected error: {str(e)}"
            self.progress_update.emit(f"\n✗ Error: {error_msg}")
            self.generation_failed.emit(error_msg)


class FloorplanVariationPage(WizardBasePage):
    """Page for generating floorplan variations using floorplan dsl scenery_builder"""

    def __init__(self):
        super().__init__()
        self.setTitle("Floorplan Variation")
        self.setSubTitle(
            "Select variation files and generate floorplan variations using scenery_builder"
        )
        self.selected_variation_files = []
        self.script_path = None
        self.generation_thread = None
        self.generated_output_dir = None
        self.is_generating = False
        # Track temporary directories for cleanup
        self.temp_dirs = []
        self.setup_ui()
        self.load_variation_files()

    def setup_ui(self):
        """Set up the user interface."""
        main_layout = QVBoxLayout()

        # Instructions
        instructions = QLabel(
            "Select one or more *.variation files from the Dataset/floorplans directory. "
            "The local scenery_builder.sh script will be executed to generate "
            "floorplan variations (variation → transform → generate)."
        )
        instructions.setWordWrap(True)
        main_layout.addWidget(instructions)

        # Selection buttons
        button_layout = QHBoxLayout()
        select_all_btn = QPushButton("Select All")
        unselect_all_btn = QPushButton("Unselect All")
        select_all_btn.clicked.connect(self.select_all_files)
        unselect_all_btn.clicked.connect(self.unselect_all_files)
        button_layout.addWidget(select_all_btn)
        button_layout.addWidget(unselect_all_btn)
        main_layout.addLayout(button_layout)

        # Variation file list
        self.variation_list = QListWidget()
        self.variation_list.setSelectionMode(QAbstractItemView.MultiSelection)
        self.variation_list.itemSelectionChanged.connect(
            self.on_selection_changed
        )
        main_layout.addWidget(self.variation_list)

        # Selection info
        self.selection_info = QLabel("No variation files selected")
        main_layout.addWidget(self.selection_info)

        # Number of variations input
        variations_layout = QFormLayout()
        self.num_variations_spin = QSpinBox()
        self.num_variations_spin.setMinimum(1)
        self.num_variations_spin.setMaximum(1000)
        self.num_variations_spin.setValue(1)
        self.num_variations_spin.setToolTip(
            "Number of variations to generate for each variation file"
        )
        variations_layout.addRow("Number of Variations:", self.num_variations_spin)
        
        # Floorplan Variation Seed
        self.floorplan_variation_seed_spin = QSpinBox()
        self.floorplan_variation_seed_spin.setRange(0, 2147483647)  # 2^31-1
        self.floorplan_variation_seed_spin.setValue(42)  # Default seed
        self.floorplan_variation_seed_spin.setSpecialValueText("Random")
        self.floorplan_variation_seed_spin.setToolTip(
            "Seed for reproducible floorplan generation (0 for random)"
        )
        variations_layout.addRow("Floorplan Variation Seed:", self.floorplan_variation_seed_spin)
        
        main_layout.addLayout(variations_layout)

        # Generate button
        self.generate_btn = QPushButton("Generate Floorplan Variations")
        self.generate_btn.clicked.connect(self.start_generation)
        self.generate_btn.setEnabled(False)
        main_layout.addWidget(self.generate_btn)

        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)  # Indeterminate progress
        self.progress_bar.setVisible(False)
        main_layout.addWidget(self.progress_bar)

        # Log output
        log_label = QLabel("Generation Log:")
        main_layout.addWidget(log_label)

        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setMaximumHeight(200)
        main_layout.addWidget(self.log_output)

        self.setLayout(main_layout)

    def load_parameters(self):
        """Load parameters from variation_data or use defaults if none exist."""
        # Load values from variation_data if available, otherwise use defaults
        if self.variation_data and self.variation_data.floorplan_variation_settings:
            settings = self.variation_data.floorplan_variation_settings
            num_variations = settings.num_variations
            variation_files = settings.variation_files
            floorplan_variation_seed = settings.floorplan_variation_seed
        else:
            # Use default values if variation_data is not available
            variation_files = []
            num_variations = 1
            floorplan_variation_seed = 1

        # Apply loaded values to spinboxes and display
        self.num_variations_spin.setValue(num_variations)
        self.floorplan_variation_seed_spin.setValue(floorplan_variation_seed)


    def load_variation_files(self):
        """Load available *.variation files from Dataset/floorplans."""
        floorplan_dir = os.path.join(
            get_scenario_base_path(), "floorplans"
        )
        variation_files = []

        if os.path.isdir(floorplan_dir):
            # Walk through all subdirectories
            for root, dirs, files in os.walk(floorplan_dir):
                for file in files:
                    if file.endswith(".variation"):
                        full_path = os.path.join(root, file)
                        # Get relative path from floorplan_dir
                        rel_path = os.path.relpath(full_path, floorplan_dir)
                        variation_files.append(
                            {"name": rel_path, "path": full_path}
                        )

        if variation_files:
            for var_file in variation_files:
                item = QListWidgetItem(var_file["name"])
                item.setData(Qt.UserRole, var_file)
                self.variation_list.addItem(item)
        else:
            item = QListWidgetItem(
                "No *.variation files found in Dataset/floorplans"
            )
            item.setFlags(item.flags() & ~Qt.ItemIsSelectable)
            self.variation_list.addItem(item)

        for i in range(self.variation_list.count()):
            item = self.variation_list.item(i)
            var_file = item.data(Qt.UserRole)
            variation_files = ["rooms/rooms.variation"]
            if "name" in var_file and var_file["name"] in variation_files and item.flags() & Qt.ItemIsSelectable:
                print(f"TODO MATCH {var_file}")
                item.setSelected(True)


    def on_selection_changed(self):
        """Handle selection changes in the variation file list."""
        selected_items = self.variation_list.selectedItems()
        self.selected_variation_files = []

        for item in selected_items:
            var_file = item.data(Qt.UserRole)
            if var_file:
                self.selected_variation_files.append(var_file["path"])

        count = len(self.selected_variation_files)
        if count == 0:
            self.selection_info.setText("No variation files selected")
            self.generate_btn.setEnabled(False)
        elif count == 1:
            self.selection_info.setText("1 variation file selected")
            self.generate_btn.setEnabled(True)
        else:
            self.selection_info.setText(f"{count} variation files selected")
            self.generate_btn.setEnabled(True)

        self.emit_status_message(f"{count} variation files selected")
        self.completeChanged.emit()

    def select_all_files(self):
        """Select all variation files."""
        for i in range(self.variation_list.count()):
            item = self.variation_list.item(i)
            if item.flags() & Qt.ItemIsSelectable:
                item.setSelected(True)

    def unselect_all_files(self):
        """Unselect all variation files."""
        self.variation_list.clearSelection()

    def get_local_script(self):
        """Get the local scenery_builder.sh script path."""
        try:
            self.log_output.append("Locating local scenery_builder.sh script...")

            # Get the path to the local script
            # Assuming the script is in dependencies/scenery_builder/ relative to the workspace root
            workspace_root = get_scenario_base_path()
            # Go up two levels from Dataset directory to get to workspace root
            workspace_root = os.path.dirname(workspace_root)
            script_path = os.path.join(workspace_root, "dependencies", "scenery_builder", "scenery_builder.sh")

            # Check if the script exists
            if not os.path.exists(script_path):
                self.log_output.append(f"✗ Script not found at: {script_path}")
                return False

            # Make it executable
            os.chmod(script_path, 0o755)

            self.script_path = script_path
            self.log_output.append(f"✓ Using local script: {script_path}")
            return True

        except Exception as e:
            self.log_output.append(
                f"✗ Failed to locate script: {str(e)}"
            )
            return False

    def start_generation(self):
        """Start the floorplan generation process."""
        if not self.selected_variation_files:
            self.log_output.append("No variation files selected!")
            return

        # Get local script if needed
        if not self.script_path or not os.path.exists(self.script_path):
            if not self.get_local_script():
                return

        # Disable generate button and show progress
        self.generate_btn.setEnabled(False)
        self.is_generating = True
        self.completeChanged.emit()  # Disable next button
        self.progress_bar.setVisible(True)
        self.log_output.clear()
        num_variations = self.num_variations_spin.value()
        seed_value = self.floorplan_variation_seed_spin.value()
        self.log_output.append(
            f"Starting generation for {len(self.selected_variation_files)} variation file(s) "
            f"with {num_variations} variation(s) each...\n"
        )

        output_dir = tempfile.TemporaryDirectory(prefix="map_", delete=False)
        # Track temporary directory for cleanup
        self.temp_dirs.append(output_dir)
            
        # Create and start generation thread
        self.generation_thread = FloorplanGenerationThread(
            self.selected_variation_files, self.script_path, output_dir.name, num_variations, seed_value
        )
        self.generation_thread.progress_update.connect(self.update_log)
        self.generation_thread.generation_complete.connect(
            self.generation_complete
        )
        self.generation_thread.generation_failed.connect(
            self.generation_failed
        )
        self.emit_status_message(
            f"Floorplan generation started."
        )
        self.generation_thread.start()

    def update_log(self, message):
        """Update the log output with a new message."""
        self.log_output.append(message)
        # Scroll to bottom
        self.log_output.verticalScrollBar().setValue(
            self.log_output.verticalScrollBar().maximum()
        )

    def generation_complete(self, output_dir):
        """Handle successful generation completion."""
        self.generated_output_dir = output_dir
        self.is_generating = False
        self.progress_bar.setVisible(False)
        self.generate_btn.setEnabled(True)
        self.emit_status_message(
            f"Floorplan generation complete: {output_dir}"
        )
        self.completeChanged.emit()  # Enable next button

    def generation_failed(self, error_message):
        """Handle generation failure."""
        self.is_generating = False
        self.progress_bar.setVisible(False)
        self.generate_btn.setEnabled(True)
        self.emit_status_message("Floorplan generation failed")
        self.completeChanged.emit()  # Re-enable next button if already generated before

    def isComplete(self):
        """Page is complete when generation has been run successfully."""
        # Prevent proceeding if generation is in progress
        if self.is_generating:
            return False
        # Allow proceeding if we have a generated output directory
        return self.generated_output_dir is not None

    def get_generated_output_dir(self):
        """Return the directory containing generated floorplan variations."""
        return self.generated_output_dir

    def set_variation_data(self, variation_data):
        """Override to load parameters from VariationData after it's set."""
        super().set_variation_data(variation_data)
        self._load_settings_from_variation_data()

    def _load_settings_from_variation_data(self):
        """Load floorplan variation settings from VariationData into UI widgets."""
        if not self.variation_data or not self.variation_data.floorplan_variation_settings:
            return

        settings = self.variation_data.floorplan_variation_settings

        # Load number of variations
        if settings.num_variations > 0:
            self.num_variations_spin.setValue(settings.num_variations)

        # Load seed value
        if settings.floorplan_variation_seed is not None:
            self.floorplan_variation_seed_spin.setValue(settings.floorplan_variation_seed)
        else:
            # Default to 42 if no seed is set
            self.floorplan_variation_seed_spin.setValue(42)

        # Load and select variation files
        if settings.variation_files:
            # Clear current selection
            self.variation_list.clearSelection()

            # Select items that match the saved variation files
            for i in range(self.variation_list.count()):
                item = self.variation_list.item(i)
                var_file = item.data(Qt.UserRole)
                if var_file and var_file["path"] in settings.variation_files:
                    item.setSelected(True)

    def validatePage(self):
        """Validate the page when moving to next page."""
        # Update variation data with current values
        if self.variation_data and self.variation_data.floorplan_variation_settings:
            self.variation_data.floorplan_variation_settings.variation_files = (
                self.selected_variation_files.copy()
            )
            self.variation_data.floorplan_variation_settings.num_variations = (
                self.num_variations_spin.value()
            )
            # Save seed value (0 means random, stored as None in data model)
            seed_value = self.floorplan_variation_seed_spin.value()
            self.variation_data.floorplan_variation_settings.floorplan_variation_seed = (
                seed_value if seed_value > 0 else None
            )
        return True

    def cleanup_temp_dirs(self):
        """Clean up all temporary directories created for map extraction."""
        for temp_dir in self.temp_dirs:
            try:
                temp_dir.cleanup()
                print(f"Cleaned up temporary directory: {temp_dir.name}")
            except Exception as exc:
                print(f"Failed to clean up temporary directory {temp_dir.name}: {exc}")
        self.temp_dirs.clear()

    def __del__(self):
        """Destructor to ensure temporary directories are cleaned up."""
        self.cleanup_temp_dirs()
