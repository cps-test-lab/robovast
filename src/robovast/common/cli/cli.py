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

import os
import shutil
import sys
import tarfile
from importlib.metadata import entry_points

import click

from ..common import load_config
from ..kubernetes import get_kubernetes_client
from ..preprocessing import reset_preprocessing_cache
from .checks import check_docker_access, check_kubernetes_access
from .project_config import ProjectConfig, get_project_config


@click.group()
@click.version_option(package_name="robovast", prog_name="RoboVAST")
def cli():
    """VAST - RoboVAST Command-Line Interface.

    Main command for managing variations, executing scenarios,
    and analyzing results in the RoboVAST framework.

    See ``vast --help`` for a list of available commands.
    """


@cli.command()
@click.argument('config', type=click.Path(exists=True))
@click.option('--results-dir', '-r', default="results", type=click.Path(),
              help='Directory for storing results')
@click.option('--force', '-f', is_flag=True,
              help='Skip Docker and Kubernetes accessibility checks')
def init(config, results_dir, force):
    """Initialize a VAST project.

    Creates a `.vast_project` file in the current directory that stores
    the configuration file path and results directory. These settings
    will be used by other VAST commands automatically.

    By default, performs the following checks before initialization:

    * Docker daemon accessibility and version
    * Kubernetes cluster connectivity and version
    * robovast pod is running in the default namespace

    Use the ``--force`` flag to skip all these checks if needed.
    """
    # Check Docker and Kubernetes access unless --force is used
    # Check Docker access
    if force:
        click.echo("⚠ Warning: Skipping checks (--force enabled)")

    # check integrity of config file
    try:
        load_config(config)
    except Exception as e:
        click.echo(f"✗ Error: Failed to load configuration file: {e}", err=True)
        if not force:
            sys.exit(1)

    click.echo("Checking Docker daemon access...")
    docker_ok, docker_msg = check_docker_access()
    if not docker_ok and not force:
        click.echo(f"✗ Error: {docker_msg}", err=True)
        click.echo("  Docker is required for RoboVAST execution.", err=True)
        sys.exit(1)
    click.echo(f"✓ {docker_msg}")

    # Check Kubernetes access
    k8s_client = get_kubernetes_client()
    click.echo("Checking Kubernetes cluster access...")
    k8s_ok, k8s_msg = check_kubernetes_access(k8s_client)
    if not k8s_ok:
        click.echo(f"✗ Error: {k8s_msg}", err=True)
        click.echo("  Kubernetes cluster is required for RoboVAST execution.", err=True)
        if not force:
            sys.exit(1)
    click.echo(f"✓ {k8s_msg}")

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


@cli.command()
@click.argument('archive', type=click.Path(exists=True))
@click.option('--output', '-o', default=None,
              help='Directory where results will be extracted (uses project results dir if not specified)')
@click.option('--force', '-f', is_flag=True,
              help='Force extraction even if run directory already exists')
def import_results(archive, output, force):
    """Import results from a downloaded archive.

    Extracts a tar.gz archive (created by ``vast execution cluster download``)
    to the results directory. This is useful for importing results that were
    downloaded on a different machine or for re-importing previously downloaded results.

    The archive should be in the format ``run-<ID>.tar.gz`` and contain
    a run directory with all test results.

    Requires project initialization with ``vast init`` first (unless ``--output`` is specified).
    """
    # Get output directory
    project_config = None
    if output is None:
        # Get from project configuration
        try:
            project_config = get_project_config()
            output = project_config.results_dir
        except Exception as e:
            click.echo("Error: Could not load project configuration.", err=True)
            click.echo(f"Details: {e}", err=True)
            click.echo("Use --output to specify the extraction directory.", err=True)
            sys.exit(1)

    # Validate output parameter
    if not output:
        click.echo("Error: --output parameter is required (or use 'vast init' to set default)", err=True)
        click.echo("Use --help for usage information", err=True)
        sys.exit(1)

    # Create output directory
    os.makedirs(output, exist_ok=True)

    try:
        archive_path = os.path.abspath(archive)
        click.echo(f"Importing results from: {archive_path}")
        click.echo(f"Extracting to: {output}")

        # Validate the archive
        click.echo(f"Validating archive...")
        try:
            with tarfile.open(archive_path, 'r:gz') as tar:
                # Get the list of members to check structure
                members = tar.getnames()
                if not members:
                    click.echo("Error: Archive is empty", err=True)
                    sys.exit(1)

                # Extract run ID from archive contents (should be run-<ID>)
                top_level_dirs = set()
                for member in members:
                    parts = member.split('/')
                    if parts:
                        top_level_dirs.add(parts[0])

                if len(top_level_dirs) != 1:
                    click.echo(f"Warning: Archive contains multiple top-level directories: {top_level_dirs}")

                run_id = list(top_level_dirs)[0] if top_level_dirs else None
                if run_id and not run_id.startswith('run-'):
                    click.echo(f"Warning: Archive does not contain a standard run directory (expected 'run-*', found '{run_id}')")

            click.echo(f"Archive validation successful")
        except (tarfile.TarError, OSError) as e:
            click.echo(f"Error: Archive validation failed: {e}", err=True)
            sys.exit(1)

        # Check if run directory already exists
        if run_id:
            run_output_dir = os.path.join(output, run_id)
            if os.path.exists(run_output_dir):
                if not force:
                    click.echo(f"Error: Run directory already exists: {run_output_dir}", err=True)
                    click.echo(f"Use --force to overwrite existing run", err=True)
                    sys.exit(1)
                else:
                    click.echo(f"Removing existing run directory...")
                    shutil.rmtree(run_output_dir)

        # Extract the archive
        click.echo(f"Extracting archive...")
        with tarfile.open(archive_path, 'r:gz') as tar:
            tar.extractall(path=output)

        click.echo(f"Successfully extracted to: {output}")

        # Reset preprocessing cache if we have project config
        if project_config:
            reset_preprocessing_cache(project_config.config_path, output)
            click.echo(f"Preprocessing cache reset")

        click.echo(f"Import completed successfully!")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


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
