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

"""Main CLI entry point for VAST."""

import click
import sys


@click.group()
@click.version_option(package_name="vast-cli")
def cli():
    """VAST - RoboVAST Command-Line Interface.
    
    A comprehensive tool for managing variations, executing scenarios,
    and analyzing results in the RoboVAST framework.
    
    Shell completion:
      Bash:   eval "$(_VAST_COMPLETE=bash_source vast)"
      Zsh:    eval "$(_VAST_COMPLETE=zsh_source vast)"
      Fish:   _VAST_COMPLETE=fish_source vast | source
    """
    pass


@cli.command()
@click.option('--shell', type=click.Choice(['bash', 'zsh', 'fish'], case_sensitive=False),
              required=True, help='Shell type for completion')
@click.option('--show', is_flag=True, help='Show completion script instead of installing')
def completion(shell, show):
    """Install or show shell completion script.
    
    Examples:
      vast completion --shell bash          # Install bash completion
      vast completion --shell zsh --show    # Show zsh completion script
    """
    shell_lower = shell.lower()
    
    # Generate completion script
    if shell_lower == 'bash':
        script = 'eval "$(_VAST_COMPLETE=bash_source vast)"'
        install_cmd = 'echo \'eval "$(_VAST_COMPLETE=bash_source vast)"\' >> ~/.bashrc'
    elif shell_lower == 'zsh':
        script = 'eval "$(_VAST_COMPLETE=zsh_source vast)"'
        install_cmd = 'echo \'eval "$(_VAST_COMPLETE=zsh_source vast)"\' >> ~/.zshrc'
    elif shell_lower == 'fish':
        script = '_VAST_COMPLETE=fish_source vast | source'
        install_cmd = 'echo \'_VAST_COMPLETE=fish_source vast | source\' >> ~/.config/fish/config.fish'
    
    if show:
        # Just show the script
        click.echo(script)
    else:
        # Provide installation instructions
        click.echo(f"To enable {shell} completion for the vast command, add this to your shell config:")
        click.echo()
        click.echo(f"  {script}")
        click.echo()
        click.echo("You can do this automatically by running:")
        click.echo()
        click.echo(f"  {install_cmd}")
        click.echo()
        click.echo("Then restart your shell or run:")
        click.echo(f"  source ~/.{shell_lower}rc")


def load_plugins():
    """Dynamically load all VAST CLI plugins from entry points."""
    try:
        from importlib.metadata import entry_points
        
        # Python 3.10+ compatible
        if sys.version_info >= (3, 10):
            eps = entry_points(group='vast.plugins')
        else:
            eps = entry_points().get('vast.plugins', [])
        
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
