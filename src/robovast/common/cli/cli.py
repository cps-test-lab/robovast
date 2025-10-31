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

"""Main CLI entry point for RoboVAST."""

import click
import os
import sys
from importlib.metadata import entry_points
from .project_config import ProjectConfig

@click.group()
@click.version_option(package_name="robovast", prog_name="RoboVAST")
def cli():
    """VAST - RoboVAST Command-Line Interface.
    
    A comprehensive tool for managing variations, executing scenarios,
    and analyzing results in the RoboVAST framework.
    """
    pass


@cli.command()
@click.option('--config', '-c', required=True, type=click.Path(exists=True),
              help='Path to .vast configuration file')
@click.option('--results-dir', '-r', default="results", type=click.Path(),
              help='Directory for storing results')
def init(config, results_dir):
    """Initialize a VAST project.
    
    Creates a .vast_project file in the current directory that stores
    the configuration file path and results directory. These settings
    will be used by other VAST commands automatically.
    
    Examples:
      vast init --config config.vast --results-dir ./results
      vast init -c scenarios/test.vast -r /tmp/test_results
    """
    # Convert to absolute paths
    project_file_dir = os.path.abspath(os.getcwd())
    if not os.path.isabs(config):
        config_path = os.path.abspath(os.path.join(project_file_dir, config))
    else:
        config_path = config
    if not os.path.isabs(results_dir):
        results_path = os.path.abspath(os.path.join(project_file_dir, results_dir))
    else:
        results_path = results_dir
    
    # Validate config file exists
    if not os.path.isfile(config_path):
        click.echo(f"✗ Error: Configuration file not found: {config_path}", err=True)
        sys.exit(1)
    
    # Create ProjectConfig and save it
    project_config = ProjectConfig(config_path=config_path, results_dir=results_path)
    
    # Validate the configuration
    is_valid, error = project_config.validate()
    if not is_valid:
        click.echo(f"✗ Error: {error}", err=True)
        sys.exit(1)
    
    # Check if .vast_project already exists
    existing_file = ProjectConfig.find_project_file()
    if existing_file:
        click.echo(f"⚠ Warning: Overwriting existing project file: {existing_file}")
    
    # Save the project file
    project_file = project_config.save()
    
    click.echo(f"✓ Project initialized successfully!")
    click.echo(f"  Configuration: {config_path}")
    click.echo(f"  Results directory: {results_path}")
    click.echo(f"  Project file: {project_file}")


@cli.command()
def install_completion():
    """Install shell completion for the vast command.
    
    Auto-detects your shell and installs completion to the appropriate config file.
    
    Examples:
      vast install-completion
    """
    
    # Auto-detect shell from SHELL environment variable
    shell_env = os.environ.get('SHELL', '')
    if 'zsh' in shell_env:
        shell = 'zsh'
    elif 'fish' in shell_env:
        shell = 'fish'
    else:
        shell = 'bash'
    
    # Generate completion script based on shell
    script = None
    if shell == 'bash':
        script = 'eval "$(_VAST_COMPLETE=bash_source vast)"'
        config_file = os.path.expanduser('~/.bashrc')
    elif shell == 'zsh':
        script = 'eval "$(_VAST_COMPLETE=zsh_source vast)"'
        config_file = os.path.expanduser('~/.zshrc')
    elif shell == 'fish':
        script = '_VAST_COMPLETE=fish_source vast | source'
        config_file = os.path.expanduser('~/.config/fish/config.fish')
    else:
        raise click.ClickException(f"Unsupported shell for completion installation: {shell}")
    
    # Install to the config file
    try:
        # Ensure directory exists
        os.makedirs(os.path.dirname(config_file), exist_ok=True)
        
        # Check if completion is already installed
        if os.path.exists(config_file):
            with open(config_file, 'r') as f:
                content = f.read()
                if script in content:
                    click.echo(f"✓ Completion already installed in {config_file}")
                    return
        
        # Append completion script to config file
        with open(config_file, 'a') as f:
            f.write(f"\n# VAST CLI completion\n{script}\n")
        
        click.echo(f"✓ Completion installed successfully!")
        click.echo(f"  Shell: {shell}")
        click.echo(f"  Added to: {config_file}")
        click.echo()
        click.echo("Restart your shell or run:")
        click.echo(f"  source {config_file}")
    except Exception as e:
        click.echo(f"✗ Failed to install completion: {e}", err=True)
        raise click.Exit(1)


def load_plugins():
    """Dynamically load all VAST CLI plugins from entry points."""
    try:        
        eps = entry_points(group='robovast.cli_plugins')

        for ep in eps:
            try:
                # Load the entry point (should return a Click group or command)
                plugin_group = ep.load()
                # Add it as a subcommand to the main CLI
                cli.add_command(plugin_group, name=ep.name)
            except Exception as e:
                click.echo(f"Warning: Failed to load plugin '{ep.name}': {e}", err=True)
    except Exception as e:
        click.echo(f"Warning: Failed to load plugins: {e}", err=True)


def main():
    """Main entry point for the VAST CLI."""
    # Load all plugins before running the CLI
    load_plugins()
    
    # Run the CLI
    cli()


if __name__ == '__main__':
    main()
