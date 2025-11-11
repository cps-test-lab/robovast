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

import click
import yaml

from robovast.common import prepare_run_configs, reset_preprocessing_cache
from robovast.common.cli import get_project_config
from robovast.common.kubernetes import check_pod_running, get_kubernetes_client
from robovast.execution.cluster_execution.cluster_execution import JobRunner
from robovast.execution.cluster_execution.download_results import \
    ResultDownloader
from robovast.execution.cluster_execution.setup import (
    delete_server, get_cluster_config, load_cluster_config_name, setup_server)

from .execute_local import initialize_local_execution


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

    Requires project initialization with ``vast init`` first.
    """


@local.command()
@click.option('--variant', '-v', default=None,
              help='Run only a specific variant by name')
@click.option('--runs', '-r', type=int, default=None,
              help='Override the number of runs specified in the config')
@click.option('--output', '-o', default=None,
              help='Output directory (uses project results dir if not specified)')
@click.option('--debug', '-d', is_flag=True,
              help='Enable debug output')
@click.option('--shell', '-s', is_flag=True,
              help='Instead of running the scenario, login with shell')
@click.option('--no-gui',  is_flag=True,
              help='Disable host GUI support')
@click.option('--network-host',  is_flag=True,
              help='Use host network mode')
@click.option('--image', '-i', default='ghcr.io/cps-test-lab/robovast:latest',
              help='Use a custom Docker image')
def run(variant, runs, output, debug, shell, no_gui, network_host, image):
    """Execute scenario variants locally using Docker.

    Runs scenario variants in Docker containers with bind mounts for configuration
    and output data. By default, runs all variants from the project configuration.
    GUI support is enabled by default (requires X11 server on host).

    Prerequisites:
    - Docker must be installed and running
    - Project initialized with ``vast init``
    - X11 server running on host (for GUI support, disable with ``--no-gui``)

    Output:
        Results are written to the project results directory by default,
        or to a custom directory specified with ``--output``.
    """
    try:
        run_script_path = initialize_local_execution(
            variant, None, runs, debug=debug, feedback_callback=click.echo
        )

        # Build command with options
        cmd = [run_script_path]
        if shell:
            cmd.append("--shell")
        if no_gui:
            cmd.append("--no-gui")
        if network_host:
            cmd.append("--network-host")
        if output:
            os.makedirs(output, exist_ok=True)
            cmd.extend(["--output", os.path.abspath(output)])
        if image != 'ghcr.io/cps-test-lab/robovast:latest':
            cmd.extend(["--image", image])

        click.echo(f"\nExecuting run script: {run_script_path}")
        click.echo("=" * 60 + "\n")

        # Use exec to replace current process for proper signal handling
        os.execv(run_script_path, cmd)

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@local.command()
@click.argument('output-dir', type=click.Path())
@click.option('--variant', '-v', default=None,
              help='Run only a specific variant by name')
@click.option('--runs', '-r', type=int, default=None,
              help='Override the number of runs specified in the config')
@click.option('--debug', '-d', is_flag=True,
              help='Enable debug output')
def prepare_run(output_dir, variant, runs, debug):
    """Prepare run configuration without executing.

    Generates all necessary configuration files and a ``run.sh`` script for
    manual execution. This is useful for inspecting the generated configuration,
    debugging, or executing scenarios with custom modifications.

    This command does NOT execute the scenario - it only prepares the files.
    Use ``vast execution local run`` for immediate execution.

    Prerequisites:
    - Project initialized with ``vast init``

    Generated files in OUTPUT-DIR:
    - config/: Directory containing all scenario configuration files
    - run.sh: Executable shell script to run the scenario with Docker
    - Various temporary configuration files for the execution

    After preparation, inspect the files in OUTPUT-DIR and execute manually ``cd OUTPUT-DIR; ./run.sh``.

    The run.sh script supports the same options as ``vast execution local run``
    (--shell, --no-gui, --network-host, --output, --image).
    """
    try:
        initialize_local_execution(
            variant, output_dir, runs, debug=debug, feedback_callback=click.echo
        )

        click.echo("-" * 60)
        click.echo(f"\nFor local execution, run: \n\n{os.path.join(output_dir, 'run.sh')}\n")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@execution.group()
def cluster():
    """Execute scenarios on a Kubernetes cluster.

    Run scenario variants as Kubernetes jobs with bind mounts
    for configuration and output data.

    Requires project initialization with ``vast init`` first.
    """


@cluster.command()
@click.option('--variant', '-v', default=None,
              help='Run only a specific variant by name')
@click.option('--runs', '-r', type=int, default=None,
              help='Override the number of runs specified in the config')
def run(variant, runs):  # pylint: disable=function-redefined
    """Execute scenarios on a Kubernetes cluster.

    Deploys all variants (or a specific variant) as Kubernetes jobs
    for distributed parallel execution.

    Requires project initialization with ``vast init`` first.
    """
    # Get project configuration
    project_config = get_project_config()
    config = project_config.config_path

    # Check if transfer pod is running
    click.echo("Checking robovast pod status...")
    k8s_client = get_kubernetes_client()
    pod_ok, pod_msg = check_pod_running(k8s_client, "robovast")
    cluster_config = None

    if pod_ok:
        try:
            config_name = load_cluster_config_name()
            if config_name:
                print(f"Auto-detected cluster config: {config_name}")
            else:
                raise ValueError(
                    "No cluster config specified and no saved config found. "
                    "Use --config <name> to select a config, or run setup first."
                )
            cluster_config = get_cluster_config(config_name)
        except Exception as e:
            pod_msg = f"Failed to get cluster config: {e}"
            pod_ok = False

    if not pod_ok:
        click.echo(f"✗ Error: {pod_msg}", err=True)
        click.echo("To set up the cluster.", err=True)
        click.echo()
        click.echo("  vast execution cluster setup <cluster-config>", err=True)
        click.echo()
        sys.exit(1)
    click.echo(f"✓ {pod_msg}")

    try:
        job_runner = JobRunner(config, variant, runs, cluster_config)
        job_runner.run()
        click.echo("### Cluster execution finished.")
        click.echo()
        click.echo("You can now download the results using:")
        click.echo()
        click.echo("  vast execution cluster download")
        click.echo()
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@cluster.command()
@click.option('--output', '-o', default=None,
              help='Directory where all runs will be downloaded (uses project results dir if not specified)')
@click.option('--force', '-f', is_flag=True,
              help='Force re-download even if files already exist locally')
def download(output, force):
    """Download result files from cluster transfer PVC.

    Downloads all test run results from the Kubernetes cluster's transfer PVC
    using HTTP server port-forwarding. Files are downloaded as compressed archives,
    validated, and extracted locally.

    Downloads can be resumed if interrupted. Use ``--force`` to re-download existing files.

    Requires project initialization with ``vast init`` first (unless ``--output`` is specified).
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
        count = downloader.download_results(output, force)
        if count > 0:
            reset_preprocessing_cache(project_config.config_path, output)
        click.echo(f"### Download of {count} runs completed successfully!")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@cluster.command()
