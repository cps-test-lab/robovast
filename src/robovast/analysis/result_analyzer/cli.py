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

"""CLI for the result analyzer."""

import sys

import click

from robovast.common.cli import get_project_config, handle_cli_exception
from robovast.common.postprocessing import load_postprocessing_plugins

from ...common import run_postprocessing
from ..merge_results import merge_results


@click.group()
def analysis():
    """Analyze test results and generate reports.

    Tools for visualizing and analyzing scenario execution results.
    """


@analysis.command(name='postprocess')
@click.option('--results-dir', '-r', default=None,
              help='Directory containing test results (uses project results dir if not specified)')
@click.option('--force', '-f', is_flag=True,
              help='Force postprocessing even if results directory is unchanged (bypasses caching)')
def postprocess_cmd(results_dir, force):
    """Run postprocessing commands on test results.

    Executes postprocessing commands defined in the configuration file's
    analysis.postprocessing section. Postprocessing is skipped if the result-directory is unchanged,
    unless --force is specified.

    Requires project initialization with ``vast init`` first (unless ``--results-dir`` is specified).
    """
    # Get project configuration
    project_config = get_project_config()
    config_path = project_config.config_path

    # Use provided results_dir or fall back to project results dir
    results_dir = results_dir if results_dir is not None else project_config.results_dir

    click.echo("Starting postprocessing...")
    click.echo(f"Results directory: {results_dir}")
    if force:
        click.echo("Force mode enabled: bypassing cache")
    click.echo("-" * 60)

    # Run postprocessing
    success, message = run_postprocessing(
        config_path=config_path,
        results_dir=results_dir,
        output_callback=click.echo,
        force=force
    )

    click.echo("\n" + "=" * 60)
    if not success:
        click.echo(f"✗ {message}", err=True)
        sys.exit(1)


@analysis.command(name='gui')
@click.option('--results-dir', '-r', default=None,
              help='Directory containing test results (uses project results dir if not specified)')
@click.option('--force', '-f', is_flag=True,
              help='Force postprocessing even if results directory is unchanged (bypasses caching)')
@click.option('--skip-postprocessing', is_flag=True,
              help='Skip postprocessing before launching the GUI')
def result_analyzer_cmd(results_dir, force, skip_postprocessing):
    """Launch the graphical test results analyzer.

    Opens a GUI application for interactive exploration and
    visualization of test results. Automatically runs postprocessing
    before launching the GUI.

    Requires project initialization with ``vast init`` first (unless ``--results-dir`` is specified).
    """
    # Get project configuration
    project_config = get_project_config()
    config = project_config.config_path

    # Use provided results_dir or fall back to project results dir
    results_dir = results_dir if results_dir is not None else project_config.results_dir

    # Run postprocessing before launching GUI (unless skipped)
    if not skip_postprocessing:
        success, message = run_postprocessing(
            config_path=config,
            results_dir=results_dir,
            output_callback=click.echo,
            force=force
        )

        if not success:
            click.echo(f"\n✗ Postprocessing failed: {message}", err=True)
            if not click.confirm(
                "Proceed to GUI anyway?",
                default=True
            ):
                sys.exit(1)

    try:
        from PySide6.QtWidgets import \
            QApplication  # pylint: disable=import-outside-toplevel

        from .result_analyzer import \
            TestResultsAnalyzer  # pylint: disable=import-outside-toplevel
    except ImportError as e:
        click.echo(
            f"Error: Required dependencies not available: {e}\n"
            "Install result-analyzer dependencies (PySide6, matplotlib, etc.)",
            err=True
        )
        sys.exit(1)

    app = QApplication(sys.argv)
    app.setStyle('Fusion')

    try:
        window = TestResultsAnalyzer(base_dir=results_dir, config_file=config)
        window.show()
        exit_code = app.exec_()
        window.deleteLater()
        sys.exit(exit_code)

    except Exception as e:
        handle_cli_exception(e)


@analysis.command(name='merge-results')
@click.argument('merged_run_dir', type=click.Path())
@click.option('--results-dir', '-r', default=None,
              help='Source directory containing run-\\* dirs (uses project results dir if not specified)')
def merge_results_cmd(merged_run_dir, results_dir):
    """Merge run-dirs with identical configs into one merged_run_dir.

    Groups run-dir/config-dir by config_identifier from config.yaml.
    Test folders (0, 1, 2, ...) from all runs are renumbered and copied.
    Original run-dirs are not modified.

    Requires project initialization with ``vast init`` first (unless ``--results-dir`` is specified).
    """
    if results_dir is not None:
        source_dir = results_dir
    else:
        project_config = get_project_config()
        source_dir = project_config.results_dir

    click.echo(f"Merging from {source_dir} into {merged_run_dir}...")
    try:
        success, message = merge_results(source_dir, merged_run_dir)
        if success:
            click.echo(f"✓ {message}")
        else:
            click.echo(f"✗ {message}", err=True)
            sys.exit(1)
    except Exception as e:
        handle_cli_exception(e)


@analysis.command(name='postprocess-commands')
def list_postprocessing_commands():
    """List all available postprocessing command plugins.

    Shows plugin names that can be used in the analysis.postprocessing section
    of the configuration file, along with their descriptions and parameters.
    """
    plugins = load_postprocessing_plugins()

    if not plugins:
        click.echo("No postprocessing command plugins available.")
        click.echo("\nPostprocessing commands can be registered as plugins.")
        click.echo("See documentation for how to add custom postprocessing commands.")
        return

    click.echo("Available postprocessing command plugins:")
    click.echo("=" * 70)

    # Sort by plugin name for consistent output
    for plugin_name in sorted(plugins.keys()):
        click.echo(f"\n{plugin_name}")

        # Try to get the function's docstring
        try:
            func = plugins[plugin_name]
            if func.__doc__:
                # Clean up docstring and display first line
                doc_lines = [line.strip() for line in func.__doc__.strip().split('\n') if line.strip()]
                if doc_lines:
                    click.echo(f"  Description: {doc_lines[0]}")
        except Exception:
            pass

    click.echo("\n" + "=" * 70)
    click.echo("\nUsage in configuration file:")
    click.echo("\n  analysis:")
    click.echo("    postprocessing:")
    click.echo("    - rosbags_tf_to_csv:")
    click.echo("        frames: [base_link, map]")
    click.echo("    - rosbags_bt_to_csv")
    click.echo("    - command:")
    click.echo("        script: ../../../tools/custom_script.sh")
    click.echo("        args: [--arg, value]")
    click.echo("\nCommands without parameters can be simple strings.")
    click.echo("Commands with parameters use plugin name as key with parameters as dict.")
