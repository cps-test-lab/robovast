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

import logging
import os
import sys
import time

import click
import yaml

from robovast.common import prepare_run_configs
from robovast.common.cli import get_project_config, handle_cli_exception
from robovast.common.cluster_context import (get_active_kube_context, get_config_context_names,
                                             require_context_for_multi_cluster)
from robovast.execution.cluster_execution.cluster_execution import (
    JobRunner, cleanup_cluster_run, get_cluster_run_job_counts_per_run,
    _label_safe_run_id)
from robovast.execution.cluster_execution.cluster_setup import (
    delete_server, get_cluster_config, get_cluster_namespace,
    load_cluster_config_name, setup_server)
from robovast.execution.cluster_execution.download_results import \
    ResultDownloader

from ..cluster_execution.kubernetes import (check_kubernetes_access,
                                            check_pod_running,
                                            get_kubernetes_client)
from .execute_local import initialize_local_execution


@click.group()
def execution():
    """Execute scenarios locally or on a cluster.

    Run scenario configurations either locally using Docker or on a
    Kubernetes cluster for distributed execution.
    """


@execution.group()
def local():
    """Execute scenarios locally using Docker.

    Run test configurations in Docker containers with bind mounts
    for configuration and output data.

    Requires project initialization with ``vast init`` first.
    """


@local.command()
@click.option('--config', '-c', default=None,
              help='Run only a specific configuration by name')
@click.option('--runs', '-r', type=int, default=None,
              help='Override the number of runs specified in the config')
@click.option('--output', '-o', default=None,
              help='Output directory (uses project results dir if not specified)')
@click.option('--start-only', is_flag=True,
              help='Start the robovast container with a shell, skipping the entrypoint script')
@click.option('--no-gui',  is_flag=True,
              help='Disable host GUI support')
@click.option('--network-host',  is_flag=True,
              help='Use host network mode')
@click.option('--image', '-i', default='ghcr.io/cps-test-lab/robovast:latest',
              help='Use a custom Docker image')
@click.option('--abort-on-failure', is_flag=True,
              help='Stop execution after the first failed test config (default: continue)')
@click.option('--use-resource-allocation', is_flag=True,
              help='Add CPU/memory reservations to docker compose run (default: skip for local)')
@click.option('--log-tree', '-t', is_flag=True,
              help='Log scenario execution live tree')
