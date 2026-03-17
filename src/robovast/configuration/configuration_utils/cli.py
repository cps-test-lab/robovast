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

import fnmatch
import os
import sys

import click
import yaml
from omegaconf import OmegaConf

from robovast.common import get_scenario_parameters
from robovast.common.cli import get_project_config, handle_cli_exception
from robovast.pipeline.callback import load_pipeline_callbacks
from robovast.pipeline.executor import run_pipeline


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
    """Show the resolved scenario configuration.

    Loads and displays the scenario section of the current config.
    Use ``vast run -m`` with Hydra overrides to enumerate sweep combinations.

    Requires project initialization with ``vast init`` first.
    """
    project_config = get_project_config()
    config = project_config.config_path

    try:
        cfg = OmegaConf.load(config)
        data = OmegaConf.to_container(cfg, resolve=True)

        if debug:
            click.echo(yaml.dump(data, default_flow_style=False, sort_keys=False).rstrip())
        else:
            scenario = data.get("scenario", data)
            click.echo(yaml.dump(scenario, default_flow_style=False, sort_keys=False).rstrip())
    except Exception as e:
        handle_cli_exception(e)


@configuration.command(name='info')
def info():
    """Show overview of configuration.

    Displays the scenario name, execution parameters, and pipeline callbacks
    defined in the current project configuration.
    """
    project_config = get_project_config()
    config = project_config.config_path

    try:
        cfg = OmegaConf.load(config)
        data = OmegaConf.to_container(cfg, resolve=True)

        runs = data.get("execution", {}).get("runs", 1)
        scenario_file = data.get("execution", {}).get("scenario_file", "N/A")
        name = data.get("metadata", {}).get("name", data.get("scenario", {}).get("name", "N/A"))
        callbacks = list(data.get("pipeline", {}).keys())

        click.echo("Configuration Overview")
        click.echo("======================")
        click.echo(f"Name: {name}")
        click.echo(f"Config file: {config}")
        click.echo(f"Runs per job: {runs}")
        click.echo(f"Scenario file: {scenario_file}")
        if callbacks:
            click.echo(f"Pipeline callbacks: {', '.join(callbacks)}")
        if "metadata" in data:
            click.echo(f"Metadata: {data['metadata']}")
    except Exception as e:
        handle_cli_exception(e)


@configuration.command(name='export-configs')
@click.argument('args', nargs=-1, required=True, metavar='PATTERN... OUTPUT')
@click.option('--input', 'input_file', default=None, type=click.Path(exists=True),
              help='Source .vast file. Defaults to the project config file.')
@click.option('--remove', is_flag=True, default=False,
              help='Remove the exported configurations from the source .vast file.')
