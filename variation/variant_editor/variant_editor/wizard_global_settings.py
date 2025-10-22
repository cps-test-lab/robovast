#!/usr/bin/env python3
"""
Global settings page for the Variant Creation Wizard.
"""

from PySide2.QtCore import Signal
from PySide2.QtWidgets import (QDoubleSpinBox, QFormLayout, QGroupBox,
                               QTextEdit, QVBoxLayout)
from variant_editor.wizard_base_page import WizardBasePage


class GlobalSettingsPage(WizardBasePage):
    """First page: Configure global settings for variant generation."""

    # Signal when robot diameter changes
    robot_diameter_changed = Signal(float)

    def __init__(self):
        super().__init__()

        self.setTitle("Global Settings")
        self.setSubTitle(
            "Configure global parameters used throughout the variant generation process."
        )

        self.setup_ui()

    def setup_ui(self):
        """Set up the user interface."""
        main_layout = QVBoxLayout()

        # Introduction text
        intro_text = QTextEdit()
        intro_text.setMaximumHeight(80)
        intro_text.setReadOnly(True)
        intro_text.setHtml(
            """
        <p>Welcome to the Variant Creation Wizard! This tool helps you generate navigation scenarios
        with different paths, obstacles, and sensor configurations.</p>
        <p><b>First, configure the global robot parameters that will be used across all pages:</b></p>
        """
        )
        main_layout.addWidget(intro_text)

        # Global Robot Parameters Group
        robot_group = QGroupBox("Robot Parameters")
        robot_layout = QFormLayout()

        # Robot Diameter
        self.robot_diameter_spin = QDoubleSpinBox()
        self.robot_diameter_spin.setRange(0.1, 5.0)
        self.robot_diameter_spin.setValue(0.35)  # Default TurtleBot4 diameter
        self.robot_diameter_spin.setSuffix(" m")
        self.robot_diameter_spin.setSingleStep(0.01)
        self.robot_diameter_spin.setDecimals(3)
        self.robot_diameter_spin.setToolTip(
            "The diameter of the robot in meters. This affects path planning clearances and obstacle placement validation. "
            "Common values: TurtleBot4 (0.354m), TurtleBot3 (0.287m), Generic (0.3-0.5m)"
        )
        self.robot_diameter_spin.valueChanged.connect(self.on_robot_diameter_changed)
        robot_layout.addRow("Robot Diameter:", self.robot_diameter_spin)

        robot_group.setLayout(robot_layout)
        main_layout.addWidget(robot_group)

        # Workflow Information Group
        workflow_group = QGroupBox("Workflow Overview")
        workflow_layout = QVBoxLayout()

        workflow_text = QTextEdit()
        workflow_text.setMaximumHeight(150)
        workflow_text.setReadOnly(True)
        workflow_text.setHtml(
            """
        <p>The wizard will guide you through these steps:</p>
        <ol>
        <li><b>Map Selection:</b> Choose which maps to use for variant generation</li>
        <li><b>Path Generation:</b> Generate navigation paths with specified parameters</li>
        <li><b>Obstacle Placement:</b> Add static obstacles to create navigation challenges</li>
        <li><b>Sensor Noise:</b> Configure sensor noise parameters for realistic simulation</li>
        </ol>
        <p>The robot diameter configured here will be used throughout all steps for proper clearance calculations.</p>
        """
        )
        workflow_layout.addWidget(workflow_text)
        workflow_group.setLayout(workflow_layout)
        main_layout.addWidget(workflow_group)

        # Add stretch to push content to top
        main_layout.addStretch()

        self.setLayout(main_layout)

    def on_robot_diameter_changed(self, value):
        """Handle robot diameter changes."""
        # Update parameters
        self.parameters['robot_diameter'] = value
        # Emit signal for other components
        self.robot_diameter_changed.emit(value)

    def set_robot_diameter(self, diameter):
        """Set the robot diameter."""
        self.robot_diameter_spin.setValue(diameter)

    def apply_parameters(self, parameters):
        """Apply parameters from a dictionary to the UI widgets."""
        super().apply_parameters(parameters)

        if 'robot_diameter' in parameters:
            self.set_robot_diameter(parameters['robot_diameter'])

    def isComplete(self):
        """This page is always complete since robot diameter has a valid default."""
        return True