def run(config, runs, output, start_only, no_gui, network_host, image, abort_on_failure,
        use_resource_allocation, log_tree):
    """Execute scenario configurations locally using Docker.

    Runs scenario configurations in Docker containers with bind mounts for configuration
    and output data. By default, runs all configurations from the project configuration
    and continues past failures. Use ``--abort-on-failure`` to stop at the first failure.
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
            config, None, runs, feedback_callback=click.echo,
            skip_resource_allocation=not use_resource_allocation,
            log_tree=log_tree
        )

        # Build command with options
        cmd = [run_script_path]
        if start_only:
            cmd.append("--start-only")
        if no_gui:
            cmd.append("--no-gui")
        if network_host:
            cmd.append("--network-host")
        if output:
            os.makedirs(output, exist_ok=True)
            cmd.extend(["--results-dir", os.path.abspath(output)])
        if image != 'ghcr.io/cps-test-lab/robovast:latest':
            cmd.extend(["--image", image])
        if abort_on_failure:
            cmd.append("--abort-on-failure")
        if log_tree:
            cmd.append("-t")

        logging.debug(f"Executing run script: {run_script_path}")

        # Use exec to replace current process for proper signal handling
        os.execv(run_script_path, cmd)

    except Exception as e:
        handle_cli_exception(e)


@local.command()
@click.argument('output-dir', type=click.Path())
@click.option('--config', '-c', default=None,
              help='Run only a specific configuration by name')
@click.option('--runs', '-r', type=int, default=None,
              help='Override the number of runs specified in the config')
@click.option('--use-resource-allocation', is_flag=True,
              help='Add CPU/memory reservations to docker compose run (default: skip for local)')
@click.option('--log-tree', '-t', is_flag=True,
              help='Log scenario execution live tree')
def prepare_run(output_dir, config, runs, use_resource_allocation, log_tree):
    """Prepare run without executing.

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
    (--start-only, --no-gui, --network-host, --output, --image, --abort-on-failure,
    --log-tree).
    """
    try:
        initialize_local_execution(
            config, output_dir, runs, feedback_callback=click.echo,
            skip_resource_allocation=not use_resource_allocation,
            log_tree=log_tree
        )

        click.echo(f"\nFor local execution, run: \n\n{os.path.join(output_dir, 'run.sh')}\n")

    except Exception as e:
        handle_cli_exception(e)


@execution.group()
def cluster():
    """Execute scenarios on a Kubernetes cluster.

    Run scenario configurations as Kubernetes jobs with bind mounts
    for configuration and output data.

    Requires project initialization with ``vast init`` first.
    """


@cluster.command()
@click.option('--config', '-c', default=None,
              help='Run only a specific configuration by name')
@click.option('--runs', '-r', type=int, default=None,
              help='Override the number of runs specified in the config')
@click.option('--follow/--no-follow', '-f/ ', default=False,
              help='Follow job execution and wait for completion (default: exit immediately after creating jobs)')
@click.option('--cleanup', is_flag=True,
              help='Clean up previous runs before starting (default: do not cleanup; allows multiple parallel runs)')
@click.option('--log-tree', '-t', is_flag=True,
              help='Log scenario execution live tree')
@click.option('--context', '-x', 'kube_context', default=None,
              help='Kubernetes context to use (default: active context in kubeconfig)')
def run(config, runs, follow, cleanup, log_tree, kube_context):  # pylint: disable=function-redefined,redefined-outer-name
    """Execute scenarios on a Kubernetes cluster.

    Deploys all test configurations (or a specific one) as Kubernetes jobs
    for distributed parallel execution.

    By default, exits immediately after creating jobs.
    Use --follow to wait for all jobs to complete before returning.
    Use --cleanup to remove previous runs before starting (by default,
    previous runs are left intact so multiple runs can run in parallel).
    Use 'vast execution cluster run-cleanup' to clean up jobs afterwards.
    Use --context to target a specific Kubernetes cluster.

    Requires project initialization with ``vast init`` first.
    """
    try:
        require_context_for_multi_cluster(kube_context)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    context_key = kube_context
    # Get project configuration
    project_config = get_project_config()

    # Check Kubernetes access (namespace-scoped so RBAC namespace-only users succeed)
    k8s_client = get_kubernetes_client(context=kube_context)
    namespace = get_cluster_namespace(context_key)
    click.echo("Checking Kubernetes cluster access...")
    k8s_ok, k8s_msg = check_kubernetes_access(k8s_client, namespace=namespace)
    if not k8s_ok:
        click.echo(f"✗ Error: {k8s_msg}", err=True)
        click.echo("  Kubernetes cluster is required for RoboVAST execution.", err=True)
        sys.exit(1)
    logging.debug(k8s_msg)

    # Check if transfer pod is running
    click.echo("Checking robovast pod status...")
    pod_ok, pod_msg = check_pod_running(k8s_client, "robovast", namespace)
    cluster_config = None

    if pod_ok:
        try:
            config_name = load_cluster_config_name(context_key)
            if config_name:
                logging.debug(f"Auto-detected cluster config: {config_name}")
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
    logging.debug(pod_msg)

    try:
        job_runner = JobRunner(
            project_config.config_path, config, runs, cluster_config,
            namespace=namespace, cleanup_before_run=cleanup, log_tree=log_tree,
            kube_context=kube_context)
        job_runner.run(detached=not follow)

        if not follow:
            click.echo(f"✓ Jobs created successfully (Run ID: {job_runner.run_id})")
            click.echo()
            click.echo("Jobs are now running in detached mode.")
            click.echo()
            click.echo("To check job status, use: vast execution cluster monitor")
            click.echo("To clean up jobs, use: vast execution cluster run-cleanup")
            click.echo()
        else:
            click.echo("Cluster execution finished.")
            click.echo()
            click.echo("You can now download the results using:")
            click.echo()
            click.echo("  vast execution cluster download")
            click.echo()
    except Exception as e:
        handle_cli_exception(e)


@cluster.command()
@click.option('--interval', '-i', type=float, default=2.0, show_default=True,
              help='Polling interval in seconds')
@click.option('--once', is_flag=True,
              help='Print job status once and exit')
@click.option('--context', '-x', 'kube_context', default=None,
              help='Kubernetes context to use (default: active context in kubeconfig)')
def monitor(interval, once, kube_context):
    """Monitor scenario execution jobs on the cluster.

    Displays progress per run: how many jobs have finished (completed or failed),
    how many are running, and how many are pending for each run.

    By default, monitors only the contexts referenced in the .vast config file.
    Falls back to the active kubeconfig context when no per-cluster config is
    defined. Use --context to restrict monitoring to a single cluster.
    Only contexts with active or past jobs are shown.

    This is intended for monitoring jobs created by
    ``vast execution cluster run``.
    """
    try:
        cursor_up = "\033[A"
        clear_line = "\033[2K"
        bar_width = 20
        pct_width = 7

        # Build list of (label, kube_context_name) to monitor
        if not kube_context:
            # Use contexts referenced in the .vast config file
            try:
                from robovast.common.cli.project_config import ProjectConfig  # pylint: disable=import-outside-toplevel
                pc = ProjectConfig.load()
                config_path = pc.config_path if pc else None
            except Exception:
                config_path = None

            config_names = get_config_context_names(config_path) if config_path else set()
            if config_names:
                contexts_to_monitor = sorted((n, n) for n in config_names)
            else:
                # No per-cluster config — fall back to active context
                active = get_active_kube_context()
                contexts_to_monitor = [(active or "(active)", active)]
            namespace = get_cluster_namespace()
        else:
            context_key = kube_context
            namespace = get_cluster_namespace(context_key)
            contexts_to_monitor = [(kube_context, kube_context)]

        multi = len(contexts_to_monitor) > 1

        # Per-context state (keyed by kube_context_name)
        initial_total: dict[str, dict] = {}   # ctx -> {run_id: total}
        max_ok: dict[str, dict] = {}          # ctx -> {run_id: max_ok}
        max_fail: dict[str, dict] = {}        # ctx -> {run_id: max_fail}
        last_per_run: dict[str, dict] = {}    # ctx -> last known per_run
        prev_line_count = [0]

        def _build_run_lines(label, ctx, per_run):
            """Return (lines, all_done) for a single context."""
            ctx_initial = initial_total.setdefault(ctx, {})
            ctx_ok = max_ok.setdefault(ctx, {})
            ctx_fail = max_fail.setdefault(ctx, {})

            all_run_ids = sorted(set(ctx_initial.keys()) | set(per_run.keys()))
            lines = []
            all_done = True
            indent = "  " if multi else ""

            for run_id in all_run_ids:
                c = per_run.get(run_id, {"completed": 0, "failed": 0, "running": 0, "pending": 0})
                current_total = c["completed"] + c["failed"] + c["running"] + c["pending"]
                if run_id not in ctx_initial:
                    ctx_initial[run_id] = current_total
                total = ctx_initial[run_id]
                ctx_ok[run_id] = max(ctx_ok.get(run_id, 0), c["completed"])
                ctx_fail[run_id] = max(ctx_fail.get(run_id, 0), c["failed"])
                still_in_cluster = c["running"] + c["pending"]
                finished = total - still_in_cluster if total > 0 else 0
                if still_in_cluster > 0:
                    all_done = False
                ok = ctx_ok[run_id]
                fail = ctx_fail[run_id]
                remainder = finished - ok - fail
                if remainder > 0:
                    ok += remainder
                pct = 100.0 * finished / total if total > 0 else 100.0
                filled = int(bar_width * finished / total) if total > 0 else bar_width
                progress_bar = "█" * filled + "░" * (bar_width - filled)
                pct_str = f"{pct:.1f}%".rjust(pct_width)
                lines.append(
                    f"{indent}{run_id}  [{progress_bar}]  {pct_str}  "
                    f"{finished}/{total}  ({ok} ok, {fail} fail)  "
                    f"Running: {c['running']}  Pending: {c['pending']}"
                )
            if not lines:
                lines.append(f"{indent}No scenario run jobs found.")
            return lines, all_done

        def _print_status_lines():
            all_lines = []
            everything_done = True
            for label, ctx in contexts_to_monitor:
                unreachable = False
                try:
                    _urllib3_logger = logging.getLogger("urllib3")
                    _prev_level = _urllib3_logger.level
                    _urllib3_logger.setLevel(logging.ERROR)
                    try:
                        per_run = get_cluster_run_job_counts_per_run(namespace, context=ctx)
                    finally:
                        _urllib3_logger.setLevel(_prev_level)
                except Exception as exc:
                    # Keep displaying even if one context is unreachable
                    per_run = {}
                    unreachable = True
                    logging.debug(f"Could not query context {ctx!r}: {exc}")
                # Use last known data when unreachable so bars stay meaningful
                if unreachable and ctx in last_per_run:
                    per_run = last_per_run[ctx]
                elif not unreachable:
                    last_per_run[ctx] = per_run
                # Skip contexts that have no jobs at all (and never had any)
                if not per_run and ctx not in initial_total:
                    if unreachable:
                        indent = "  " if multi else ""
                        if multi:
                            all_lines.append(f"[{label}]")
                        all_lines.append(f"{indent}(unreachable)")
                        everything_done = False
                    continue
                if multi:
                    ctx_label_str = f"[{label}]" + (" (unreachable)" if unreachable else "")
                    all_lines.append(ctx_label_str)
                elif unreachable:
                    all_lines.append("(unreachable - showing last known state)")
                run_lines, done = _build_run_lines(label, ctx, per_run)
                all_lines.extend(run_lines)
                if not done:
                    everything_done = False

            # Erase previous output and redraw
            for _ in range(prev_line_count[0]):
                sys.stdout.write(cursor_up)
            for line in all_lines:
                sys.stdout.write("\r" + clear_line + line + "\n")
            for _ in range(len(all_lines), prev_line_count[0]):
                sys.stdout.write("\r" + clear_line + "\n")
            prev_line_count[0] = len(all_lines)
            sys.stdout.flush()
            return everything_done

        if once:
            _print_status_lines()
            return

        ctx_label = "configured contexts" if multi else f"context '{contexts_to_monitor[0][0]}'"
        click.echo(f"Monitoring scenario run jobs on {ctx_label} (press Ctrl+C to stop)...")
        sys.stdout.write("\n")
        sys.stdout.flush()

        while True:
            all_done = _print_status_lines()
            if all_done:
                sys.stdout.write("\n")
                sys.stdout.flush()
                click.echo("All jobs finished.")
                break
            time.sleep(interval)

    except KeyboardInterrupt:
        sys.stdout.write("\n")
        sys.stdout.flush()
    except Exception as e:
        handle_cli_exception(e)


@cluster.command()
@click.option('--output', '-o', default=None,
              help='Directory where all runs will be downloaded (uses project results dir if not specified)')
@click.option('--force', '-f', is_flag=True,
              help='Force re-download even if files already exist locally')
@click.option('--verbose', '-v', is_flag=True,
              help='Print per-file progress instead of a single-line progress bar per run')
@click.option('--skip-removal', is_flag=True,
              help='Do not remove remote archive or delete S3 bucket after download')
@click.option('--port-forward-only', is_flag=True,
              help='Only start port-forward and print URLs; do not download (Ctrl+C to stop)')
@click.option('--remote-compress-only', is_flag=True,
              help='Only create .tar.gz archives on the remote pod; do not download')
@click.option('--no-keep-archive', is_flag=True,
              help='Remove the local .tar.gz file after extraction (default: keep it)')
@click.option('--context', '-x', 'kube_context', default=None,
              help='Kubernetes context to use (default: active context in kubeconfig)')
def download(output, force, verbose, skip_removal, port_forward_only, remote_compress_only,
             no_keep_archive, kube_context):
    """Download result files from the cluster S3 (MinIO) server.

    Downloads all test run results from the MinIO S3 server embedded in the
    robovast pod. Each run is stored in a separate S3 bucket (``run-*``) and
    downloaded into a subdirectory of the output directory.

    By default a single progress bar line is shown for each run. Use
    ``--verbose`` to print individual file names instead.

    Use ``--force`` to re-download runs that already exist locally.

    Use ``--skip-removal`` to keep the remote archive and S3 bucket after download.

    Use ``--port-forward-only`` to start a port-forward and print HTTP URLs for
    manual download (e.g. with curl). Press Ctrl+C to stop.

    Use ``--remote-compress-only`` to create compressed archives on the remote
    pod without downloading. Useful to pre-compress before downloading later
    via ``--port-forward-only`` or a full download run.

    By default the downloaded .tar.gz is kept after extraction; use
    ``--no-keep-archive`` to remove it to save space.

    Requires project initialization with ``vast init`` first (unless ``--output``
    is specified, or when using ``--port-forward-only`` or ``--remote-compress-only``).
    """
    if port_forward_only and remote_compress_only:
        click.echo("Error: --port-forward-only and --remote-compress-only are mutually exclusive", err=True)
        sys.exit(1)

    try:
        require_context_for_multi_cluster(kube_context)
        context_key = kube_context
        config_name = load_cluster_config_name(context_key)
        cluster_config = get_cluster_config(config_name)
        downloader = ResultDownloader(namespace=get_cluster_namespace(context_key), cluster_config=cluster_config,
                                      context=kube_context)

        if port_forward_only:
            downloader.port_forward_only()
            return

        if remote_compress_only:
            count = downloader.remote_compress_only(force=force, verbose=verbose)
            click.echo(f"✓ Compressed {count} runs on remote pod.")
            return

        # Full download
        if output is None:
            project_config = get_project_config()
            output = project_config.results_dir

        if not output:
            click.echo("Error: --output parameter is required (or use 'vast init' to set default)", err=True)
            click.echo("Use --help for usage information", err=True)
            sys.exit(1)

        count = downloader.download_results(
            output, force, verbose=verbose, skip_removal=skip_removal,
            keep_archive=not no_keep_archive
        )
        click.echo(f"✓ Download of {count} runs completed successfully!")

    except Exception as e:
        handle_cli_exception(e)


@cluster.command()
@click.option('--list', 'list_configs', is_flag=True,
              help='List available cluster configuration plugins')
@click.option('--namespace', '-n', default='default', show_default=True,
              help='Kubernetes namespace for execution (used by cluster run)')
@click.option('--option', '-o', 'options', multiple=True,
              help='Cluster-specific option in key=value format (can be used multiple times)')
@click.option('--force', '-f', is_flag=True,
              help='Force re-setup even if cluster is already set up')
@click.option('--context', '-x', 'kube_context', default=None,
              help='Kubernetes context to use (default: active context in kubeconfig)')
@click.argument('cluster_config', required=False)
def setup(list_configs, namespace, options, force, kube_context, cluster_config):
    """Set up the Kubernetes cluster for execution.

    Deploys a MinIO S3 server in the Kubernetes cluster. The server is used
    to store run configurations and results for individual scenario execution jobs.

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
            handle_cli_exception(e)

    if not cluster_config:
        click.echo("Error: CLUSTER_CONFIG argument is required when not using --list", err=True)
        sys.exit(1)

    try:
        require_context_for_multi_cluster(kube_context)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    # Parse cluster-specific options
    cluster_kwargs = {"namespace": namespace}
    if kube_context is not None:
        cluster_kwargs["kube_context"] = kube_context
    for option in options:
        if '=' not in option:
            click.echo(f"Error: Invalid option format '{option}'. Expected key=value", err=True)
            sys.exit(1)
        key, value = option.split('=', 1)
        cluster_kwargs[key] = value

    try:
        setup_server(config_name=cluster_config, list_configs=False, force=force, **cluster_kwargs)
        click.echo("✓ Cluster setup completed successfully!")

    except Exception as e:
        handle_cli_exception(e)


