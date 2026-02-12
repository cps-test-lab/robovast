from .base_variation import Variation
from .parameter_variation import (ParameterVariationDistributionGaussian,
                                  ParameterVariationDistributionUniform,
                                  ParameterVariationList)

# Lazy import for GUI classes to avoid loading PySide6 in headless environments
def __getattr__(name):
    if name == 'VariationGui':
        from .base_variation_gui import VariationGui
        return VariationGui
    elif name == 'VariationGuiRenderer':
        from .base_variation_gui import VariationGuiRenderer
        return VariationGuiRenderer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    'Variation',
    'VariationGui',
    'VariationGuiRenderer',
    'ParameterVariationList',
    'ParameterVariationDistributionUniform',
    'ParameterVariationDistributionGaussian',
]
