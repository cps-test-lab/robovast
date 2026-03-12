from .base_variation import ProvContribution, Variation
from .loader import load_variation_classes, validate_variation_plugins
from .one_of_variation import OneOfVariation
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
    'ProvContribution',
    'Variation',
    'VariationGui',  # pylint: disable=undefined-all-variable
    'VariationGuiRenderer',  # pylint: disable=undefined-all-variable
    'load_variation_classes',
    'OneOfVariation',
    'ParameterVariationList',
    'ParameterVariationDistributionUniform',
    'ParameterVariationDistributionGaussian',
]