@click.option('--list', 'list_configs', is_flag=True,
              help='List available cluster configuration plugins')
@click.option('--option', '-o', 'options', multiple=True,
              help='Cluster-specific option in key=value format (can be used multiple times)')
@click.option('--force', '-f', is_flag=True,
              help='Force re-setup even if cluster is already set up')
@click.argument('cluster_config', required=False)
def setup(list_configs, options, force, cluster_config):
    """Set up the Kubernetes cluster for execution.

    Sets up the NFS server in the Kubernetes cluster, that is
    used to provide configurations and store results created by individual
    scenario execution jobs.

    This command should be run once before executing scenarios
    on the cluster for the first time.

    If the cluster is already set up, this command will exit with an error.
    Run 'vast execution cluster cleanup' first to clean up the existing setup,
    or use ``--force`` to force re-setup.

    Use ``--list`` to see available cluster configuration plugins.

    Cluster-specific options can be passed using ``--option key=value``.
    """
    if list_configs:
        try:
            setup_server(config_name=None, list_configs=True)
            return
        except Exception as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)

    if not cluster_config:
        click.echo("Error: CLUSTER_CONFIG argument is required when not using --list", err=True)
        sys.exit(1)

    # Parse cluster-specific options
    cluster_kwargs = {}
    for option in options:
        if '=' not in option:
            click.echo(f"Error: Invalid option format '{option}'. Expected key=value", err=True)
            sys.exit(1)
        key, value = option.split('=', 1)
        cluster_kwargs[key] = value

    try:
        setup_server(config_name=cluster_config, list_configs=False, force=force, **cluster_kwargs)
        click.echo("### Cluster setup completed successfully!")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@cluster.command()
