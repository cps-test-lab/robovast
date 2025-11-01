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

import argparse
import sys
from pathlib import Path

from .preprocessing import (
    is_preprocessing_needed,
)

import click
import sys
import os
import subprocess
from robovast.common.cli import get_project_config
from robovast.analysis.result_analyzer.preprocessing import (
    get_preprocessing_commands,
    compute_preprocessing_hash,
    get_flag_file_path,
    is_preprocessing_needed,
)


@click.group()
def analysis():
    """Analyze test results and generate reports.
    
    Tools for visualizing and analyzing scenario execution results.
    """
    pass


@analysis.command(name='preprocess')
@click.option('--output', '-o', default=None,
              help='Directory containing test results (uses project results dir if not specified)')
def preprocess_cmd(output):
    """Run preprocessing commands on test results.
    
    Executes preprocessing commands defined in the configuration file's
    analysis.preprocessing section. Creates a .robovast_preprocessed flag
    file with a hash of the commands to track if preprocessing is up to date.
    
    Requires project initialization with 'vast init' first (unless --output is specified).
    
    Examples:
      vast analysis preprocess
      vast analysis preprocess --output ./custom_results
    """
    # Get project configuration
    project_config = get_project_config()
    config_path = project_config.config_path
    
    # Use provided output or fall back to project results dir
    results_dir = output if output is not None else project_config.results_dir
    
    # Get preprocessing commands
    commands = get_preprocessing_commands(config_path)
    
    if not commands:
        click.echo("No preprocessing commands defined in configuration.")
        return
    
    click.echo("Starting preprocessing...")
    click.echo(f"Results directory: {results_dir}")
    click.echo("-" * 60)
    
    # Make results directory if it doesn't exist
    os.makedirs(results_dir, exist_ok=True)
    
    # Execute each preprocessing command
    config_dir = os.path.dirname(config_path)
    success = True
    
    for i, command in enumerate(commands, 1):        
        # Make command path absolute relative to config file
        if not os.path.isabs(command):
            command_path = os.path.join(config_dir, command)
        else:
            command_path = command
        
        if not os.path.exists(command_path):
            click.echo(f"✗ Error: Command not found: {command_path}", err=True)
            success = False
            break
        
        click.echo(f"\n[{i}/{len(commands)}] Executing: {command_path} {results_dir}")
        try:
            # Run the command with results directory as argument
            result = subprocess.run(
                [command_path, results_dir],
                cwd=config_dir,
                capture_output=True,
                text=True,
                check=False
            )
            
            if result.returncode == 0:
                click.echo(f"✓ Success")
                if result.stdout:
                    click.echo(result.stdout)
            else:
                click.echo(f"✗ Failed with exit code {result.returncode}", err=True)
                if result.stderr:
                    click.echo(result.stderr, err=True)
                if result.stdout:
                    click.echo(result.stdout)
                success = False
                break
                
        except Exception as e:
            click.echo(f"✗ Error executing command: {e}", err=True)
            success = False
            break
    
    if success:
        # Create flag file with hash
        flag_file = get_flag_file_path(results_dir)
        command_hash = compute_preprocessing_hash(commands)
        
        try:
            with open(flag_file, 'w') as f:
                f.write(command_hash)
            
            click.echo("\n" + "=" * 60)
            click.echo("✓ Preprocessing completed successfully!")
            click.echo(f"  Flag file created: {flag_file}")
        except IOError as e:
            click.echo(f"\n✗ Error creating flag file: {e}", err=True)
            sys.exit(1)
    else:
        click.echo("\n" + "=" * 60)
        click.echo("✗ Preprocessing failed!", err=True)
        sys.exit(1)


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
    
    # Check if preprocessing is needed
    is_needed, reason = is_preprocessing_needed(config, results_dir)
    if is_needed:
        click.echo(f"⚠ Warning: {reason}", err=True)
        click.echo("Please run 'vast analysis preprocess' before launching the GUI.", err=True)
        sys.exit(1)
    
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