@cluster.command(name='download-cleanup')
@click.option('--run-id', '-i', default=None,
              help='Only remove this run\'s bucket (e.g. run-2025-02-27-123456). Without this, removes all run buckets.')
@click.option('--context', '-x', 'kube_context', default=None,
              help='Kubernetes context to use (default: active context in kubeconfig)')
def download_cleanup(run_id, kube_context):
    """Remove result buckets from cluster S3 without downloading.

    Deletes run result buckets (``run-*``) from the MinIO S3 server in the cluster.
    Does not download any data; use ``vast execution cluster download`` if you
    need the results first.

    Use --run-id to remove only a specific run's bucket.
    """
    try:
        require_context_for_multi_cluster(kube_context)
        context_key = kube_context
        config_name = load_cluster_config_name(context_key)
        cluster_config = get_cluster_config(config_name)
        downloader = ResultDownloader(namespace=get_cluster_namespace(context_key), cluster_config=cluster_config,
                                      context=kube_context)
        count = downloader.cleanup_s3_buckets(run_id=run_id)
        click.echo(f"✓ Removed {count} bucket(s) from S3.")

    except Exception as e:
        handle_cli_exception(e)


@cluster.command(name='run-cleanup')
@click.option('--run-id', '-i', default=None,
              help='Clean only jobs for this run (e.g. run-2025-02-27-123456). Without this, cleans all scenario-runs jobs.')