def export_configs(args, input_file, remove):
    """Export configurations matching PATTERN(s) into a new .vast file.

    PATTERN is one or more glob patterns (e.g. 'unirandom*') matched against
    configuration names. OUTPUT is the last argument and specifies the path
    of the new .vast file to create.

    Example::

        vast config export-configs unirandom* new.vast
        vast config export-configs --remove 'office*' 'hospital*' subset.vast
    """
    if len(args) < 2:
        raise click.UsageError(
            "Requires at least one PATTERN and an OUTPUT file.\n"
            "Usage: vast config export-configs PATTERN... OUTPUT"
        )

    patterns = args[:-1]
    output = args[-1]

    # Determine source file
    if input_file:
        source_path = input_file
    else:
        project_config = get_project_config()
        source_path = project_config.config_path

    try:
        with open(source_path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
    except Exception as e:
        handle_cli_exception(e)
        return

    configurations = data.get('configuration', [])
    if not isinstance(configurations, list):
        click.echo("Error: 'configuration' key is missing or not a list.", err=True)
        sys.exit(1)

    def matches_any(name):
        return any(fnmatch.fnmatch(name, p) for p in patterns)

    matched = [cfg for cfg in configurations if matches_any(cfg.get('name', ''))]

    if not matched:
        click.echo(
            f"No configurations matched patterns: {', '.join(patterns)}", err=True
        )
        sys.exit(1)

    data['configuration'] = matched

    output_dir = os.path.dirname(output)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    if os.path.exists(output):
        if not click.confirm(f"File '{output}' already exists. Overwrite?", default=True):
            click.echo("Aborted.")
            sys.exit(0)

    try:
        with open(output, 'w', encoding='utf-8') as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    except Exception as e:
        handle_cli_exception(e)
        return

    click.echo(
        f"✓ Exported {len(matched)} configuration(s) matching "
        f"{', '.join(repr(p) for p in patterns)} → {output}"
    )

    if remove:
        remaining = [cfg for cfg in configurations if not matches_any(cfg.get('name', ''))]
        source_data = dict(data)
        source_data['configuration'] = remaining
        try:
            with open(source_path, 'w', encoding='utf-8') as f:
                yaml.dump(source_data, f, default_flow_style=False,
                          sort_keys=False, allow_unicode=True)
        except Exception as e:
            handle_cli_exception(e)
            return
        click.echo(
            f"✓ Removed {len(matched)} configuration(s) from {source_path}."
        )


@configuration.command()
@click.argument('output-dir', type=click.Path())
def generate(output_dir):
    """Run pipeline callbacks and write generated files to OUTPUT_DIR.

    Instantiates all pipeline callbacks defined in the config and runs
    them in order, producing one set of output files (floorplans, paths,
    obstacle configs, etc.).

    Requires project initialization with ``vast init`` first.
    """
    project_config = get_project_config()
    config = project_config.config_path

    click.echo("Running pipeline callbacks...")

    try:
        os.makedirs(output_dir, exist_ok=True)
        cfg = OmegaConf.load(config)
        from pathlib import Path  # pylint: disable=import-outside-toplevel
        ctx = run_pipeline(cfg, output_dir=Path(output_dir))
        click.echo(f"✓ Pipeline complete. Generated files: {list(ctx.generated_files.keys())}")
    except Exception as e:
        handle_cli_exception(e)


@configuration.command(name='variation-types')
def variation_types():
    """List available pipeline callbacks.

    Shows all registered pipeline callback entry points that can be used
    in the ``pipeline`` section of config files.
    """
    click.echo("Available pipeline callbacks:")
    click.echo("")

    try:
        callbacks = load_pipeline_callbacks()

        if not callbacks:
            click.echo("No pipeline callbacks found.", err=True)
            sys.exit(1)

        for name, cls in callbacks.items():
            click.echo(f"- {name}")
            if cls.__doc__:
                doc_lines = cls.__doc__.strip().split('\n')
                if doc_lines:
                    click.echo(f"  {doc_lines[0].strip()}")
            click.echo()

    except Exception as e:
        handle_cli_exception(e)


@configuration.command(name='variation-points')
def variation_points():
    """List sweepable scenario parameters from the scenario file.

    Shows all scenario parameters that can be swept via Hydra overrides
    (``vast run -m scenario.param=v1,v2``).

    Requires project initialization with ``vast init`` first.
    """
    project_config = get_project_config()
    config_path = project_config.config_path

    click.echo("Loading scenario parameter template...")
    click.echo("")

    try:
        cfg = OmegaConf.load(config_path)
        data = OmegaConf.to_container(cfg, resolve=True)
        scenario_file_name = data.get("execution", {}).get("scenario_file")
        if not scenario_file_name:
            click.echo("Error: No scenario_file defined in execution section", err=True)
            sys.exit(1)

        config_dir = os.path.dirname(config_path)
        scenario_file = scenario_file_name if os.path.isabs(scenario_file_name) \
            else os.path.join(config_dir, scenario_file_name)

        if not os.path.exists(scenario_file):
            click.echo(f"Error: Scenario file does not exist: {scenario_file}", err=True)
            sys.exit(1)

        scenario_template = get_scenario_parameters(scenario_file)
        if not scenario_template:
            click.echo("No variation points found in scenario", err=True)
            sys.exit(1)

        scenario_parameters = next(iter(scenario_template.values()))
        click.echo(f"Variation points in scenario file: {scenario_file}")
        for param in scenario_parameters:
            param_type = param['type'] if not param['is_list'] else f"list[{param['type']}]"
            click.echo(f"    {param['name']}: {param_type}")
    except Exception as e:
        handle_cli_exception(e)
