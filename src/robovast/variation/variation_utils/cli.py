#!/usr/bin/env python3
# Copyright (C) 2025 Frederik Pasch
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

"""CLI plugin for variation management."""

import os
import sys
import tempfile
from importlib.metadata import entry_points

import click
import yaml

from robovast.common import (filter_variants, generate_scenario_variations,
                             get_scenario_parameters, load_config)
from robovast.common.cli import get_project_config


@click.group()
def variation():
    """Manage scenario variations.

    Generate and list scenario variations from configuration files.
    """


@variation.command()
@click.option('--debug', is_flag=True, help='Show internal values starting with _')
def gui(debug):
    """Launch the graphical configuration editor.

    Opens a GUI for editing and validating RoboVAST configuration files.
    """
    from PySide6.QtWidgets import \
        QApplication  # pylint: disable=import-outside-toplevel
    from robovast.variation.gui.variation_editor import VariationEditor  # pylint: disable=import-outside-toplevel
    project_config = get_project_config()

    app = QApplication(sys.argv)
    app.setStyle('Fusion')

    try:
        window = VariationEditor(project_config, debug=debug)
        window.show()
        exit_code = app.exec_()
        window.deleteLater()
        sys.exit(exit_code)

    except Exception as e:
        click.echo(f"Application error: {e}", err=True)
        sys.exit(1)


@variation.command(name='list')
@click.option('--debug', is_flag=True, help='Show internal values starting with _')
def list_cmd(debug):
    """List scenario variants without generating files.

    This command shows all variants that would be generated from the
    configuration file without actually creating the output files.

    Requires project initialization with ``vast init`` first.
    """
    # Get project configuration
    project_config = get_project_config()
    config = project_config.config_path

    def progress_callback(message):
        click.echo(message)

    click.echo(f"Listing scenario variants from {config}...")
    click.echo("-" * 60)

    with tempfile.TemporaryDirectory(prefix="list_variants_") as temp_dir:
        try:
            variants, _ = generate_scenario_variations(
                variation_file=config,
                progress_update_callback=progress_callback,
                output_dir=temp_dir
            )
            if variants:
                click.echo("-" * 60)
                variants_file = os.path.join(temp_dir, "scenario.variants")
                if os.path.exists(variants_file):

                    with open(variants_file, "r", encoding="utf-8") as vf:
                        # Load all YAML documents
                        all_variants = list(yaml.safe_load_all(vf))

                        # Filter out internal values unless --debug is enabled
                        if debug:
                            filtered_documents = all_variants
                        else:
                            filtered_documents = filter_variants(all_variants)

                        # Build output string with document separators
                        output_parts = []
                        for i, doc in enumerate(filtered_documents):
                            if i > 0:
                                output_parts.append("---")
                            output_parts.append(yaml.dump(doc, default_flow_style=False, sort_keys=False).rstrip())

                        output = "\n".join(output_parts)
                        click.echo(output)
                else:
                    click.echo(f"No scenario.variants file found at {variants_file}", err=True)
                    sys.exit(1)
            else:
                click.echo("✗ Failed to list scenario variants", err=True)
                sys.exit(1)

        except Exception as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)


@variation.command()
@click.argument('output-dir', type=click.Path())
def generate(output_dir):
    """Generate scenario variants and output files.

    Creates all variant configurations and associated files in the
    configured results directory.

    Requires project initialization with ``vast init`` first.
    """
    # Get project configuration
    project_config = get_project_config()
    config = project_config.config_path

    def progress_callback(message):
        click.echo(message)

    click.echo(f"Generating scenario variants from {config}...")
    click.echo(f"Output directory: {output_dir}")
    click.echo("-" * 60)

    try:
        variants, _ = generate_scenario_variations(
            variation_file=config,
            progress_update_callback=progress_callback,
            output_dir=output_dir
        )

        if variants:
            click.echo("-" * 60)
            click.echo(f"✓ Successfully generated {len(variants)} scenario variants!")
        else:
            click.echo("✗ Failed to generate scenario variants", err=True)
            sys.exit(1)

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@variation.command()
def types():
    """List available variation types.

    Shows all registered variation type entry points that can be used
    in the variation section of .vast configuration files.
    """
    click.echo("Available variation types:")
    click.echo("-" * 60)

    try:
        eps = entry_points()
        variation_eps = eps.select(group='robovast.variation_types')

        if not variation_eps:
            click.echo("No variation types found.", err=True)
            sys.exit(1)

        for ep in variation_eps:
            try:
                # Load the class to verify it's accessible
                variation_class = ep.load()
                click.echo(f"- {ep.name}")
                # Try to get docstring if available
                if variation_class.__doc__:
                    doc_lines = variation_class.__doc__.strip().split('\n')
                    if doc_lines:
                        click.echo(f"  {doc_lines[0].strip()}")
                click.echo()
            except Exception as e:
                click.echo(f"  {ep.name} (Failed to load: {e})", err=True)
                click.echo()

    except Exception as e:
        click.echo(f"Error loading variation types: {e}", err=True)
        sys.exit(1)


@variation.command()
def points():
    """List possible variation points from the scenario files.

    Shows all available variation points (scenario parameters) that can be
    varied in the scenarios as defined in the vast configuration file.

    Requires project initialization with ``vast init`` first.
    """
    # Get project configuration
    project_config = get_project_config()
    config = project_config.config_path

    click.echo("Loading scenario parameter template...")
    click.echo("-" * 60)

    # Load the execution section to get the scenario file
    full_config = load_config(config)
    scenarios = full_config.get('definition', [])

    for scenario in scenarios:
        scenario_file = scenario.get('_scenario_file')
        click.echo(f"{scenario['name']} ({scenario_file}):")

        if not scenario_file:
            click.echo("Error: No scenario file found in configuration", err=True)
            sys.exit(1)

        # Make scenario path absolute relative to config file
        if not os.path.isabs(scenario_file):
            scenario_file = os.path.join(os.path.dirname(config), scenario_file)

        if not os.path.exists(scenario_file):
            click.echo(f"Error: Scenario file does not exist: {scenario_file}", err=True)
            sys.exit(1)

        # Get the scenario parameter template
        scenario_template = get_scenario_parameters(scenario_file)

        if scenario_template:
            scenario_parameters = next(iter(scenario_template.values()))
        else:
            scenario_parameters = None

        if not scenario_parameters:
            click.echo("No variation points found in scenario", err=True)
            sys.exit(1)

        # Display the parameters in a readable format
        for param in scenario_parameters:
            click.echo(f"    {param["name"]}: {param["type"] if not param["is_list"] else f'list[{param["type"]}]'}")
