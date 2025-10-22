#!/usr/bin/env python3
"""
Floorplan variation page for the Variant Creation Wizard.

This page allows selection of *.variation files and generates floorplan variations
by calling the scenery_builder.sh script multiple times with different parameters.
"""

import os

from PySide2.QtCore import Qt, Slot
from PySide2.QtWidgets import (QAbstractItemView, QFormLayout, QHBoxLayout,
                               QLabel, QListWidget, QListWidgetItem,
                               QProgressBar, QPushButton, QSpinBox, QTextEdit,
                               QVBoxLayout)
from robovast_common import FloorplanVariation
from variant_editor.wizard_base_page import WizardBasePage

from .variation_thread import VariationThread


class FloorplanVariationPage(WizardBasePage):
    """Page for generating floorplan variations using floorplan dsl scenery_builder"""

    def __init__(self, variation_file):
        super().__init__()
        self.setTitle("Floorplan Variation")
        self.setSubTitle(
            "Select variation files and generate floorplan variations using scenery_builder"
        )
        self.script_path = None
        self.generation_thread = None
        self.generated_output_dir = None
        self.is_generating = False
        self.variation_file = variation_file
        self.setup_ui()
        self.load_variation_files(os.path.dirname(variation_file))

    def setup_ui(self):
        """Set up the user interface."""
        main_layout = QVBoxLayout()

        # Instructions
        instructions = QLabel(
            "Select one or more *.variation files from the Dataset/floorplans directory. "
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
        self.num_variations_spin.valueChanged.connect(self.on_num_variations_changed)
        variations_layout.addRow("Number of Variations:", self.num_variations_spin)

        # Floorplan Variation Seed
        self.floorplan_variation_seed_spin = QSpinBox()
        self.floorplan_variation_seed_spin.setRange(0, 2147483647)  # 2^31-1
        self.floorplan_variation_seed_spin.setValue(42)  # Default seed
        self.floorplan_variation_seed_spin.setSpecialValueText("Random")
        self.floorplan_variation_seed_spin.setToolTip(
            "Seed for reproducible floorplan generation (0 for random)"
        )
        self.floorplan_variation_seed_spin.valueChanged.connect(self.on_seed_value_changed)
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

        self.parameters_changed.connect(self.on_parameters_changed)
        self.setLayout(main_layout)

    @Slot()
    def on_seed_value_changed(self, value):
        self.parameters["floorplan_variation_seed"] = value

    @Slot()
    def on_num_variations_changed(self, value):
        self.parameters["num_variations"] = value

    @Slot()
    def on_parameters_changed(self):
        num_variations = self.parameters.get("num_variations", 1)
        floorplan_variation_seed = self.parameters.get("floorplan_variation_seed", 42)

        # Apply loaded values to spinboxes and display
        self.num_variations_spin.setValue(num_variations)
        self.floorplan_variation_seed_spin.setValue(floorplan_variation_seed)
        self.update_selected_variation_files()

    def load_variation_files(self, floorplan_dir):
        """Load available *.variation files"""
        variation_files = []

        if os.path.isdir(floorplan_dir):
            # Walk through all subdirectories
            for root, _, files in os.walk(floorplan_dir):
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
            self.update_selected_variation_files()

    def update_selected_variation_files(self):
        for i in range(self.variation_list.count()):
            item = self.variation_list.item(i)
            var_file = item.data(Qt.UserRole)
            if not self.parameters:
                print("NO PARAM")

            if self.parameters and "variation_files" in self.parameters:
                print(f"var files {self.parameters['variation_files']}")
                if "name" in var_file and var_file["name"] in self.parameters["variation_files"] and item.flags() & Qt.ItemIsSelectable:
                    item.setSelected(True)

    def on_selection_changed(self):
        """Handle selection changes in the variation file list."""
        selected_items = self.variation_list.selectedItems()
        selected_variation_files = []

        for item in selected_items:
            var_file = item.data(Qt.UserRole)
            if var_file:
                rel_path = os.path.relpath(var_file["path"], os.path.dirname(self.variation_file))
                selected_variation_files.append(rel_path)

        count = len(selected_variation_files)
        if count == 0:
            self.selection_info.setText("No variation files selected")
            self.generate_btn.setEnabled(False)
        elif count == 1:
            self.selection_info.setText("1 variation file selected")
            self.generate_btn.setEnabled(True)
        else:
            self.selection_info.setText(f"{count} variation files selected")
            self.generate_btn.setEnabled(True)
        self.parameters["variation_files"] = selected_variation_files
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

    def start_generation(self):
        """Start the floorplan generation process."""
        if not self.parameters["variation_files"]:
            self.log_output.append("No variation files selected!")
            return

        # Disable generate button and show progress
        self.generate_btn.setEnabled(False)
        self.is_generating = True
        self.completeChanged.emit()  # Disable next button
        self.progress_bar.setVisible(True)
        self.log_output.clear()
        self.log_output.append(
            f"Starting generation for {len(self.parameters["variation_files"])} variation file(s)"
            f"with {self.parameters["num_variations"]} variation(s) each (seed {self.parameters["floorplan_variation_seed"]})...\n"
        )

        self.generation_thread = VariationThread(
            FloorplanVariation,
            {
                'FloorplanVariation': self.parameters,
                'general': self.global_parameters
            }
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

    def generation_complete(self, variants):
        """Handle successful generation completion."""
        self.set_output_variants(variants)
        self.is_generating = False
        self.progress_bar.setVisible(False)
        self.generate_btn.setEnabled(True)
        count = len(variants) if variants else 0
        self.emit_status_message(
            f"Floorplan generation complete: {count} variations generated"
        )
        self.completeChanged.emit()  # Enable next button

    def generation_failed(self, error_message):
        """Handle generation failure."""
        self.is_generating = False
        self.progress_bar.setVisible(False)
        self.generate_btn.setEnabled(True)
        self.emit_status_message(f"Floorplan generation failed: {error_message}")

    def isComplete(self):
        """Page is complete when generation has been run successfully."""
        # Prevent proceeding if generation is in progress
        if self.is_generating:
            return False
        return super().isComplete()
