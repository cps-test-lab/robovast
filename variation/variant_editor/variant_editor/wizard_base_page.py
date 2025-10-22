#!/usr/bin/env python3
"""
Base class for wizard pages with status bar integration.
"""

from PySide2.QtCore import Signal
from PySide2.QtWidgets import QWizardPage


class WizardBasePage(QWizardPage):
    """Base class for wizard pages with status bar signals."""

    # Generic signals for status bar updates
    status_message_update = Signal(str)  # Signal for status message updates
    variant_count_update = Signal(int)  # Signal for variant count updates
    parameters_changed = Signal(dict)

    def __init__(self):
        self.input_variants = []  # List of input variants
        self.output_variants = []  # List of output variants
        self.parameters = {}
        self.global_parameters = {}
        super().__init__()

    def apply_parameters(self, parameters):
        self.parameters = parameters
        self.parameters_changed.emit(parameters)

    def get_parameters(self):
        return self.parameters

    def apply_global_parameters(self, parameters):
        self.global_parameters = parameters

    def emit_status_message(self, message):
        """Emit a status message update."""
        self.status_message_update.emit(message)

    def emit_variant_count_update(self, count):
        """Emit a variant count update."""
        self.variant_count_update.emit(count)

    def set_input_variants(self, variants):
        """
        Set the input variants for the wizard page.

        Args:
            variants (list of Variant): List of Variant objects from data_model.
        """
        self.input_variants = variants

    def set_output_variants(self, variants):
        """
        Set the output variants for the wizard page.

        Args:
            variants (list of Variant): List of Variant objects to be output.
        """
        self.output_variants = variants
        self.emit_variant_count_update(len(variants))

    def isComplete(self):
        # Allow proceeding if we have a generated output directory
        return self.output_variants is not None
