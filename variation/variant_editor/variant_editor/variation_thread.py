
from PySide2.QtCore import QThread, Signal
from robovast_common import execute_variation


class VariationThread(QThread):
    """Thread for running floorplan generation without blocking the UI."""

    progress_update = Signal(str)
    generation_complete = Signal(dict)  # output directory path
    generation_failed = Signal(str)

    def __init__(self, variation_class, parameters):
        super().__init__()
        self.variants = []
        self.variation_class = variation_class
        self.parameters = parameters

    def set_input_variants(self, variants):
        """Set the input variants for the thread."""
        self.variants = variants

    def run(self):
        """Execute the variation pipeline."""
        try:
            variants = execute_variation(self.variants,
                                         self.variation_class,
                                         self.parameters,
                                         self.progress_update.emit)

            # Check if variation failed (returned None or empty list)
            if variants is None:
                error_msg = f"{self.variation_class.__name__} failed to generate variants"
                self.progress_update.emit(f"\n✗ Error: {error_msg}")
                self.generation_failed.emit(error_msg)
                return

            if len(variants) == 0:
                error_msg = f"{self.variation_class.__name__} generated no variants"
                self.progress_update.emit(f"\n✗ Error: {error_msg}")
                self.generation_failed.emit(error_msg)
                return

            # All generation complete
            self.progress_update.emit(
                f"\n✓ Generation complete!"
            )
            self.progress_update.emit(
                f"Generated {len(variants)} variants"
            )

            self.generation_complete.emit(variants)

        except Exception as e:
            error_msg = f"Unexpected error: {str(e)}"
            self.progress_update.emit(f"\n✗ Error: {error_msg}")
            self.generation_failed.emit(error_msg)
