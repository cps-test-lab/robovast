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

    def __init__(self):
        self.input_variants = []  # List of input variants
        self.output_variants = []  # List of output variants
        self.variation_data = None  # Will be set by the main wizard
        super().__init__()

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

    def set_variation_data(self, variation_data):
        """Set the variation data for this page."""
        self.variation_data = variation_data
