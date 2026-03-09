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

"""CLI plugin for run definition."""

import os
import sys
from importlib.metadata import entry_points

import click
import yaml

from robovast.common import (convert_dataclasses_to_dict, filter_configs,
                             generate_scenario_variations,
                             get_scenario_parameters, prepare_campaign_configs)
from robovast.common.cli import get_project_config, handle_cli_exception


@click.group()
def configuration():
    """Manage run configuration.
    """


@configuration.command()
@click.option('--debug', is_flag=True, help='Show internal config values starting with _')
def gui(debug):
    """Launch the graphical configuration editor.

    Opens a GUI for editing and validating RoboVAST configuration files.
    """
    from PySide6.QtWidgets import \
        QApplication  # pylint: disable=import-outside-toplevel

    from robovast.configuration.gui.config_editor import \
        ConfigEditor  # pylint: disable=import-outside-toplevel
    project_config = get_project_config()

    app = QApplication(sys.argv)
    app.setStyle('Fusion')

    try:
        window = ConfigEditor(project_config, debug=debug)
        window.show()
        exit_code = app.exec_()
        window.deleteLater()
        sys.exit(exit_code)

    except Exception as e:
        handle_cli_exception(e)


@configuration.command(name='list')
@click.option('--debug', is_flag=True, help='Show internal values starting with _')
def list_cmd(debug):
    """List scenario configs without generating files.

    This command shows all configs that would be generated from the
    configuration file without actually creating the output files.

    Requires project initialization with ``vast init`` first.
    """
    # Get project configuration
    project_config = get_project_config()
    config = project_config.config_path

    try:
        campaign_data, _ = generate_scenario_variations(
            variation_file=config,
            progress_update_callback=None,
            output_dir=None
        )
        configs_data = campaign_data["configs"]
        configs = convert_dataclasses_to_dict(configs_data)
        if configs:
            # Filter out internal values unless --debug is enabled
            if debug:
                filtered_documents = configs
            else:
                filtered_documents = filter_configs(configs)

            # Build output string with document separators
            output_parts = []
            for i, doc in enumerate(filtered_documents):
                if i > 0:
                    output_parts.append("---")
                output_parts.append(yaml.dump(doc, default_flow_style=False, sort_keys=False).rstrip())

            output = "\n".join(output_parts)
            click.echo(output)
    except Exception as e:
        handle_cli_exception(e)


@configuration.command(name='info')
def info():
    """Show overview of configuration.

    Displays the number of configurations and runs that would be generated
    from the current project configuration.
    """
    # Get project configuration
    project_config = get_project_config()
    config = project_config.config_path

    try:
        campaign_data, _ = generate_scenario_variations(
            variation_file=config,
            progress_update_callback=None,
            output_dir=None
        )
        configs = campaign_data["configs"]
        runs_per_config = campaign_data.get("execution", {}).get("runs", 1)
        total_runs = len(configs) * runs_per_config

        click.echo("Configuration Overview")
        click.echo("======================")
        click.echo(f"Configurations: {len(configs)}")
        click.echo(f"Runs per configuration: {runs_per_config}")
        click.echo(f"Total runs: {total_runs}")
        click.echo(f"Scenario file: {campaign_data.get('scenario_file', 'N/A')}")
        click.echo(f"VAST file: {campaign_data.get('vast', 'N/A')}")
        if "metadata" in campaign_data:
            click.echo(f"Metadata: {campaign_data['metadata']}")
    except Exception as e:
        handle_cli_exception(e)


@configuration.command()
@click.argument('output-dir', type=click.Path())
@click.option('--keep-transient', is_flag=True, default=False,
              help='Keep and display temporary folders used during generation (e.g. by FloorplanGeneration).')
@click.option('--no-cache', is_flag=True, default=False,
              help='Skip cache lookup and force a fresh generation even if inputs are unchanged.')
def generate(output_dir, keep_transient, no_cache):
    """Generate run configurations and output files.

    Creates all configurations and associated files in the
    configured results directory.

    Requires project initialization with ``vast init`` first.
    """
    # Get project configuration
    project_config = get_project_config()
    config = project_config.config_path

    click.echo(f"Generating scenario configurations...")

    try:
        os.makedirs(output_dir, exist_ok=True)

        campaign_data, _ = generate_scenario_variations(
            variation_file=config,
            progress_update_callback=None,
            output_dir=output_dir,
            use_cache=not no_cache,
        )
        configs = campaign_data["configs"]

        if configs:
            config_path_result = os.path.join(output_dir, "out_template")
            prepare_campaign_configs(config_path_result, campaign_data)
            click.echo(f"✓ Successfully generated {len(configs)} scenario configurations in directory '{output_dir}'.")

            if keep_transient:
                _print_transient_locations(campaign_data)
        else:
            click.echo("✗ Failed to generate scenario configurations", err=True)
            sys.exit(1)

    except Exception as e:
        if keep_transient:
            _print_transient_dirs_from_output(output_dir)
        handle_cli_exception(e)


def _print_transient_locations(campaign_data):
    """Print all transient directories produced during config generation."""
    transient_dirs = set()
    _gen_output_dir = campaign_data.get("_output_dir", "")

    for config in campaign_data.get("configs", []):
        for _rel, path in config.get("_config_transient_files", []):
            abs_path = path if os.path.isabs(path) else os.path.join(_gen_output_dir, path)
            transient_dirs.add(os.path.dirname(abs_path))

    for _rel, abs_path in campaign_data.get("_transient_files", []):
        transient_dirs.add(os.path.dirname(abs_path))

    if transient_dirs:
        click.echo("\nTransient directories (--keep-transient):")
        for d in sorted(transient_dirs):
            click.echo(f"  {d}")
    else:
        click.echo("\nNo transient directories were produced.")


def _print_transient_dirs_from_output(output_dir):
    """Scan output_dir for transient working directories left by variations."""
    if not os.path.isdir(output_dir):
        return
    transient_dirs = []
    for entry in sorted(os.scandir(output_dir), key=lambda e: e.name):
        if entry.is_dir() and entry.name != "out_template":
            transient_dirs.append(entry.path)
    if transient_dirs:
        click.echo("\nTransient directories (--keep-transient):", err=True)
        for d in transient_dirs:
            click.echo(f"  {d}", err=True)


@configuration.command(name='variation-types')
def variation_types():
    """List available variation types.

    Shows all registered variation type entry points that can be used
    in the variations section of .vast configuration files.
    """
    click.echo("Available variation types:")
    click.echo("")

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
        handle_cli_exception(e)


@configuration.command(name='variation-points')
def variation_points():
    """List possible variation points from the scenario files.

    Shows all available variation points (scenario parameters) that can be
    varied in the scenarios as defined in the vast configuration file.

    Requires project initialization with ``vast init`` first.
    """
    # Get project configuration
    project_config = get_project_config()
    config = project_config.config_path

    click.echo("Loading scenario parameter template...")
    click.echo("")

    try:
        campaign_data, _ = generate_scenario_variations(
            variation_file=config,
            progress_update_callback=None,
            output_dir=None,
        )
        configs = campaign_data["configs"]
    except Exception as e:
        handle_cli_exception(e)

    unique_scenarios = set()
    for config in configs:
        unique_scenarios.add(config.get('_scenario_file'))

    for scenario_file in unique_scenarios:
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
        print(f"Variation points in scenario file: {scenario_file}")
        for param in scenario_parameters:
            click.echo(f"    {param["name"]}: {param["type"] if not param["is_list"] else f'list[{param["type"]}]'}")
