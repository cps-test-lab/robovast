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

"""CLI plugin for test definition."""

import os
import sys
import tempfile
from importlib.metadata import entry_points

import click
import yaml
from matplotlib import pyplot as plt

from robovast.common import (convert_dataclasses_to_dict, filter_configs,
                             generate_scenario_variations,
                             get_scenario_parameters)
from robovast.common.cli import get_project_config, handle_cli_exception


@click.group()
def configuration():
    """Manage test configuration.
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
@click.option('--visualize', is_flag=True, help="Plot configurations")
@click.option('--debug', is_flag=True, help='Show internal values starting with _')
def list_cmd(debug, visualize):
    """List scenario configs without generating files.

    This command shows all configs that would be generated from the
    configuration file without actually creating the output files.

    Requires project initialization with ``vast init`` first.
    """
    # Get project configuration
    project_config = get_project_config()
    config = project_config.config_path

    try:
        run_data, _ = generate_scenario_variations(
            variation_file=config,
            progress_update_callback=None,
            output_dir=None
        )
        configs_data = run_data["configs"]
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
            if visualize:
                for config in configs:
                    fig, ax = plt.subplots()
                    plot_map(ax, config)
                    plot_config(ax, config)
                    if not os.path.exists("figs"):
                        os.makedirs("figs")
                    plt.savefig("figs/" + config.get("name") + ".png", bbox_inches='tight')
    except Exception as e:
        handle_cli_exception(e)


def plot_map(ax, config):
    """
    Plot the occupancy grid
    """
    import matplotlib.pyplot as plt

    map_file = "environments/secorolab/maps/secorolab.yaml"
    # map_file = config.get("_map_file")
    with open(map_file, "r") as f:
        meta_data = yaml.safe_load(f)

    pgm_file = map_file.replace(".yaml", ".pgm")
    im = plt.imread(pgm_file)

    resolution = meta_data.get("resolution")
    x_orig, y_orig, yaw = meta_data.get("origin")
    ymax, xmax = im.shape

    ax.imshow(
        im,
        cmap="gray",
        origin="upper",
        extent=(
            x_orig,
            (xmax * resolution) - abs(x_orig),
            y_orig,
            (ymax * resolution) - abs(y_orig),
        ),
    )

def plot_config(ax, config):
    """
    Plot start and goal poses.

    Also plots the path if it is computed and included in the config.
    """
    _config = config.get("config")
    start_pose = _config.get("start_pose")
    ax.scatter(
        start_pose["position"]["x"],
        start_pose["position"]["y"],
        color="green",
        marker="*",
        label="start pose",
    )
    goal_poses = _config.get("goal_poses")
    for i, g in enumerate(goal_poses):
        x = g["position"]["x"]
        y = g["position"]["y"]
        ax.scatter(
            x,
            y,
            color="blue",
            marker="o",
            label="goal pose",
        )
        ax.annotate(f"g{i}", (x, y), fontsize=10)

    if config.get("_path"):
        x_path = []
        y_path = []
        for p in config.get("_path"):
            x_path.append(p.x)
            y_path.append(p.y)

        ax.plot(x_path, y_path, color="red")

    ax.set_aspect("equal", adjustable="box")
    # hide axes and borders
    ax.axis("off")


@configuration.command()
@click.argument('output-dir', type=click.Path())
def generate(output_dir):
    """Generate test configurations and output files.

    Creates all configurations and associated files in the
    configured results directory.

    Requires project initialization with ``vast init`` first.
    """
    # Get project configuration
    project_config = get_project_config()
    config = project_config.config_path

    click.echo(f"Generating scenario configurations...")

    try:
        run_data, _ = generate_scenario_variations(
            variation_file=config,
            progress_update_callback=None,
            output_dir=output_dir
        )
        configs = run_data["configs"]

        if configs:
            click.echo(f"✓ Successfully generated {len(configs)} scenario configurations in directory '{output_dir}'.")
        else:
            click.echo("✗ Failed to generate scenario configurations", err=True)
            sys.exit(1)

    except Exception as e:
        handle_cli_exception(e)


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

    with tempfile.TemporaryDirectory(prefix="robovast_list_configs_") as temp_dir:
        try:
            run_data, _ = generate_scenario_variations(
                variation_file=config,
                progress_update_callback=None,
                output_dir=temp_dir
            )
            configs = run_data["configs"]
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
