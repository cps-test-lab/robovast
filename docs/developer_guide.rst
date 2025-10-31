.. _devguide:

Developer Guide
===============

Analysis
--------

Create/debug jupyter notebooks
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

To be able to run jupyter notebooks with convenience functions, provided by RoboVAST,
you need to make use of the poetry-created virtual python environment.

In VSCode, while opening your notebook, be sure to select the correct python environment.

General
-------

Command-line Plugin Development
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

To create a plugin for the VAST CLI:

1. Create a Click group or command in your package
2. Register it in your `pyproject.toml` under `[tool.poetry.plugins."robovast.cli_plugins"]`
3. The plugin will be automatically discovered and added to the `vast` command

Example plugin registration:

.. code-block:: toml

    [tool.poetry.plugins."vast.plugins"]
    variation = "variation_utils.cli:variation"