@click.option('--cluster-config', '-c', 'config_name', default=None,
              help='Cluster configuration plugin to use (auto-detects if not specified)')
def cleanup(config_name):
    """Clean up the Kubernetes cluster after execution.

    Removes the NFS server pod and service from the Kubernetes cluster
    by deleting the NFS manifest configuration.

    This command can be run after completing all scenario executions
    to clean up cluster resources.

    If --config is not specified, it will automatically detect which
    cluster configuration was used during setup.
    """
    try:
        delete_server(config_name=config_name)
        click.echo("### Cluster cleanup completed successfully!")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@cluster.command()
@click.argument('output', type=click.Path())
@click.option('--variant', '-v', default=None,
              help='Prepare only a specific variant by name')
@click.option('--runs', '-r', type=int, default=None,
              help='Override the number of runs specified in the config')
@click.option('--cluster-config', '-c', default=None,
              help='Override the cluster configuration specified in the config')
@click.option('--option', '-o', 'options', multiple=True,
              help='Cluster-specific option in key=value format (can be used multiple times)')
def prepare_run(output, variant, runs, cluster_config, options):  # pylint: disable=function-redefined
    """Prepare complete cluster execution package for offline deployment.

    Generates all necessary files for cluster execution and writes them to
    the specified output directory. This package can be transferred to a
    cluster environment for offline deployment and execution.

    The output directory will contain:
    - config/ directory with all scenario configurations
    - jobs/ directory with individual job manifest YAML files
    - ``all-jobs.yaml`` file with all jobs combined
    - ``copy_configs.py`` script to upload test configurations to the cluster
    - README.md with general execution instructions
    - Cluster-specific setup files (manifests, templates, README)

    The generated package is self-contained and can be used to:
    1. Set up the cluster infrastructure (NFS server, PVCs)
    2. Upload configuration files to the cluster
    3. Deploy and execute all scenario jobs

    Cluster-specific options can be passed using --option key=value.

    Requires project initialization with ``vast init`` first.
    """
    # Get project configuration
    project_config = get_project_config()
    config_path = project_config.config_path

    # Create output directory
    os.makedirs(output, exist_ok=True)

    # Parse cluster-specific options
    cluster_kwargs = {}
    for option in options:
        if '=' not in option:
            click.echo(f"Error: Invalid option format '{option}'. Expected key=value", err=True)
            sys.exit(1)
        key, value = option.split('=', 1)
        cluster_kwargs[key] = value

    if cluster_config is None:
        cluster_config = load_cluster_config_name()
        if cluster_config:
            print(f"Auto-detected cluster config: {cluster_config}")
        else:
            raise ValueError(
                "No cluster config specified and no saved config found. "
                "Use --cluster-config <name> to select a config, or run setup first."
            )
    try:
        cluster_config = get_cluster_config(cluster_config)
    except Exception as e:
        raise RuntimeError(f"Failed to get cluster config: {e}") from e

    # Initialize job runner (this prepares all scenarios)
    job_runner = JobRunner(config_path, variant, runs, cluster_config)

    click.echo(f"### Preparing run configuration (ID: {job_runner.run_id})")
    click.echo(f"### Variants: {len(job_runner.variants)}")
    click.echo(f"### Runs per variant: {job_runner.num_runs}")
    click.echo(f"### Total jobs: {len(job_runner.variants) * job_runner.num_runs}")

    # Prepare config files
    click.echo("### Preparing configuration files...")

    prepare_run_configs(
        job_runner.run_id,
        job_runner.variants,
        output
    )

    # Create jobs directory
    jobs_dir = os.path.join(output, "jobs")
    os.makedirs(jobs_dir, exist_ok=True)

    # Generate all job manifests
    click.echo("### Generating job manifests...")
    all_jobs = []
    job_count = 0

    for run_number in range(job_runner.num_runs):
        for variant_key in job_runner.variants:
            # Use the centralized function to create the job manifest
            job_manifest = job_runner.create_job_manifest_for_scenario(variant_key, run_number)

            # Save individual job manifest
            job_name = job_manifest['metadata']['name']
            job_file = os.path.join(jobs_dir, f"{job_name}.yaml")
            with open(job_file, 'w') as f:
                yaml.dump(job_manifest, f, default_flow_style=False)

            all_jobs.append(job_manifest)
            job_count += 1

    # Save combined manifest
    combined_file = os.path.join(output, "all-jobs.yaml")
    with open(combined_file, 'w') as f:
        yaml.dump_all(all_jobs, f, default_flow_style=False)

    cluster_config.prepare_setup_cluster(output, **cluster_kwargs)

    generate_copy_script(output, job_runner.run_id)

    click.echo(f"### Successfully prepared {job_count} job manifests")
    click.echo(f"### Configuration files: {output}/config/")
    click.echo(f"### Individual job files: {output}/jobs/")
    click.echo(f"### Combined manifest: {output}/all-jobs.yaml")
    click.echo(f"### Copy script: {output}/copy_configs.py")