@click.option('--context', '-x', 'kube_context', default=None,
              help='Kubernetes context to use (default: active context in kubeconfig)')
def run_cleanup(run_id, kube_context):
    """Clean up jobs and pods from a cluster run.

    Removes scenario execution jobs and their associated pods. By default
    removes all runs. Use --run-id to clean only a specific run.

    Useful after running with --detach to clean up resources once jobs
    have completed.

    Usage: vast execution cluster run-cleanup
    Usage: vast execution cluster run-cleanup --run-id run-2025-02-27-123456
    """
    try:
        require_context_for_multi_cluster(kube_context)
        context_key = kube_context
        namespace = get_cluster_namespace(context_key)
        k8s_client = get_kubernetes_client(context=kube_context)
        click.echo("Checking Kubernetes cluster access...")
        k8s_ok, k8s_msg = check_kubernetes_access(k8s_client, namespace=namespace)
        if not k8s_ok:
            click.echo(f"✗ Error: {k8s_msg}", err=True)
            sys.exit(1)

        if run_id:
            per_run = get_cluster_run_job_counts_per_run(namespace, context=kube_context)
            label_safe = _label_safe_run_id(run_id)
            if label_safe not in per_run:
                available = sorted(per_run.keys())
                if available:
                    click.echo(f"Run '{run_id}' not found in cluster.", err=True)
                    click.echo("Available run-ids:", err=True)
                    for rid in available:
                        click.echo(f"  - {rid}", err=True)
                else:
                    click.echo("No scenario run jobs in cluster.", err=True)
                sys.exit(1)
            click.echo(f"Cleaning up jobs and pods for run '{run_id}'...")
        else:
            click.echo("Cleaning up all scenario run jobs and pods...")
        cleanup_cluster_run(namespace=namespace, run_id=run_id, context=kube_context)
        click.echo("✓ Cleanup completed successfully!")

    except Exception as e:
        handle_cli_exception(e)


