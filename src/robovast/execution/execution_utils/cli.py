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

import click
import os
import sys
import tempfile
from pprint import pprint

from robovast.common import (load_config, get_execution_variants, prepare_run_configs)
from robovast.common.cli.project_config import get_project_config
from .execute_local import execute_docker_container, get_docker_image_from_yaml
from robovast.execution.cluster_execution.cluster_execution import JobRunner
from robovast.execution.cluster_execution.download_results import ResultDownloader

@click.group()
def execution():
    """Execute scenarios locally or on a cluster.
    
    Run scenario variations either locally using Docker or on a
    Kubernetes cluster for distributed execution.
    """
    pass


@execution.command()
@click.option('--variant', '-v', required=True,
              help='Variant to execute')
@click.option('--debug', '-d', is_flag=True,
              help='Enable debug output')
@click.option('--shell', '-s', is_flag=True,
              help='Instead of running the scenario, login with shell')
def local(variant, debug, shell):
    """Execute a scenario variant locally using Docker.
    
    Runs a single variant in a Docker container with bind mounts
    for configuration and output data.
    
    Requires project initialization with 'vast init' first.
    """
    # Get project configuration
    project_config = get_project_config()
    config = project_config.config_path
    output = project_config.results_dir
    
    execution_parameters = load_config(config, "execution")
    yaml_path = os.path.join(os.path.dirname(config), execution_parameters["kubernetes_manifest"])
    
    if not os.path.exists(yaml_path):
        click.echo(f"Error: Kubernetes template not found: {yaml_path}", err=True)
        sys.exit(1)
    
    docker_image = get_docker_image_from_yaml(yaml_path)
    if not docker_image:
        click.echo("Error: Could not extract Docker image from YAML file", err=True)
        sys.exit(1)
    
    click.echo(f"Docker image: {docker_image}")
    click.echo("-" * 60)

    variants = get_execution_variants(config)

    if variant not in variants:
        click.echo(f"Error: variant '{variant}' not found in config.", err=True)
        click.echo("Available variants:")
        for v in variants:
            click.echo(f"  - {v}")
        sys.exit(1)

    variant_configs = {variant: variants[variant]}
    
    click.echo(f"Executing variant '{variant}' from {config}...")
    click.echo(f"Output directory: {output}")

    os.makedirs(output, exist_ok=True)

    if debug:
        click.echo("Variants:")
        pprint(variant_configs)
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
