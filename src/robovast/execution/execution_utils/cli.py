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

import datetime
import logging
import os
import sys
import tempfile
import time

import click
import yaml

from robovast.common import prepare_campaign_configs
from robovast.common.cli import get_project_config, handle_cli_exception
from robovast.common.cli.project_config import get_vast_file_override
from robovast.common.cli.service_target import echo_target as _echo_target
from robovast.common.cli.service_target import (detected_service_url,
                                                service_client, target_options)
from robovast.common.cluster_context import (get_active_kube_context,
                                             get_config_context_names,
                                             require_context_for_multi_cluster)
from robovast.common.common import load_config
from robovast.common.config import validate_config
from robovast.common.status import Status, stall_report
from robovast.execution.cluster_execution.cluster_execution import (
    _label_safe_campaign, cleanup_cluster_campaign,
    get_cluster_job_counts_per_campaign)
from robovast.execution.cluster_execution.cluster_setup import (
    delete_server, get_cluster_config, get_cluster_config_for_context,
    get_kubernetes_node_labels_from_config, setup_server)
from robovast.execution.cluster_execution.share_providers import \
    load_share_provider_plugins
from robovast.common.host_display import gui_by_default

from ..cluster_execution.kubernetes import (check_kubernetes_access,
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

    Run run configurations in Docker containers with bind mounts
    for configuration and output data.

    Requires project initialization with ``vast init`` first.
    """


@local.command()
@click.option('--config', '-c', default=None,
              help='Run only configurations matching this name or glob pattern (e.g. hall*)')
@click.option('--runs', '-r', type=int, default=None,
              help='Override the number of runs specified in the config')
@click.option('--output', '-o', default=None,
              help='Output directory (uses project results dir if not specified)')
@click.option('--start-only', is_flag=True,
              help='Start the robovast container with a shell, skipping the entrypoint script')
@click.option('--no-gui',  is_flag=True,
              help='Disable host GUI support')
@click.option('--image', '-i', default=None,
              help='Use a custom Docker image (overrides execution.image, ROBOVAST_IMAGE '
                   'and the built-in default)')
@click.option('--abort-on-failure', is_flag=True,
              help='Stop execution after the first failed run config (default: continue)')
@click.option('--use-resource-allocation', is_flag=True,
              help='Add CPU/memory reservations to docker compose run (default: skip for local)')
@click.option('--log-tree', '-t', is_flag=True,
              help='Log scenario execution live tree')
@click.option('--debug', '-d', is_flag=True,
              help='Enable scenario execution debug output')
@click.option('--campaign-id', default=None,
              help='Use this campaign id (and directory name) instead of generating '
                   'one. Lets an external launcher know the id up front.')
@click.option('--campaign-name', default=None,
              help='Override the campaign name; the id becomes <name>-<timestamp>. '
                   'Ignored when --campaign-id is given (that sets the whole id).')
@click.option('--upload-to-share', 'upload_to_share', is_flag=True,
              help='Before postprocessing, write a raw <campaign>.tar.gz to the '
                   'results _archives/ dir (the local upload-to-share deliverable).')
def run(config, runs, output, start_only, no_gui, image, abort_on_failure,
        use_resource_allocation, log_tree, debug, campaign_id, campaign_name, upload_to_share):
    """Execute the project locally using Docker.

    Behaviour is selected by the project ``.vast``:

    - If it defines a ``search:`` block, this runs an iterative **search loop**:
      each generation proposes parameter sets, executes them locally, scores the
      results, and feeds them back to the strategy. Results and a live-queryable
      ``campaign.db`` are written under the output directory. (A ``search:``
      block is mutually exclusive with a ``configuration:`` block.)
    - Otherwise it runs every configuration once as a **batch** in Docker
      containers, continuing past failures (use ``--abort-on-failure`` to stop at
      the first failure). GUI support is enabled by default.

    Prerequisites:
    - Docker must be installed and running
    - Project initialized with ``vast init``
    - X11 server running on host (for GUI support, disable with ``--no-gui``)

    Output:
        Results are written to the project results directory by default,
        or to a custom directory specified with ``--output``.
    """
    try:
        from robovast.execution.backends import RunOptions
        from robovast.execution.controller import (campaign_id_for,
                                                   run_batch_campaign,
                                                   run_search_campaign)

        project_config = get_project_config()
        campaign_config = validate_config(load_config(project_config.config_path))
        results_dir = output or project_config.results_dir

        # GUI is this command's *default*, so a host without a display degrades to headless
        # rather than failing (see gui_by_default). An explicit show_gui through the
        # service is refused instead — there the request was the caller's.
        gui = gui_by_default(no_gui, notify=lambda msg: click.echo(msg, err=True))

        # --campaign-name overrides the name half of the auto id; an explicit
        # --campaign-id still wins (it sets the whole id, timestamp included).
        if campaign_id is None and campaign_name:
            campaign_id = campaign_id_for(campaign_config, campaign_name)

        # --start-only is an interactive debugging shell (drops into a container,
        # produces no campaign) → keep the legacy direct run.sh path for it.
        if start_only and campaign_config.search is None:
            run_script_path = initialize_local_execution(
                config, None, runs, feedback_callback=click.echo,
                skip_resource_allocation=not use_resource_allocation,
                log_tree=log_tree, debug=debug, gui=gui)
            cmd = [run_script_path, "--start-only"]
            if not gui:
                cmd.append("--no-gui")
            # Only an explicit --image is forwarded; otherwise the generated
            # run.sh already bakes in the resolved image (config/ROBOVAST_IMAGE/default).
            if image:
                cmd.extend(["--image", image])
            os.execv(run_script_path, cmd)  # replaces this process
            return

        options = RunOptions(
            gui=gui, start_only=start_only,
            abort_on_failure=abort_on_failure, image=image, log_tree=log_tree,
            debug=debug, skip_resource_allocation=not use_resource_allocation,
            upload_to_share=upload_to_share)

        if campaign_config.search is not None:
            ignored = [name for name, set_ in (
                ("--config", config), ("--start-only", start_only),
                ("--abort-on-failure", abort_on_failure),
            ) if set_]
            if ignored:
                click.echo(f"Note: {', '.join(ignored)} ignored in search mode.")
            report = run_search_campaign(
                project_config.config_path, campaign_config, results_dir, runs,
                options=options, campaign_id=campaign_id)
            _print_search_summary(report)
        else:
            report = run_batch_campaign(
                project_config.config_path, campaign_config, results_dir, runs,
                config_filter=config, options=options, campaign_id=campaign_id)
            click.echo(f"\nBatch run complete: {report['configs']} configuration(s) "
                       f"in {report['campaign_root']}.")

    except Exception as e:
        handle_cli_exception(e)


_RUN_PY_TEMPLATE = '''#!/usr/bin/env python3
"""Prepared robovast run (generated by `vast exec local prepare-run`).

Runs the campaign controller for this project — a batch, or the full search loop.
Edit the settings below, then:  python run.py
"""
from robovast.common.common import load_config
from robovast.common.config import validate_config
from robovast.execution.backends import RunOptions
from robovast.execution.controller import run_batch_campaign, run_search_campaign

VAST = {vast!r}
RESULTS_DIR = {results_dir!r}
RUNS = {runs!r}                      # None -> execution.runs from the vast
CONFIG_FILTER = {config_filter!r}    # batch only: name/glob to run a subset
OPTIONS = RunOptions(gui={gui}, abort_on_failure={abort},
                     image={image!r}, log_tree={log_tree}, debug={debug},
                     skip_resource_allocation={skip_ra})


def main():
    cfg = validate_config(load_config(VAST))
    if cfg.search is not None:
        run_search_campaign(VAST, cfg, RESULTS_DIR, RUNS, options=OPTIONS)
    else:
        run_batch_campaign(VAST, cfg, RESULTS_DIR, RUNS, config_filter=CONFIG_FILTER,
                           options=OPTIONS)


if __name__ == "__main__":
    main()
'''


def _write_controller_run_script(output_dir, *, vast, results_dir, runs,
                                 config_filter, options):
    """Write an editable run.py that runs the controller for this project."""
    content = _RUN_PY_TEMPLATE.format(
        vast=vast, results_dir=results_dir, runs=runs, config_filter=config_filter,
        gui=options.gui,
        abort=options.abort_on_failure, image=options.image,
        log_tree=options.log_tree, debug=options.debug,
        skip_ra=options.skip_resource_allocation)
    path = os.path.join(output_dir, "run.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    os.chmod(path, 0o755)
    return path


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
@click.option('--debug', '-d', is_flag=True,
              help='Enable scenario execution debug output')
def prepare_run(output_dir, config, runs, use_resource_allocation, log_tree, debug):
    """Prepare a run without executing — materialize a runnable directory.

    What is produced depends on the project ``.vast`` (does NOT execute anything):

    - **batch** (no ``search:`` block): the full enumerated config tree
      ``out_template/`` + a docker-compose ``run.sh`` (the classic prepare-run).
      Inspect/tweak the configs and run ``cd OUTPUT-DIR && ./run.sh``. (This runs
      the containers only — postprocessing/``campaign.db`` come from
      ``vast results postprocess`` or ``vast execution local run``.) ``run.sh``
      supports the same flags as ``run`` (``--no-gui``, ``--abort-on-failure``, …).
    - **search**: an editable ``run.py`` only — search configs are composed per
      batch by the controller, so there is nothing static to materialize. Run the
      whole search loop with ``python OUTPUT-DIR/run.py`` (edit its settings
      freely; it launches the campaign controller against the project ``.vast``).

    Prerequisite: project initialized with ``vast init``.
    """
    try:
        import fnmatch

        from robovast.common.config_generation import generate_scenario_variations
        from robovast.execution.backends import RunOptions, stage_run_script

        project_config = get_project_config()
        vast = os.path.abspath(project_config.config_path)
        campaign_config = validate_config(load_config(vast))
        os.makedirs(output_dir, exist_ok=True)
        # The staged run.sh defaults to windowed, so stage the matching scenario params —
        # but only where this host could actually show one. On a headless machine the
        # windowed variant would bake in a `headless: False` that its own run.sh cannot
        # honour. `prepare-run` takes no --no-gui: the generated run.sh does.
        options = RunOptions(
            gui=gui_by_default(False, notify=lambda msg: click.echo(msg, err=True)),
            log_tree=log_tree, debug=debug,
            skip_resource_allocation=not use_resource_allocation)
        eff_runs = runs if runs is not None else campaign_config.execution.runs

        if campaign_config.search is not None:
            # Search configs are composed per batch by the controller, so there is
            # nothing static to materialize — emit only an editable run.py that
            # drives the whole search loop.
            _write_controller_run_script(
                output_dir, vast=vast, results_dir=os.path.abspath(output_dir),
                runs=runs, config_filter=None, options=options)
            click.echo(f"\nPrepared search launcher in {output_dir}:")
            click.echo(f"  run the search loop:   python {os.path.join(output_dir, 'run.py')}\n")
        else:
            # Batch is fully enumerated — materialize the whole config tree + a
            # runnable docker-compose run.sh (the classic prepare-run).
            with tempfile.TemporaryDirectory(prefix="robovast_prepare_") as tmp:
                campaign_data, _ = generate_scenario_variations(
                    variation_file=vast, progress_update_callback=None, output_dir=tmp)
                if not campaign_data["configs"]:
                    raise click.ClickException("No configs found in vast-file.")
                if config:
                    matched = [c for c in campaign_data["configs"]
                               if fnmatch.fnmatch(c["name"], config)]
                    if not matched:
                        raise click.ClickException(f"No configs matched pattern '{config}'.")
                    campaign_data["configs"] = matched
                stage_run_script(campaign_data, output_dir, eff_runs, options,
                                 results_dir=os.path.abspath(output_dir))
            click.echo(f"\nPrepared batch in {output_dir}:")
            click.echo(f"  run it:   cd {output_dir} && ./run.sh")
            click.echo("  (runs the containers only; postprocessing/store run via "
                       "'vast results postprocess' or 'vast exec local run')\n")

    except Exception as e:
        handle_cli_exception(e)


def _print_search_summary(report):
    """Echo a short summary of a finished search campaign's report."""
    click.echo(f"\nSearch complete: {len(report.evaluations)} evaluation(s).")
    if report.best is not None:
        objs = ", ".join(f"{k}={v:.4g}" for k, v in report.best.objectives.items())
        click.echo(f"Most interesting: {report.best.params.values} ({objs})")
    if report.extra.get("num_elites") is not None:
        click.echo(
            f"Archive: {report.extra['num_elites']} elite(s), "
            f"coverage={report.extra.get('coverage', 0):.2f}, "
            f"qd_score={report.extra.get('qd_score', 0):.4g}")


@execution.command('command')
@click.argument('shell_command', required=False, default='')
@click.option('--workspace', 'workspace_id', default='',
              help='Workspace whose project to use (with --config for the .vast).')
@click.option('--config', 'config_path', default='',
              help='Which .vast in the workspace (workspace-relative).')
@click.option('--campaign', 'campaign_id', default='',
              help="Use an existing campaign's _config/ as the project instead.")
@click.option('--config-name', 'config_name', default='',
              help='Stage this configuration. Omitted, the bare image is used.')
@click.option('--keep-alive', is_flag=True,
              help='Leave the container running so later calls can inspect it.')
@target_options
def exec_command(shell_command, workspace_id, config_path, campaign_id, config_name,
                 keep_alive, cluster, namespace, context):  # pylint: disable=redefined-outer-name
    """Test a container and its setup by running SHELL_COMMAND in the experiment image.

    Produces **no campaign data** — nothing durable, no provenance, no repetitions. Use
    ``vast execution local run`` / ``cluster run`` to run the experiment itself.

    Omit ``--config-name`` to check the bare image (``python3 -c 'import x'``,
    ``ros2 pkg list``, file checks). Name one to stage that configuration; an empty
    SHELL_COMMAND then starts its scenario, and its output goes to a log file inside the
    container rather than to stdout (the path is printed).

    There is at most one such container at a time; ``vast execution stop-container``
    ends it. No ``--timeout``: the limit is derived from what is being run (the
    project's ``execution.timeout`` for a scenario, a fixed cap for a command) and
    reported with the result.
    """
    from robovast.service.interface import ExecRequest
    try:
        with service_client(cluster, namespace, context) as (client, label):
            _echo_target(label)
            result = client.exec_in_container(ExecRequest(
                command=shell_command, workspace_id=workspace_id,
                config_path=config_path, campaign_id=campaign_id,
                config_name=config_name, keep_alive=keep_alive,
                backend='cluster' if cluster else None))
    except Exception as e:  # noqa: BLE001 - handled uniformly as a CLI error
        handle_cli_exception(e)
        return
    if result.stdout:
        click.echo(result.stdout, nl=False)
    if result.stderr:
        click.echo(result.stderr, nl=False, err=True)
    click.echo(f"\n[exit {result.exit_code}"
               f"{' TIMED OUT' if result.timed_out else ''}"
               f", limit {result.limit_s}s from {result.limit_source}]", err=True)
    if result.log_path:
        click.echo(f"[scenario log inside the container: {result.log_path} — read it with "
                   f"a follow-up: --keep-alive \"tail -200 {result.log_path}\"]", err=True)
    if result.container.kept:
        click.echo(f"[container kept: image {result.container.image}"
                   f"{', config ' + result.container.config if result.container.config else ''}"
                   f", hard stop in {result.container.deadline_in_s}s]", err=True)
    if result.timed_out or result.exit_code != 0:
        sys.exit(result.exit_code or 1)


@execution.command('stop-container')
@target_options
def stop_container(cluster, namespace, context):  # pylint: disable=redefined-outer-name
    """Stop the held container-exec container, if there is one."""
    try:
        with service_client(cluster, namespace, context) as (client, label):
            _echo_target(label)
            result = client.stop_exec_container('cluster' if cluster else None)
    except Exception as e:  # noqa: BLE001
        handle_cli_exception(e)
        return
    click.echo(f"stopped {result.target}" if result.stopped
               else "no exec container was running")


@execution.group()
def cluster():
    """Execute scenarios on a Kubernetes cluster.

    Run scenario configurations as Kubernetes jobs with bind mounts
    for configuration and output data.

    The commands that execute a *campaign* (``run``, ``prepare-run``) need a project
    (``vast init``, or ``-V <file>``) to know which ``.vast`` to run. The ones that
    act on the *cluster* — ``setup``, ``cleanup``, ``monitor``, ``stop``,
    ``run-cleanup``, ``download-cleanup`` — do not: they read what they need from the
    cluster itself and work from any directory.
    """


def _sole_running_campaign(client):
    """The one running campaign's id, or None; errors if several are running.

    Campaigns run in parallel now, so a bare ``stop`` is only unambiguous when
    exactly one is live.
    """
    from robovast.execution.control_server import is_running
    from robovast.service.interface import ListCampaignsRequest
    live = [c for c in client.list_campaigns(ListCampaignsRequest(limit=100)).campaigns
            if is_running(c.phase)]
    if not live:
        return None
    if len(live) > 1:
        names = ", ".join(c.campaign_id for c in live)
        raise ValueError(
            f"{len(live)} campaigns are running ({names}); pass --campaign to choose one.")
    return live[0].campaign_id


def _confirm_overwrite(name, workspace_id):
    """Ask before a launch overwrites the workspace the project already has.

    Same shape as ``vast workspace init``'s collision prompt: the default is yes, so
    Enter is enough for the common case of re-launching the project you just edited.
    Off a TTY there is nobody to ask and blocking would hang a scripted launch, so it
    proceeds with that default — announced, never silent.

    It says *what* the overwrite can disturb because nothing else can: a campaign is
    workspace-independent by design (``_execution/launch.yaml`` deliberately does not
    record which workspace it came from), so neither this command nor the service can
    tell whether one is still reading these files.
    """
    question = (f"workspace {name!r} ({workspace_id}) already holds this project — "
                "overwrite its files (a campaign still running from them would see "
                "the change)?")
    if not sys.stdin.isatty():
        click.echo(f"note: {question} yes (not a terminal)")
        return True
    return click.confirm(question, default=True)


@cluster.command()
@click.option('--config', '-c', default=None,
              help='Run only configurations matching this name or glob pattern (e.g. hall*)')
@click.option('--runs', '-r', type=int, default=None,
              help='Override execution.runs (default: the value in the .vast).')
@click.option('--log-tree', '-t', is_flag=True,
              help='Log scenario execution live tree')
@target_options
@click.option('--wait-and-download', 'wait_and_download', is_flag=True,
              help='Block until the campaign finishes and its results are uploaded, '
                   'then download them into the project results directory — making a '
                   'cluster run as transparent as a local run.')
@click.option('--poll-interval', type=float, default=5.0, show_default=True,
              help='Seconds between status polls when --wait-and-download is set.')
@click.option('--campaign-id', default=None,
              help='Launch under this campaign id instead of generating one.')
@click.option('--campaign-name', default=None,
              help='Override the campaign name; the id becomes <name>-<timestamp>.')
@click.option('--upload-to-share', 'upload_to_share', is_flag=True,
              help='Stream a raw (pre-postprocess) archive to the configured share '
                   'when the campaign finishes.')
@click.option('--description', default=None, metavar='TEXT',
              help='One line saying what this run is for. It is what tells two '
                   'same-day <name>-<timestamp> campaigns apart in the monitor and '
                   'the web UI.')
@click.option('--workspace', 'workspace_name', default=None, metavar='NAME',
              help="Workspace to push the project into (default: the .vast's "
                   'directory name). Reused when it already exists.')
def run(config, runs, log_tree, cluster, namespace, context, wait_and_download,
        poll_interval, campaign_id, campaign_name, upload_to_share,
        description, workspace_name):  # pylint: disable=function-redefined,redefined-outer-name
    """Execute a campaign (batch or search) on a Kubernetes cluster.

    Runs through the robovast-service, which drives the campaign in-process and
    creates the per-batch scenario Jobs. The service is auto-detected on the
    conventional local port, so with a ``vast serve`` (or an SSH /
    ``kubectl port-forward`` / ``vast serve --attach`` tunnel) up, this needs
    **no flags**; otherwise pass
    ``--cluster`` (with ``-x`` to pick the context) to tunnel to the in-cluster
    service for this call. By default the command is fire-and-forget: it returns
    once the campaign is launched. Track it with 'vast exec cluster monitor'.

    Pass ``--wait-and-download`` to instead block until the campaign finishes and
    its results have been uploaded, then download them into the project results
    directory automatically — one command, results on local disk, like a local run.

    Use --config to run only matching configurations (batch campaigns).

    Names a project with ``vast init``, or directly: ``vast -V my.vast exec cluster
    run``. The project is pushed into a workspace named after its directory, which is
    **reused** on later launches (overwritten, after asking) rather than accumulating
    one workspace per run — ``--workspace`` picks a different name.
    """
    try:
        from robovast.common.cli.project_config import \
            get_project_config  # pylint: disable=import-outside-toplevel
        from robovast.common.cli.service_target import \
            service_client  # pylint: disable=import-outside-toplevel
        from robovast.execution.execution_utils.cluster_run import \
            wait_for_cluster_campaign  # pylint: disable=import-outside-toplevel
        from robovast.service.interface import \
            DESCRIPTION_MAX_LEN  # pylint: disable=import-outside-toplevel
        from robovast.service.project_push import (  # pylint: disable=import-outside-toplevel
            download_campaign_via_service, run_project_via_service)

        # Checked here rather than left to the request model: this says what to do
        # instead of surfacing a pydantic validation string, and it refuses before the
        # project is pushed.
        if description and len(description) > DESCRIPTION_MAX_LEN:
            raise click.ClickException(
                f"--description is {len(description)} characters; the limit is "
                f"{DESCRIPTION_MAX_LEN} — shorten it to one line.")

        project = get_project_config()
        with service_client(cluster, namespace, context,
                            require_service=True) as (client, target):
            _echo_target(target)
            cid = run_project_via_service(
                client, project.config_path, config_filter=config or "",
                # 0, not 1: the service reads a non-positive count as "use the .vast's
                # execution.runs". Substituting 1 here ran a 25-repetition sweep once
                # per configuration and still reported success.
                runs=runs or 0, feedback=click.echo, upload_to_share=upload_to_share,
                campaign_name=campaign_name or "", description=description or "",
                workspace_name=workspace_name or "", on_exists=_confirm_overwrite)
            if not wait_and_download:
                click.echo(f"Launched cluster campaign '{cid}' via robovast-service. "
                           "Track it with 'vast exec cluster monitor' or the service.")
                return

            click.echo(f"Launched cluster campaign '{cid}'. Waiting for it to finish...")
            outcome = wait_for_cluster_campaign(
                cid, client=client, interval=poll_interval, feedback=click.echo)
            if outcome == "failed":
                raise click.ClickException(
                    f"Campaign '{cid}' failed. Inspect with 'vast exec cluster "
                    "monitor' (the failure reason is on its status).")

            click.echo(f"Campaign '{cid}' finished. Downloading results...")
            # The service streams the campaign from the object store — no external
            # share needed for delivery.
            download_campaign_via_service(client, cid, os.getcwd(), feedback=click.echo)
    except (click.UsageError, click.ClickException):
        raise
    except Exception as e:
        handle_cli_exception(e)


def _progress_bar(done, total, width=20):
    """Return ``(bar, pct)`` — the ``█``/``░`` progress bar used across the monitor."""
    frac = max(0.0, min(1.0, done / total)) if total and total > 0 else 0.0
    filled = int(width * frac)
    return "█" * filled + "░" * (width - filled), 100.0 * frac


def _fmt_size(n):
    """Format a byte count as MiB (matches the upload progress display)."""
    return f"{n / 1024 / 1024:.1f} MiB"


def _fmt_duration(seconds):
    """Format an elapsed time with an adaptive unit ("42s", "7m 12s", "1h 04m")."""
    total = int(max(0, seconds))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m {total % 60:02d}s"
    return f"{total // 3600}h {(total % 3600) // 60:02d}m"


def _fmt_rate(bps):
    """Format a transfer rate (bytes/s) with an adaptive unit."""
    if bps >= 1024 * 1024:
        return f"{bps / 1024 / 1024:.1f} MiB/s"
    if bps >= 1024:
        return f"{bps / 1024:.1f} KiB/s"
    return f"{bps:.0f} B/s"


def _monitor_via_service(namespace, kube_context, interval, once):
    """Monitor campaigns through the robovast-service.

    The service drives every campaign in-process, so its ``get_status`` *is* the
    controller's live state — no controller pod to find and no ``port-forward`` to
    open (both are gone). Handles **multiple concurrent campaigns**: each live one
    is its own block, driven by that campaign's ``phase`` — the authoritative
    "done" signal, which is what keeps the monitor from exiting in the gap between
    search generations (when live Jobs momentarily drop to zero). Campaigns started
    while monitoring are picked up on the next tick.

    Returns ``True`` if it handled the monitoring, ``False`` if no service is
    configured so the caller can fall back to the Kubernetes-only view.
    """
    from robovast.service.client import RobovastClient
    from robovast.service.interface import ListCampaignsRequest

    url = detected_service_url()
    if not url:
        logging.debug("No robovast-service detected; falling back to K8s view.")
        return False
    client = RobovastClient(url)

    def _live():
        """(campaign_id, phase) for everything the service is tracking."""
        try:
            resp = client.list_campaigns(ListCampaignsRequest(limit=100))
        except Exception:  # pylint: disable=broad-except
            logging.debug("Could not list campaigns from the service.")
            return None
        return [(c.campaign_id, c.phase) for c in resp.campaigns]

    campaigns = _live()
    if campaigns is None:
        return False
    if not campaigns:
        click.echo("No campaigns known to the robovast-service.")
        return True

    cursor_up, clear_line = "\033[A", "\033[2K"
    prev = [0]

    def _live_counts(campaign_id):
        """Current batch's live job counts (running/pending) for this campaign.

        Read from the service so it works in every deployment mode — including a
        remote/in-cluster service the CLI host has no Kubernetes access to.
        """
        if not campaign_id:
            return {}
        try:
            counts = client.list_jobs(campaign_id).counts
        except Exception:  # pylint: disable=broad-except
            return {}
        return {"running": counts.running, "pending": counts.pending,
                "waiting": counts.waiting, "blocked": counts.blocked}

    def _campaign_lines(status):
        c = _live_counts(status.get("campaign_id"))
        runs = status.get("runs") or {}
        phase_label = status.get("phase", "?")
        if status.get("stage"):
            phase_label += f" / {status['stage']}"
        if status.get("phase") == "uploading" and status.get("share_provider"):
            phase_label += f" via {status['share_provider']}"
        lines = [f"Campaign {status.get('campaign_id', '?')}  [{phase_label}]"]
        line2 = f"  Batch {status.get('batch', 0)} (done {status.get('batches_done', 0)})"
        if status.get("best_objective") is not None:
            line2 += f"   best={status['best_objective']:.4g}"
        lines.append(line2)
        if status.get("budget"):
            lines.append("  Budget: " + " | ".join(
                f"{b['label']} {b.get('current')}/{b.get('limit')}" for b in status["budget"]))
        completed, total = runs.get('completed', 0), runs.get('total', 0)
        bar_str, pct = _progress_bar(completed, total)
        run_line = f"  Runs (this batch): [{bar_str}] {pct:5.1f}%  {completed}/{total}"
        if c:
            run_line += f"   Running: {c.get('running', 0)}  Pending: {c.get('pending', 0)}"
            if c.get("waiting"):
                # Queued for cluster capacity — normal, so no reason is printed; it is
                # on each job's detail (`list_campaign_jobs`) when one is needed.
                run_line += f"  Waiting: {c['waiting']}"
            if c.get("blocked"):
                # Jobs that cannot start (e.g. ImagePullBackOff). The reason rides on
                # each job's detail; the campaign fails with it after a grace window.
                run_line += f"  Blocked: {c['blocked']}"
        lines.append(run_line)
        # How long since a run last completed, and the stall verdict when the campaign
        # declared a per-run budget. A campaign spends its whole life in one `running`
        # phase, so without this a wedged run and a slow one are the same picture.
        stall = stall_report(Status.model_validate(status))
        if stall.get("progress_age_s") is not None and (completed or total):
            age_line = f"  Last run completed: {_fmt_duration(stall['progress_age_s'])} ago"
            if stall.get("stalled"):
                age_line += "   *** STALLED ***"
            elif stall.get("stalled") is None:
                # Tri-state: no declared execution.timeout, so silence here would read
                # as "fine". Say the verdict is unavailable instead.
                age_line += "   (no execution.timeout — stall unjudged)"
            lines.append(age_line)
        if stall.get("stall_reason"):
            lines.append(f"  Stalled: {stall['stall_reason']}")
        up = (status.get("extra") or {}).get("upload")
        if status.get("phase") == "uploading" and up:
            u_bar, u_pct = _progress_bar(up.get("sent", 0), up.get("total", 0))
            up_line = (f"  Upload: [{u_bar}] {u_pct:5.1f}%  "
                       f"{_fmt_size(up.get('sent', 0))}/{_fmt_size(up.get('total', 0))}")
            if up.get("rate") is not None:
                up_line += f"   {_fmt_rate(up['rate'])}"
            lines.append(up_line)
        if status.get("stop"):
            lines.append(f"  Stop: {status['stop'].get('reason', '')}")
        if status.get("error"):
            # Indent the (possibly multi-line) failure reason under the campaign.
            first, *rest = str(status["error"]).splitlines()
            lines.append(f"  Error: {first}")
            lines.extend(f"         {ln}" for ln in rest)
        return lines

    def _render(blocks):
        lines = [line for block in blocks for line in block]
        for _ in range(prev[0]):
            sys.stdout.write(cursor_up)
        for line in lines:
            sys.stdout.write("\r" + clear_line + line + "\n")
        for _ in range(len(lines), prev[0]):
            sys.stdout.write("\r" + clear_line + "\n")
        prev[0] = len(lines)
        sys.stdout.flush()

    def _blocks_for(ids):
        """Render blocks for *ids*, and report which have reached a terminal phase."""
        from robovast.execution.control_server import is_terminal
        blocks, finished = [], set()
        for cid in ids:
            try:
                # `Status` is a pydantic model; _campaign_lines reads it as a dict.
                status = client.get_status(cid).model_dump()
            except Exception:  # pylint: disable=broad-except
                blocks.append([f"Campaign {cid}  [status unavailable]"])
                continue
            blocks.append(_campaign_lines(status))
            if is_terminal(status.get("phase")):
                finished.add(cid)
        return blocks, finished

    try:
        if once:
            blocks, _ = _blocks_for([cid for cid, _ph in campaigns])
            _render(blocks)
            return True

        click.echo(f"Monitoring {len(campaigns)} campaign(s) (press Ctrl+C to stop)...")
        sys.stdout.write("\n")
        sys.stdout.flush()
        while True:
            current = _live()
            if current is None:
                return False
            ids = [cid for cid, _ph in current]
            blocks, finished = _blocks_for(ids)
            _render(blocks)
            if ids and finished >= set(ids):
                click.echo("\nAll campaigns finished.")
                return True
            time.sleep(interval)
    except Exception:  # pylint: disable=broad-except
        logging.debug("Service monitor failed; falling back to K8s view.")
        return False


@cluster.command()
@click.option('--interval', '-i', type=float, default=2.0, show_default=True,
              help='Polling interval in seconds')
@click.option('--once', is_flag=True,
              help='Print job status once and exit')
@click.option('--context', '-x', 'kube_context', default=None,
              help='Kubernetes context to use (default: active context in kubeconfig)')
@click.option('--namespace', '-n', default='default', show_default=True,
              help='Kubernetes namespace the scenario Jobs run in.')
def monitor(interval, once, kube_context, namespace):
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
            # --vast-file, else the project's .vast if run inside one; monitoring a
            # cluster works without either (it then watches the active context).
            from robovast.common.cli.project_config import \
                resolve_vast_file  # pylint: disable=import-outside-toplevel
            config_path = resolve_vast_file()

            config_names = get_config_context_names(config_path) if config_path else set()
            if config_names:
                contexts_to_monitor = sorted((n, n) for n in config_names)
            else:
                # No per-cluster config — fall back to active context
                active = get_active_kube_context()
                contexts_to_monitor = [(active or "(active)", active)]
        else:
            contexts_to_monitor = [(kube_context, kube_context)]

        multi = len(contexts_to_monitor) > 1

        # Prefer the robovast-service (single-context campaigns): it drives the
        # campaigns, so its status reports loop phase/batch/run progress and is
        # authoritative for "done" — the monitor never exits in the gap between
        # search generations. Falls through to the Kubernetes-only view below when
        # no service is configured (multi-cluster, or partial setups).
        if not multi:
            if _monitor_via_service(namespace, contexts_to_monitor[0][1], interval, once):
                return

        # Per-context state (keyed by kube_context_name)
        initial_total: dict[str, dict] = {}        # ctx -> {campaign: total}
        max_ok: dict[str, dict] = {}               # ctx -> {campaign: max_ok}
        max_fail: dict[str, dict] = {}             # ctx -> {campaign: max_fail}
        last_per_run: dict[str, dict] = {}         # ctx -> last known per_run
        run_first_finished: dict[str, dict] = {}   # ctx -> {campaign: (timestamp, finished_count)}
        all_jobs_seen: dict[str, dict] = {}        # ctx -> {campaign: bool} — True once all jobs visible
        prev_line_count = [0]

        def _build_run_lines(label, ctx, per_run):
            """Return (lines, all_done) for a single context."""
            ctx_initial = initial_total.setdefault(ctx, {})
            ctx_ok = max_ok.setdefault(ctx, {})
            ctx_fail = max_fail.setdefault(ctx, {})
            ctx_first = run_first_finished.setdefault(ctx, {})
            ctx_all_seen = all_jobs_seen.setdefault(ctx, {})

            all_campaigns = sorted(set(ctx_initial.keys()) | set(per_run.keys()))
            lines = []
            all_done = True
            indent = "  " if multi else ""
            now = time.time()

            for campaign in all_campaigns:
                c = per_run.get(campaign, {"completed": 0, "failed": 0, "running": 0, "pending": 0,
                                           "total_job_num": None})
                current_total = c["completed"] + c["failed"] + c["running"] + c["pending"]
                if campaign not in ctx_initial:
                    ctx_initial[campaign] = current_total
                # Prefer annotation-based total so the monitor shows the full run size
                # even while many jobs are still pending / not yet visible in the API.
                annotated_total = c.get("total_job_num")
                total = annotated_total if annotated_total else ctx_initial[campaign]
                ctx_ok[campaign] = max(ctx_ok.get(campaign, 0), c["completed"])
                ctx_fail[campaign] = max(ctx_fail.get(campaign, 0), c["failed"])
                still_in_cluster = c["running"] + c["pending"]
                # Once all jobs have been seen in the cluster at least once, it's safe
                # to infer finished count from total - still_in_cluster (which handles
                # TTL-deleted Job objects). Before that point, jobs are still being
                # submitted and still_in_cluster underestimates, causing finished to be
                # wildly overestimated and ok to appear inflated.
                if current_total >= total:
                    ctx_all_seen[campaign] = True
                if ctx_all_seen.get(campaign) or annotated_total:
                    finished = total - still_in_cluster if total > 0 else 0
                else:
                    finished = c["completed"] + c["failed"]
                if still_in_cluster > 0:
                    all_done = False
                ok = ctx_ok[campaign]
                fail = ctx_fail[campaign]
                remainder = finished - ok - fail
                if remainder > 0:
                    ok += remainder
                pct = 100.0 * finished / total if total > 0 else 100.0
                filled = int(bar_width * finished / total) if total > 0 else bar_width
                progress_bar = "█" * filled + "░" * (bar_width - filled)
                pct_str = f"{pct:.1f}%".rjust(pct_width)

                # Track first observed completion for this run (to compute rate/ETA)
                if finished > 0 and campaign not in ctx_first:
                    ctx_first[campaign] = (now, finished)

                # Compute rate (jobs/min) and ETA
                rate_str = ""
                eta_str = ""
                if campaign in ctx_first and still_in_cluster > 0:
                    first_ts, first_finished = ctx_first[campaign]
                    elapsed = now - first_ts
                    jobs_since = finished - first_finished
                    if elapsed >= 10 and jobs_since > 0:
                        rate_per_min = jobs_since / (elapsed / 60.0)
                        rate_str = f"  {rate_per_min:.1f} jobs/min"
                        remaining = total - finished
                        if remaining > 0 and rate_per_min > 0:
                            eta_secs = remaining / (rate_per_min / 60.0)
                            eta_dt = datetime.datetime.fromtimestamp(now + eta_secs)
                            eta_str = f"  ETA ~{eta_dt.strftime('%H:%M')}"

                lines.append(
                    f"{indent}{campaign}  [{progress_bar}]  {pct_str}  "
                    f"{finished}/{total}  ({ok} ok, {fail} fail)  "
                    f"Running: {c['running']}  Pending: {c['pending']}"
                    f"{rate_str}{eta_str}"
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
                    # Suppress urllib3 retry warnings for unreachable contexts — this
                    # display reports reachability itself, one line below.
                    from robovast.common.kube import quiet_urllib3_retries
                    with quiet_urllib3_retries():
                        per_run = get_cluster_job_counts_per_campaign(namespace, context=ctx)
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
@click.option('--campaign', '-i', default=None,
              help='Campaign to stop (default: the only running one)')
@target_options
def stop(campaign, cluster, namespace, context):
    """Ask a running campaign to stop gracefully (after the current batch).

    Goes through the robovast-service, which drives the campaign in-process: the
    loop ends once the in-flight batch finishes and the campaign is published as
    usual. A no-op if nothing is running.
    """
    try:
        with service_client(cluster, namespace, context,
                            require_service=True) as (client, target):
            _echo_target(target)
            campaign_id = campaign or _sole_running_campaign(client)
            if campaign_id is None:
                click.echo("No running campaign found.")
                return
            result = client.stop(campaign_id)
            if result.ok:
                click.echo(f"Stop requested for '{campaign_id}'. "
                           "The campaign will end after the current batch.")
            else:
                click.echo(f"Stop failed: {result.message}")
    except (click.UsageError, click.ClickException):
        raise
    except Exception as e:
        handle_cli_exception(e)


@cluster.command()
@click.option('--campaign', '-i', default=None,
              help='Campaign to show the log for (default: the only running one)')
@click.option('--follow', '-f', is_flag=True,
              help='Stream new output until the campaign finishes')
@target_options
def log(campaign, follow, cluster, namespace, context):
    """Print a campaign's unified infrastructure log.

    The same divider-separated stream the web UI and MCP show — the variation
    (config-generation), run (controller) and postprocessing phases in order, each
    under a ``===== PHASE =====`` divider. Goes through the robovast-service when
    one is reachable (auto-detected, or ``--cluster`` to tunnel in); otherwise
    reads the campaign from disk (the local project's results dir, or an absolute
    campaign path).
    """
    try:
        from robovast.service.client import HTTPTransport
        with service_client(cluster, namespace, context) as (client, target):
            _echo_target(target)
            if isinstance(client, HTTPTransport):
                campaign_id = campaign or _sole_running_campaign(client)
                if campaign_id is None:
                    click.echo("No running campaign found; pass --campaign.")
                    return
                offset = 0
                while True:
                    chunk = client.get_campaign_logs(campaign_id, offset)
                    if chunk.text:
                        click.echo(chunk.text, nl=False)
                        offset = chunk.next_offset
                    if chunk.eof or not follow:
                        break
                    time.sleep(1.5)
                return

            # No service reachable: read the campaign directory directly.
            from robovast.common.campaign_logs import assemble_log_from_dir
            if not campaign:
                raise ValueError(
                    "No robovast-service reachable; pass --campaign (name or path) "
                    "to read a campaign on disk.")
            if os.path.isabs(campaign):
                campaign_dir = campaign
            else:
                from robovast.common.cli.project_config import ProjectConfig
                cfg = ProjectConfig.load()
                if cfg is None or not cfg.results_dir:
                    raise ValueError(
                        "Project not initialized; run 'vast init' or pass an "
                        "absolute campaign path.")
                campaign_dir = os.path.join(cfg.results_dir, campaign)
            text, _, _ = assemble_log_from_dir(campaign_dir, offset=0, eof=True)
            click.echo(text, nl=False)
    except (click.UsageError, click.ClickException):
        raise
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

    Ignores projects entirely: this deploys into a cluster and runs from any
    directory, so a ``.robovast_project`` is neither required nor read here — it is
    found by walking up to the filesystem root, and a project above an unrelated CWD
    has no business deciding where a cluster's pods may run.

    Node label selectors for job and control pods are therefore read only from a
    ``.vast`` you name explicitly, under
    ``execution.kubernetes.jobs.node_labels`` and
    ``execution.kubernetes.control.node_labels``::

        vast -V my_campaign.vast exec cluster setup rke2

    Without ``-V`` no node labels are applied (logged at INFO) and pods schedule
    wherever Kubernetes puts them. A named ``.vast`` that cannot be read is an error
    rather than a silent "no labels".

    Share credentials (``ROBOVAST_SHARE_TYPE`` and its provider variables — e.g.
    ``ROBOVAST_GCS_BUCKET`` / ``ROBOVAST_GCS_KEY_FILE``) are read from the host
    environment / project ``.env`` at setup and handed to the in-cluster service
    as a Secret, so ``--upload-to-share`` campaigns work from the cluster. A key
    *file* is inlined into the Secret; nothing else is needed on the host.
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
        # Only an explicitly named config, never an ambient project — see setup_server.
        require_context_for_multi_cluster(kube_context, get_vast_file_override())
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
@click.option('--campaign', '-i', default=None,
              help='Only remove this campaign\'s bucket (e.g. campaign-2025-02-27-123456). Without this, removes all campaign buckets.')
@click.option('--force', is_flag=True,
              help='Delete a named campaign even if the service still considers it live.')
@target_options
def download_cleanup(campaign, force, cluster, namespace, context):
    """Remove result buckets from the cluster object store (via the service).

    Deletes run result buckets (``campaign-*``) from the object store. This runs
    **through the robovast-service**, which holds the object-store credentials and
    the authoritative live-campaign set — so no local credentials are needed and a
    bulk delete never removes a campaign that is still running.

    The service is auto-detected on the conventional local port (a ``vast serve``
    or a tunnel); or pass ``--cluster`` (``-x`` context, ``-n``
    namespace) to tunnel to the in-cluster service for this call. Use ``--campaign``
    to remove a single one.
    """
    try:
        from robovast.service.interface import CleanupDataRequest
        with service_client(cluster, namespace, context,
                            require_service=True) as (client, target):
            _echo_target(target)
            res = client.cleanup_campaign_data(
                CleanupDataRequest(campaign_id=campaign, force=force))
            if not res.ok:
                raise click.ClickException(res.message or "cleanup-data failed")
            click.echo(f"✓ {res.message}")
    except (click.UsageError, click.ClickException):
        raise
    except Exception as e:
        handle_cli_exception(e)


@cluster.command(name='run-cleanup')
@click.option('--campaign', '-i', default=None,
              help='Clean only jobs for this campaign (e.g. campaign-2025-02-27-123456). Without this, cleans all scenario-runs jobs.')
@click.option('--data', is_flag=True,
              help='Also delete the campaign result bucket(s) from the object store (via the service).')
@click.option('--force', is_flag=True,
              help='With --data: delete a named campaign even if the service still considers it live.')
@target_options
def run_cleanup(campaign, data, force, cluster, namespace, context):
    """Clean up jobs and pods from a cluster run.

    Removes scenario execution Jobs and their pods directly (using your kubeconfig
    — the ``-x`` context, ``-n`` namespace). By default removes all campaigns; use
    ``--campaign`` for one.

    Use ``--data`` to **also** delete the campaign result bucket(s) from the object
    store. That step goes **through the robovast-service** (which holds the
    object-store credentials), auto-detected on the conventional local port or
    reached with ``--cluster`` — no local credentials needed.

    Usage: vast execution cluster run-cleanup
    Usage: vast execution cluster run-cleanup --campaign campaign-2025-02-27-123456
    Usage: vast execution cluster run-cleanup --campaign campaign-2025-02-27-123456 --data
    """
    try:
        require_context_for_multi_cluster(context, get_vast_file_override())
        k8s_client = get_kubernetes_client(context=context)
        click.echo("Checking Kubernetes cluster access...")
        k8s_ok, k8s_msg = check_kubernetes_access(k8s_client, namespace=namespace)
        if not k8s_ok:
            click.echo(f"✗ Error: {k8s_msg}", err=True)
            sys.exit(1)

        skip_job_cleanup = False
        if campaign:
            per_run = get_cluster_job_counts_per_campaign(namespace, context=context)
            label_safe = _label_safe_campaign(campaign)
            if label_safe not in per_run:
                available = sorted(per_run.keys())
                if data:
                    # Jobs already gone — warn but continue to bucket cleanup
                    click.echo(f"Campaign '{campaign}' not found in cluster (jobs already cleaned up).", err=True)
                    skip_job_cleanup = True
                else:
                    if available:
                        click.echo(f"Campaign '{campaign}' not found in cluster.", err=True)
                        click.echo("Available campaign-ids:", err=True)
                        for rid in available:
                            click.echo(f"  - {rid}", err=True)
                    else:
                        click.echo("No scenario run jobs in cluster.", err=True)
                    sys.exit(1)
            if not skip_job_cleanup:
                click.echo(f"Cleaning up jobs and pods for campaign '{campaign}'...")
        else:
            click.echo("Cleaning up all scenario run jobs and pods...")

        if not skip_job_cleanup:
            cleanup_cluster_campaign(namespace=namespace, campaign=campaign, context=context)
            click.echo("✓ Job/pod cleanup completed successfully!")

        if data:
            # Bucket cleanup runs server-side: the service owns the object-store
            # credentials and the authoritative live-campaign guard.
            from robovast.service.interface import CleanupDataRequest
            with service_client(cluster, namespace, context,
                                require_service=True) as (client, target):
                _echo_target(target)
                res = client.cleanup_campaign_data(
                    CleanupDataRequest(campaign_id=campaign, force=force))
                if not res.ok:
                    raise click.ClickException(res.message or "cleanup-data failed")
                click.echo(f"✓ {res.message}")

    except (click.UsageError, click.ClickException):
        raise
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

    If ``--cluster-config`` is not specified, it will automatically detect which
    cluster configuration was used during setup by reading it back from the deployed
    ``robovast-service`` — so this works from any host and needs no project.
    When specifying ``--cluster-config`` explicitly, pass ``-n <namespace>`` if the
    setup was done in a non-default namespace.
    """
    try:
        require_context_for_multi_cluster(kube_context, get_vast_file_override())
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
    - ``upload_configs.py`` script to upload run configurations to the cluster
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
        context_key = kube_context
        # Get project configuration. This command runs a *campaign*, so the config it
        # is about to prepare is also the one whose per-cluster resource lists decide
        # whether --context is required — resolved before that check, not by it.
        project_config = get_project_config()
        config_path = project_config.config_path
        require_context_for_multi_cluster(kube_context, config_path)

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

        namespace = cluster_kwargs.get("namespace", "default")

        if cluster_config is None:
            # Auto-detect: read config (with credentials) from the deployed service.
            cluster_config = get_cluster_config_for_context(context_key, namespace)
            if cluster_config:
                logging.debug("Read cluster config from the deployed robovast-service")
            else:
                raise ValueError(
                    "No cluster config specified and no deployed service found to "
                    "read it from. Use --cluster-config <name> (with -o for "
                    "credentials) to run fully offline, or check --context."
                )
        else:
            try:
                from robovast.execution.cluster_execution.service_deploy import \
                    read_service_config_from_cluster
                _, stored_kwargs = read_service_config_from_cluster(namespace, context_key)
                cluster_config = get_cluster_config(cluster_config)
                if cluster_config and stored_kwargs:
                    cluster_config.restore_from_setup_kwargs(stored_kwargs)
                if cluster_config and cluster_kwargs:
                    cluster_config.restore_from_setup_kwargs(cluster_kwargs)
            except Exception as e:
                raise RuntimeError(f"Failed to get cluster config: {e}") from e

        # Compose the batch campaign data on the host (the same path the
        # controller uses for batch), then build the manifests with the very
        # builder the controller submits with. This command is offline — only
        # manifest generation, no Kubernetes API calls.
        import fnmatch  # pylint: disable=import-outside-toplevel
        from robovast.common.config_generation import \
            generate_scenario_variations  # pylint: disable=import-outside-toplevel
        from robovast.common.execution import \
            resolve_robovast_image  # pylint: disable=import-outside-toplevel
        from robovast.execution.cluster_execution.kubernetes_backend import (  # pylint: disable=import-outside-toplevel
            BatchJobRunner, _instance_type_command)
        from robovast.execution.controller import \
            campaign_id_for  # pylint: disable=import-outside-toplevel

        campaign_config = validate_config(load_config(config_path))
        if campaign_config.search is not None:
            raise click.ClickException(
                "'cluster prepare-run' is a batch-only debugging aid, but the given "
                ".vast defines a 'search:' block. Use 'vast exec cluster run' instead."
            )
        campaign_id = campaign_id_for(campaign_config)
        num_runs = runs if runs is not None else campaign_config.execution.runs

        # generate_scenario_variations writes resolved inputs into a working dir
        # that campaign_data references; keep it alive until the manifests + the
        # config tree have been written.
        with tempfile.TemporaryDirectory(prefix="robovast_prepare_") as _work:
            campaign_data, _ = generate_scenario_variations(
                variation_file=config_path, progress_update_callback=None,
                output_dir=_work)
            if not campaign_data["configs"]:
                raise click.ClickException("No configs found in vast-file")
            if config:
                matched = [c for c in campaign_data["configs"]
                           if fnmatch.fnmatch(c["name"], config)]
                if not matched:
                    raise click.ClickException(f"No configs matched pattern '{config}'")
                campaign_data["configs"] = matched

            from robovast.common.config import SCENARIO_CONTAINER
            _containers = (campaign_data.get("execution") or {}).get("containers") or {}
            image = resolve_robovast_image(
                required=True,
                config_image=(_containers.get(SCENARIO_CONTAINER) or {}).get("image"))
            job_runner = BatchJobRunner.for_batch(
                campaign_data=campaign_data, campaign_id=campaign_id, batch_tag=None,
                runs=num_runs, cluster_config=cluster_config, namespace=namespace,
                image=image, kube_context=kube_context, log_tree=log_tree)

            click.echo(f"Preparing run configuration 'ID: {campaign_id}', run configs: "
                       f"{len(campaign_data['configs'])}, runs per run config: {num_runs}...")

            # Prepare config files
            logging.debug("Preparing configuration files...")
            out_dir = os.path.join(output, "out_template")
            prepare_campaign_configs(
                out_dir, campaign_data, cluster=True,
                instance_type_command=_instance_type_command(cluster_config))
            # Per-job multi-document parameter files + job-link manifest (matches
            # what upload writes for a real run).
            job_runner._write_job_param_files(out_dir)  # pylint: disable=protected-access

            # Generate all job manifests — one K8s Job per packed job
            # (runs_per_job=1 → one job per config/run).
            logging.debug("Generating job manifests...")
            jobs_dir = os.path.join(output, "jobs")
            os.makedirs(jobs_dir, exist_ok=True)
            all_jobs = []
            jobs = job_runner._build_jobs()  # pylint: disable=protected-access
            for job in jobs:
                job_manifest = job_runner.create_job_manifest(job, len(jobs))
                job_name = job_manifest['metadata']['name']
                with open(os.path.join(jobs_dir, f"{job_name}.yaml"), 'w') as f:
                    yaml.dump(job_manifest, f, default_flow_style=False)
                all_jobs.append(job_manifest)
            job_count = len(all_jobs)

        # Save combined manifest
        combined_file = os.path.join(output, "all-jobs.yaml")
        with open(combined_file, 'w') as f:
            yaml.dump_all(all_jobs, f, default_flow_style=False)

        # Read node labels from the vast config
        _jobs_node_labels, _control_node_labels = get_kubernetes_node_labels_from_config(config_path)

        cluster_config.prepare_setup_cluster(output, control_node_labels=_control_node_labels,
                                             **cluster_kwargs)
        from robovast.execution.cluster_execution.kubernetes_kueue import \
            prepare_kueue_setup  # pylint: disable=import-outside-toplevel
        prepare_kueue_setup(output, namespace=namespace, kube_context=kube_context,
                            node_labels=_jobs_node_labels)

        generate_upload_script(output, campaign_id, namespace, cluster_config)

        click.echo(f"✓ Successfully prepared {job_count} job manifests in directory'{
                   output}'.\n\nFollow README files to set up and execute.\n")

    except Exception as e:
        handle_cli_exception(e)


def generate_upload_script(output_dir, campaign, namespace="default", cluster_config=None):
    """Generate a Python script to upload configuration files to S3."""
    bucket_name = campaign.lower().replace("_", "-")
    access_key = "minioadmin"
    secret_key = "minioadmin"
    if cluster_config is not None:
        access_key, secret_key = cluster_config.get_s3_credentials()

    # Determine shared-bucket / external-S3 settings from the cluster config.
    uses_embedded = True
    host_endpoint = "None"
    shared_bucket = "None"
    s3_region = '"us-east-1"'
    if cluster_config is not None:
        uses_embedded = cluster_config.uses_embedded_s3()
        ep = cluster_config.get_host_s3_endpoint()
        if ep is not None:
            host_endpoint = f'"{ep}"'
        sb = cluster_config.get_s3_bucket()
        if sb is not None:
            shared_bucket = f'"{sb}"'
        s3_region = f'"{cluster_config.get_s3_region()}"'

    script_content = f'''#!/usr/bin/env python3
"""
Script to upload configuration files to the cluster storage backend.

Generated by: vast execution cluster prepare-run
Run ID: {campaign}
Bucket: {bucket_name}
Namespace: {namespace}
"""

import os
import re
import subprocess
import sys
import time

import boto3
from botocore.config import Config as _BotoConfig

ACCESS_KEY = "{access_key}"
SECRET_KEY = "{secret_key}"
USES_EMBEDDED = {uses_embedded}
HOST_ENDPOINT = {host_endpoint}
SHARED_BUCKET = {shared_bucket}
S3_REGION = {s3_region}
CAMPAIGN = "{bucket_name}"
NAMESPACE = "{namespace}"

_FORWARD_RE = re.compile(r"Forwarding from 127\\.0\\.0\\.1:(\\d+)")


def _start_port_forward():
    """Port-forward the in-cluster MinIO (robovast pod); return (proc, endpoint)."""
    proc = subprocess.Popen(
        ["kubectl", "port-forward", "-n", NAMESPACE, "pod/robovast", ":9000"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.time() + 30
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                raise RuntimeError("kubectl port-forward exited early")
            continue
        match = _FORWARD_RE.search(line)
        if match:
            return proc, f"http://127.0.0.1:{{match.group(1)}}"
    proc.terminate()
    raise TimeoutError("timed out establishing kubectl port-forward")


def _client(endpoint):
    return boto3.client(
        "s3", endpoint_url=endpoint,
        aws_access_key_id=ACCESS_KEY, aws_secret_access_key=SECRET_KEY,
        region_name=S3_REGION,
        config=_BotoConfig(signature_version="s3v4",
                           s3={{"addressing_style": "path"}},
                           request_checksum_calculation="when_required",
                           response_checksum_validation="when_required"))


def main():
    config_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out_template")
    if not os.path.isdir(config_dir):
        print(f"ERROR: Config directory not found: {{config_dir}}")
        sys.exit(1)

    # Match the layout the job init containers mirror from
    # (in_pod_storage.campaign_storage_location): a shared bucket stores the
    # campaign under "<campaign>/"; otherwise the campaign is its own bucket.
    if SHARED_BUCKET:
        bucket, prefix = SHARED_BUCKET, f"{{CAMPAIGN}}/"
    else:
        bucket, prefix = CAMPAIGN, ""

    pf = None
    try:
        if USES_EMBEDDED:
            pf, endpoint = _start_port_forward()
        else:
            endpoint = HOST_ENDPOINT
        s3 = _client(endpoint)
        if not SHARED_BUCKET:
            try:
                s3.head_bucket(Bucket=bucket)
            except Exception:  # noqa: BLE001 - bucket likely absent; create it
                s3.create_bucket(Bucket=bucket)
        print(f"Uploading config files to '{{bucket}}' (prefix '{{prefix}}')...")
        count = 0
        for root, _dirs, files in os.walk(config_dir):
            for name in files:
                path = os.path.join(root, name)
                rel = os.path.relpath(path, config_dir).replace(os.sep, "/")
                extra = {{"Metadata": {{"executable": "yes"}}}} if os.access(path, os.X_OK) else {{}}
                s3.upload_file(path, bucket, prefix + rel, ExtraArgs=extra)
                count += 1
        print(f"Upload complete ({{count}} files).")
    finally:
        if pf is not None:
            pf.terminate()


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

### 1. Set up the S3 storage backend

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
