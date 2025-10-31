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
from importlib.metadata import entry_points

@click.group()
@click.version_option(package_name="vast-cli")
def cli():
    """VAST - RoboVAST Command-Line Interface.
    
    A comprehensive tool for managing variations, executing scenarios,
    and analyzing results in the RoboVAST framework.
    """
    pass


@cli.command()
def install_completion():
    """Install shell completion for the vast command.
    
    Auto-detects your shell and installs completion to the appropriate config file.
    
    Examples:
      vast install-completion
    """
    import os
    
    # Auto-detect shell from SHELL environment variable
    shell_env = os.environ.get('SHELL', '')
    if 'zsh' in shell_env:
        shell = 'zsh'
    elif 'fish' in shell_env:
        shell = 'fish'
    else:
        shell = 'bash'
    
    # Generate completion script based on shell
    if shell == 'bash':
        script = 'eval "$(_VAST_COMPLETE=bash_source vast)"'
        config_file = os.path.expanduser('~/.bashrc')
    elif shell == 'zsh':
        script = 'eval "$(_VAST_COMPLETE=zsh_source vast)"'
        config_file = os.path.expanduser('~/.zshrc')
    elif shell == 'fish':
        script = '_VAST_COMPLETE=fish_source vast | source'
        config_file = os.path.expanduser('~/.config/fish/config.fish')
    
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