@cluster.command()
@click.option('--cluster-config', '-c', 'config_name', default=None,
              help='Cluster configuration plugin to use (auto-detects if not specified)')
@click.option('--namespace', '-n', default=None,
              help='Kubernetes namespace to clean up (required when using --cluster-config without prior setup)')
@click.option('--option', '-o', 'options', multiple=True,
              help='Cluster-specific option in key=value format (can be used multiple times)')
@click.option('--context', '-x', 'kube_context', default=None,
              help='Kubernetes context to use (default: active context in kubeconfig)')
def cleanup(config_name, namespace, options, kube_context):
    """Clean up the Kubernetes cluster setup.

    Removes the NFS server pod and service from the Kubernetes cluster
    by deleting the NFS manifest configuration.

    This command can be run after completing all scenario executions
    to clean up cluster infrastructure resources (different from run-cleanup
    which only cleans up job pods).

    If ``--cluster-config`` is not specified, it will automatically detect
    which cluster configuration was used during setup (from the project flag file).
    When specifying ``--cluster-config`` explicitly, pass ``-n <namespace>`` if the
    setup was done in a non-default namespace.
    """
    try:
        require_context_for_multi_cluster(kube_context)
        cluster_kwargs = {}
        if namespace is not None:
            cluster_kwargs["namespace"] = namespace
        if kube_context is not None:
            cluster_kwargs["kube_context"] = kube_context
        for option in options:
            if '=' not in option:
                click.echo(f"Error: Invalid option format '{option}'. Expected key=value", err=True)
                sys.exit(1)
            key, value = option.split('=', 1)
            cluster_kwargs[key] = value
        delete_server(config_name=config_name, **cluster_kwargs)
        click.echo("✓ Cluster cleanup completed successfully!")

    except Exception as e:
        handle_cli_exception(e)


