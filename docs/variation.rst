.. _variation:

Variation
=========

RoboVAST provides a flexible variation mechanism that allows users to declaratively define variation points.

Variation Types
----------------

RoboVAST supports plugin-provided variation types. The following are available by default:

ParameterVariationList
^^^^^^^^^^^^^^^^^^^^^^

Creates variants from a predefined list of parameter values.

  Expected parameters:

  - ``name``: Name of the parameter to vary
  - ``values``: List of values for the parameter


ParameterVariationDistributionUniform
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Creates variants with random parameter values from a uniform distribution.

  Expected parameters:

  - ``name``: Name of the parameter to vary
  - ``num_variations``: Number of variants to create
  - ``min``: Minimum value for the parameter
  - ``max``: Maximum value for the parameter
  - ``type``: Data type of the parameter (e.g., int, float, string)
  - ``seed``: Seed for random number generation to ensure reproducibility

ParameterVariationDistributionGaussian
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Creates variants with random parameter values from a Gaussian (normal) distribution.

  Expected parameters:

  - ``name``: Name of the parameter to vary
  - ``num_variations``: Number of variants to create
  - ``mean``: Mean value for the parameter
  - ``std``: Standard deviation for the parameter
  - ``min``: Minimum value for the parameter
  - ``max``: Maximum value for the parameter
  - ``type``: Data type of the parameter (e.g., int, float, string)
  - ``seed``: Seed for random number generation to ensure reproducibility

Adding New Variation Types
--------------------------

<TBD>