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

from robovast.common.cli import get_project_config

from ...common import run_preprocessing


@click.group()
def analysis():
    """Analyze test results and generate reports.

    Tools for visualizing and analyzing scenario execution results.
    """


@analysis.command(name='preprocess')
@click.option('--results-dir', '-r', default=None,
              help='Directory containing test results (uses project results dir if not specified)')
def preprocess_cmd(results_dir):
    """Run preprocessing commands on test results.

    Executes preprocessing commands defined in the configuration file's
    analysis.preprocessing section. Preprocessing is skipped if the result-directory is unchanged.

    Requires project initialization with ``vast init`` first (unless ``--results-dir`` is specified).
    """
    # Get project configuration
    project_config = get_project_config()
    config_path = project_config.config_path

    # Use provided results_dir or fall back to project results dir
    results_dir = results_dir if results_dir is not None else project_config.results_dir

    click.echo("Starting preprocessing...")
    click.echo(f"Results directory: {results_dir}")
    click.echo("-" * 60)

    # Run preprocessing
    success, message = run_preprocessing(
        config_path=config_path,
        results_dir=results_dir,
        output_callback=click.echo
    )

    click.echo("\n" + "=" * 60)
    if not success:
        click.echo(f"✗ {message}", err=True)
        sys.exit(1)


@analysis.command(name='gui')
@click.option('--results-dir', '-r', default=None,
              help='Directory containing test results (uses project results dir if not specified)')
def result_analyzer_cmd(results_dir):
    """Launch the graphical test results analyzer.

    Opens a GUI application for interactive exploration and
    visualization of test results. Automatically runs preprocessing
    before launching the GUI.

    Requires project initialization with ``vast init`` first (unless ``--results-dir`` is specified).
    """
    # Get project configuration
    project_config = get_project_config()
    config = project_config.config_path

    # Use provided results_dir or fall back to project results dir
    results_dir = results_dir if results_dir is not None else project_config.results_dir

    # Run preprocessing before launching GUI
    success, message = run_preprocessing(
        config_path=config,
        results_dir=results_dir,
        output_callback=click.echo
    )

    if not success:
        click.echo(f"\n✗ Preprocessing failed: {message}", err=True)
        click.echo("Cannot launch GUI without successful preprocessing.", err=True)
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