@cluster.command()
@click.argument('output', type=click.Path())
@click.option('--config', '-c', default=None,
              help='Prepare only a specific config by name')
@click.option('--runs', '-r', type=int, default=None,
              help='Override the number of runs specified in the config')
@click.option('--cluster-config', '-k', default=None,
              help='Override the cluster configuration specified in the config')
@click.option('--option', '-o', 'options', multiple=True,
              help='Cluster-specific option in key=value format (can be used multiple times)')
@click.option('--log-tree', '-t', is_flag=True,
              help='Log scenario execution live tree')
@click.option('--context', '-x', 'kube_context', default=None,
              help='Kubernetes context to use (default: active context in kubeconfig)')
def prepare_run(output, config, runs, cluster_config, options, log_tree, kube_context):  # pylint: disable=function-redefined
    """Prepare complete setup for manual deployment.

    Generates all necessary files for cluster execution and writes them to
    the specified output directory.

    The output directory will contain:
    - ``kueue-queue-setup.yaml`` and ``README_kueue.md`` — Kueue queue manifests and setup instructions
    - config/ directory with all scenario configurations
    - jobs/ directory with individual job manifest YAML files
    - ``all-jobs.yaml`` file with all jobs combined
    - ``upload_configs.py`` script to upload test configurations to the cluster
    - README.md with general execution instructions
    - Cluster-specific setup files (manifests, templates, README)

    The generated package is self-contained and can be used to:
    1. Set up Kueue (job queueing) — follow README_kueue.md
    2. Set up the cluster infrastructure (MinIO S3 server, PVCs)
    3. Upload configuration files to the cluster
    4. Deploy and execute all scenario jobs

    Cluster-specific options can be passed using --option key=value.

    Requires project initialization with ``vast init`` first.
    """
    try:
        require_context_for_multi_cluster(kube_context)
        context_key = kube_context
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
            cluster_config = load_cluster_config_name(context_key)
            if cluster_config:
                logging.debug(f"Auto-detected cluster config: {cluster_config}")
            else:
                raise ValueError(
                    "No cluster config specified and no saved config found. "
                    "Use --cluster-config <name> to select a config, or run setup first."
                )
        try:
            cluster_config = get_cluster_config(cluster_config)
        except Exception as e:
            raise RuntimeError(f"Failed to get cluster config: {e}") from e

        namespace = cluster_kwargs.get("namespace", get_cluster_namespace(context_key))

        # Initialize job runner (this prepares all scenarios)
        job_runner = JobRunner(
            config_path, config, runs, cluster_config,
            namespace=namespace, log_tree=log_tree,
            kube_context=kube_context)

        click.echo(f"Preparing run configuration 'ID: {job_runner.run_id}', test configs: {
                   len(job_runner.configs)}, runs per test config: {job_runner.num_runs}...")

        # Prepare config files
        logging.debug("Preparing configuration files...")

        out_dir = os.path.join(output, "out_template")
        prepare_run_configs(
            out_dir,
            job_runner.run_data,
            cluster=True
        )

        # Create jobs directory
        jobs_dir = os.path.join(output, "jobs")
        os.makedirs(jobs_dir, exist_ok=True)

        # Generate all job manifests
        logging.debug("Generating job manifests...")
        all_jobs = []
        job_count = 0

        for run_number in range(job_runner.num_runs):
            for cfg in job_runner.configs:
                config_name = cfg.get("name")
                # Use the centralized function to create the job manifest
                job_manifest = job_runner.create_job_manifest_for_scenario(config_name, run_number)

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
        from robovast.execution.cluster_execution.kubernetes_kueue import (  # pylint: disable=import-outside-toplevel
            prepare_kueue_setup,
        )
        prepare_kueue_setup(output, namespace=namespace, kube_context=kube_context)

        generate_upload_script(
            output, job_runner.run_id, namespace, cluster_config,
        )

        click.echo(f"✓ Successfully prepared {job_count} job manifests in directory'{
                   output}'.\n\nFollow README files to set up and execute.\n")

    except Exception as e:
        handle_cli_exception(e)


