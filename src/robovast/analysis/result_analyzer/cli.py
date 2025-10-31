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

"""CLI plugin for analysis tools."""

import click
import sys
from robovast.common.cli.project_config import get_project_config


@click.group()
def analysis():
    """Analyze test results and generate reports.
    
    Tools for visualizing and analyzing scenario execution results.
    """
    pass


@analysis.command(name='gui')
@click.option('--output', '-o', default=None,
              help='Directory containing test results (uses project results dir if not specified)')
def result_analyzer_cmd(output):
    """Launch the graphical test results analyzer.
    
    Opens a GUI application for interactive exploration and
    visualization of test results.
    
    Requires project initialization with 'vast init' first (unless --output is specified).
    
    Examples:
      vast analysis gui
      vast analysis gui --output ./custom_results
    """
    # Get project configuration
    project_config = get_project_config()
    config = project_config.config_path
    
    # Use provided output or fall back to project results dir
    results_dir = output if output is not None else project_config.results_dir
    
    try:
        from PySide6.QtWidgets import QApplication # pylint: disable=import-outside-toplevel
        from .result_analyzer import TestResultsAnalyzer # pylint: disable=import-outside-toplevel
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
