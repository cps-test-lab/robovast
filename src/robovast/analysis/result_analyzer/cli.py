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

import os
import subprocess
import sys

import click

from robovast.analysis.result_analyzer.preprocessing import \
    get_preprocessing_commands
from robovast.common import FileCache
from robovast.common.cli import get_project_config

from .preprocessing import (get_cached_file, get_hash_file_name,
                            is_preprocessing_needed)


@click.group()
def analysis():
    """Analyze test results and generate reports.

    Tools for visualizing and analyzing scenario execution results.
    """


@analysis.command(name='preprocess')
@click.option('--results-dir', '-r', default=None,
              help='Directory containing test results (uses project results dir if not specified)')
@click.option('--force', '-f', is_flag=True,
              help='Force preprocessing by skipping cache check')
def preprocess_cmd(results_dir, force):
    """Run preprocessing commands on test results.

    Executes preprocessing commands defined in the configuration file's
    analysis.preprocessing section. Creates a ``.robovast_preprocessed`` flag
    file with a hash of the commands to track if preprocessing is up to date.

    Requires project initialization with ``vast init`` first (unless ``--results-dir`` is specified).
    """
    # Get project configuration
    project_config = get_project_config()
    config_path = project_config.config_path

    # Use provided results_dir or fall back to project results dir
    results_dir = results_dir if results_dir is not None else project_config.results_dir

    # Get preprocessing commands
    commands = get_preprocessing_commands(config_path)

    if not commands:
        click.echo("No preprocessing commands defined in configuration.")
        return

    command_files = []
    command_paths = []
    for command in commands:
        splitted = command.split()
        if splitted:
            if os.path.isabs(splitted[0]):
                command_path = splitted[0]
            else:
                command_path = os.path.join(os.path.dirname(config_path), splitted[0])

            if os.path.exists(command_path):
                command_paths.append(splitted)
                command_files.append(command_path)
            else:
                click.echo(f"✗ Error: Preprocessing command not found: {command_path}", err=True)
                sys.exit(1)
        else:
            click.echo(f"✗ Error: Invalid preprocessing command: {command}", err=True)
            sys.exit(1)

    cached_file = get_cached_file(os.path.dirname(config_path), results_dir, commands, command_files)

    if cached_file and not force:
        click.echo("✓ Preprocessing is already up to date. No action needed.")
        return

    if force:
        click.echo("Force mode enabled: skipping cache check")

    click.echo("Starting preprocessing...")
    click.echo(f"Results directory: {results_dir}")
    click.echo("-" * 60)

    if not os.path.exists(results_dir):
        click.echo(f"✗ Error: Results directory does not exist: {results_dir}", err=True)
        sys.exit(1)

    # Execute each preprocessing command
    config_dir = os.path.dirname(config_path)
    success = True

    for i, command in enumerate(command_paths, 1):
        command.append(os.path.abspath(results_dir))
        click.echo(f"\n[{i}/{len(command_paths)}] Executing: {' '.join(command)}")
        try:
            # Run the command with results directory as argument
            # Stream output in real-time instead of capturing
            result = subprocess.run(
                command,
                cwd=config_dir,
                check=False,
                env={**os.environ, 'PYTHONUNBUFFERED': '1'}
            )

            if result.returncode == 0:
                click.echo(f"✓ Success")
            else:
                click.echo(f"✗ Failed with exit code {result.returncode}", err=True)
                success = False
                break

        except Exception as e:
            click.echo(f"✗ Error executing command: {e}", err=True)
            success = False
            break

    if success:
        file_cache = FileCache()
        file_cache.set_current_data_directory(os.path.dirname(config_path))
        file_cache.save_file_to_cache(command_files, get_hash_file_name(results_dir), None, content=False, strings_for_hash=commands)

        click.echo("\n" + "=" * 60)
        click.echo("✓ Preprocessing completed successfully!")
    else:
        click.echo("\n" + "=" * 60)
        click.echo("✗ Preprocessing failed!", err=True)
        sys.exit(1)


@analysis.command(name='gui')
@click.option('--results-dir', '-r', default=None,
              help='Directory containing test results (uses project results dir if not specified)')
def result_analyzer_cmd(results_dir):
    """Launch the graphical test results analyzer.

    Opens a GUI application for interactive exploration and
    visualization of test results.

    Requires project initialization with ``vast init`` first (unless ``--results-dir`` is specified).
    """
    # Get project configuration
    project_config = get_project_config()
    config = project_config.config_path

    # Use provided results_dir or fall back to project results dir
    results_dir = results_dir if results_dir is not None else project_config.results_dir

    # Check if preprocessing is needed
    is_needed = is_preprocessing_needed(config, results_dir)
    if is_needed:
        click.echo(f"⚠ Warning: Preprocessing is needed", err=True)
        click.echo("Please run 'vast analysis preprocess' before launching the GUI.", err=True)
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
        click.echo(f"Application error: {e}", err=True)
        sys.exit(1)
