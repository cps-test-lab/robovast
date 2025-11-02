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

"""CLI plugin for execution management."""

import os
import sys
import tempfile

import click

from robovast.common import prepare_run_configs
from robovast.common.cli import get_project_config
from robovast.execution.cluster_execution.cluster_execution import JobRunner
from robovast.execution.cluster_execution.download_results import \
    ResultDownloader

from .execute_local import (execute_docker_container, get_commandline,
                            initialize_local_execution)


@click.group()
def execution():
    """Execute scenarios locally or on a cluster.
    
    Run scenario variations either locally using Docker or on a
    Kubernetes cluster for distributed execution.
    """


@execution.group()
def local():
    """Execute scenarios locally using Docker.
    
    Run scenario variants in Docker containers with bind mounts
    for configuration and output data.
    
    Requires project initialization with 'vast init' first.
    """


@local.command()
@click.argument('variant')
@click.option('--debug', '-d', is_flag=True,
              help='Enable debug output')
@click.option('--shell', '-s', is_flag=True,
              help='Instead of running the scenario, login with shell')
def run(variant, debug, shell):
    """Execute a scenario variant locally using Docker.
    
    Runs a single variant in a Docker container with bind mounts
    for configuration and output data.
    """
    config, output, docker_image, variant_configs = initialize_local_execution(variant, debug)
    
    click.echo(f"Executing variant '{variant}' from {config}...")
    click.echo(f"Output directory: {output}")

    os.makedirs(output, exist_ok=True)
    click.echo("-" * 60)

    try:
        temp_path = tempfile.TemporaryDirectory(prefix="robovast_local_", delete=not debug)
        if debug:
            click.echo(f"Temp path: {temp_path.name}")
        
        prepare_run_configs(variant, variant_configs, temp_path.name)
        config_path = os.path.join(temp_path.name, "config", variant, variant)
        
        click.echo(f"Config path: {config_path}")
        return_code = execute_docker_container(
            docker_image, config_path, temp_path.name, output, variant, shell=shell
        )
        sys.exit(return_code)

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@local.command()
@click.argument('variant')
@click.argument('output-dir')
@click.option('--debug', '-d', is_flag=True,
              help='Enable debug output')
def prepare_run(variant, output_dir, debug):
    """Prepare run configuration and print Docker command.
    
    Prepares all necessary configuration files for a variant
    and prints the Docker command that can be used to execute it manually.
    Files are written to OUTPUT-DIR for inspection or manual execution.
    """
    config, output, docker_image, variant_configs = initialize_local_execution(variant, debug)
    
    click.echo(f"Preparing variant '{variant}' from {config}...")
    click.echo(f"Output directory: {output_dir}")

    # Create the output directory
    os.makedirs(output_dir, exist_ok=True)
    click.echo("-" * 60)

    try:
        # Prepare the run configuration files in the output directory
        prepare_run_configs(variant, variant_configs, output_dir)
        config_path = os.path.join(output_dir, "config", variant, variant)
        
        click.echo(f"Config path: {config_path}")
        click.echo(f"Configuration files prepared in: {output_dir}")
        click.echo("-" * 60)
        
        # Get the Docker command line
        docker_cmd = get_commandline(docker_image, config_path, output_dir, variant, run_num=0, shell=False)
        
        click.echo("\nDocker command to run:")
        click.echo("-" * 60)
        click.echo(' '.join(docker_cmd))
        click.echo("-" * 60)
        click.echo("\nYou can now execute this command manually or inspect the configuration files.")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@execution.command()
@click.option('--variant', '-v', default=None,
              help='Run only a specific variant by name')
def cluster(variant):
    """Execute scenarios on a Kubernetes cluster.
    
    Deploys all variants (or a specific variant) as Kubernetes jobs
    for distributed parallel execution.
    
    Requires project initialization with 'vast init' first.
    """
    # Get project configuration
    project_config = get_project_config()
    config = project_config.config_path
    
    try:
        job_runner = JobRunner(config, variant)
        job_runner.run()
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@execution.command()
@click.option('--output', '-o', default=None,
              help='Directory where all runs will be downloaded (uses project results dir if not specified)')
@click.option('--force', '-f', is_flag=True,
              help='Force re-download even if files already exist locally')
def download(output, force):
    """Download result files from cluster transfer PVC.
    
    Downloads all test run results from the Kubernetes cluster's transfer PVC
    using HTTP server port-forwarding. Files are downloaded as compressed archives,
    validated, and extracted locally.
    
    Downloads can be resumed if interrupted. Use --force to re-download existing files.
    
    Requires project initialization with 'vast init' first (unless --output is specified).
    
    Examples:
      vast execution download
      vast execution download --output ./custom_results
      vast execution download --force
    """
    # Get output directory
    if output is None:
        # Get from project configuration
        project_config = get_project_config()
        output = project_config.results_dir
    
    # Validate output parameter
    if not output:
        click.echo("Error: --output parameter is required (or use 'vast init' to set default)", err=True)
        click.echo("Use --help for usage information", err=True)
        sys.exit(1)
    
    try:
        downloader = ResultDownloader()
        
        # Download all runs
        downloader.download_results(output, force)
        click.echo("### Download completed successfully!")
        
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
