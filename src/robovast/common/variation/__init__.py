from .base_variation import Variation
from .parameter_variation import (ParameterVariationDistributionGaussian,
                                  ParameterVariationDistributionUniform,
                                  ParameterVariationList)


# Lazy import for GUI classes to avoid loading PySide6 in headless environments
def __getattr__(name):
    if name == 'VariationGui':
        from .base_variation_gui import \
            VariationGui  # pylint: disable=import-outside-toplevel
        return VariationGui
    elif name == 'VariationGuiRenderer':
        from .base_variation_gui import \
            VariationGuiRenderer  # pylint: disable=import-outside-toplevel
        return VariationGuiRenderer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    'Variation',
    'VariationGui',  # pylint: disable=undefined-all-variable
    'VariationGuiRenderer',  # pylint: disable=undefined-all-variable
    'ParameterVariationList',
    'ParameterVariationDistributionUniform',
    'ParameterVariationDistributionGaussian',
]