def generate_upload_script(output_dir, run_id, namespace="default", cluster_config=None):
    """Generate a Python script to upload configuration files to S3."""
    bucket_name = run_id.lower().replace("_", "-")
    access_key = "minioadmin"
    secret_key = "minioadmin"
    if cluster_config is not None:
        access_key, secret_key = cluster_config.get_s3_credentials()

    script_content = f'''#!/usr/bin/env python3
"""
Script to upload configuration files to the cluster S3 (MinIO) server.

Generated by: vast execution cluster prepare-run
Run ID: {run_id}
Bucket: {bucket_name}
Namespace: {namespace}
"""

import os
import sys
from robovast.execution.cluster_execution.s3_client import upload_configs_to_s3
from robovast.execution.cluster_config.base_config import BaseConfig


class _StaticConfig(BaseConfig):
    def get_s3_credentials(self):
        return ("{access_key}", "{secret_key}")
    def setup_cluster(self, **kw): pass
    def cleanup_cluster(self, **kw): pass
    def prepare_setup_cluster(self, output_dir, **kw): pass
    def get_instance_type_command(self): return ""


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_dir = os.path.join(script_dir, "out_template")
    bucket_name = "{bucket_name}"
    namespace = "{namespace}"

    if not os.path.exists(config_dir):
        print(f"ERROR: Config directory not found: {{config_dir}}")
        sys.exit(1)

    print(f"Uploading config files to S3 bucket '{{bucket_name}}'...")
    upload_configs_to_s3(config_dir, bucket_name, _StaticConfig(), namespace)
    print("Upload complete.")


if __name__ == "__main__":
    main()
'''

    script_path = os.path.join(output_dir, "upload_configs.py")
    with open(script_path, 'w') as f:
        f.write(script_content)
    os.chmod(script_path, 0o755)

    readme_content = """# Execution Instructions
This directory contains the necessary manifests to set up the RoboVAST execution environment on a cluster.

### 0. Set up Kueue (job queueing)

Follow README_kueue.md to install Kueue and apply the queue manifests.

### 1. Set up the MinIO S3 server

Follow README_<CLUSTER CONFIG>.md for cluster-specific setup instructions.

### 2. Upload Configuration Files

After the cluster setup is complete, upload the configuration files to S3:

```bash
./upload_configs.py
```

### 3. Deploy Jobs

Deploy the scenario execution jobs:

```bash
kubectl apply -f all-jobs.yaml
```

To re-deploy after a previous run (Job spec is immutable, so plain apply will fail):
use the same namespace as setup (e.g. ``-n <namespace>``) and either delete then apply,
or replace (delete and recreate) in one step:

```bash
kubectl replace --force -f all-jobs.yaml
```

For a single job file: ``kubectl replace --force -f jobs/<job-name>.yaml -n <namespace>``
"""
    readme_content = readme_content.rstrip()
    with open(f"{output_dir}/README.md", "w") as f:
        f.write(readme_content)