def generate_copy_script(output_dir, run_id):
    """Generate a Python script to copy configuration files to the cluster."""
    script_content = f'''#!/usr/bin/env python3
"""
Script to copy configuration files to the Kubernetes cluster.

This script uploads the prepared configuration files to the robovast pod
in the Kubernetes cluster for use by scenario execution jobs.

Generated by: vast execution cluster prepare-run
Run ID: {run_id}
"""

import os
import subprocess
import sys
from robovast.common.kubernetes import copy_config_to_cluster


def main():
    """Main function to copy configs to cluster."""
    # Get the directory where this script is located
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_dir = os.path.join(script_dir, "config")
    run_id = "{run_id}"

    if not os.path.exists(config_dir):
        print(f"ERROR: Config directory not found: {{config_dir}}")
        sys.exit(1)

    print(f"### Copying config files to transfer pod using kubectl cp...")
    print(f"### Source: {{config_dir}}")
    print(f"### Destination: robovast pod at /exports/")

    copy_config_to_cluster(config_dir, run_id)


if __name__ == "__main__":
    main()
'''

    # Write the script to the output directory
    script_path = os.path.join(output_dir, "copy_configs.py")
    with open(script_path, 'w') as f:
        f.write(script_content)

    # Make the script executable
    os.chmod(script_path, 0o755)

    # Create README with setup instructions
    readme_content = """# Execution Instructions
This directory contains the necessary manifests to set up the RoboVAST execution environment on a cluster.

### 1. Setup transfer mechanism

Before uploading configuration files, ensure that the NFS server is running and accessible from the job pods.

Follow README_<CLUSTER CONFIG>.md for cluster-specific setup instructions.

### 2. Upload Configuration Files

After the cluster setup is complete, upload the configuration files using the provided script:

```bash
./copy_configs.py
```

### 3. Deploy Jobs

Deploy the scenario execution jobs:

```bash
kubectl apply -f all-jobs.yaml
```
"""
    with open(f"{output_dir}/README.md", "w") as f:
        f.write(readme_content)
