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
from robovast.common.common import load_config
from robovast.common.config import validate_config
from robovast.common.status import Status, stall_report
from robovast.execution.share_providers import \
    load_share_provider_plugins
from robovast.common.host_display import gui_by_default

from .execute_local import initialize_local_execution


#: Entry-point group for subcommands that attach to ``vast exec``.
EXEC_PLUGIN_GROUP = "robovast.exec_plugins"


class _LazyExecGroup(click.Group):
    """``vast exec``, with subgroups that are listed without being imported.

    ``vast exec cluster`` lives in the cluster package and pulls in the Kubernetes
    client. Registering it eagerly would put that import back on every ``vast``
    invocation -- `load_plugins()` imports this module each time -- for a subcommand
    almost nobody in a given run is about to type.

    Click asks for the names it can offer (``list_commands``) separately from the one it
    is about to run (``get_command``), so the names come from entry-point *metadata* and
    only the chosen subgroup is loaded. A subgroup that fails to import is reported and
    skipped, the same way `load_plugins()` treats a missing plugin: an install without
    the cluster package should be short a subcommand, not broken.
    """

    def _plugins(self):
        from importlib.metadata import \
            entry_points  # pylint: disable=import-outside-toplevel
        return {ep.name: ep for ep in entry_points(group=EXEC_PLUGIN_GROUP)}

    def list_commands(self, ctx):
        return sorted(set(super().list_commands(ctx)) | set(self._plugins()))

    def get_command(self, ctx, cmd_name):
        command = super().get_command(ctx, cmd_name)
        if command is not None:
            return command
        entry = self._plugins().get(cmd_name)
        if entry is None:
            return None
        try:
            return entry.load()
        except Exception as exc:  # noqa: BLE001 - a missing package is not a crash
            click.echo(f"Warning: '{cmd_name}' could not be loaded: {exc}", err=True)
            return None


@click.group(cls=_LazyExecGroup)
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
            # --start-only / --abort-on-failure are harmless no-ops here, so a note is
            # enough. --config is not: it is how one asks for a pilot, and answering a
            # request to run *one* configuration by running the entire search budget is
            # the opposite of what was asked. It is passed through and refused below.
            ignored = [name for name, set_ in (
                ("--start-only", start_only), ("--abort-on-failure", abort_on_failure),
            ) if set_]
            if ignored:
                click.echo(f"Note: {', '.join(ignored)} ignored in search mode.")
            report = run_search_campaign(
                project_config.config_path, campaign_config, results_dir, runs,
                config_filter=config, options=options, campaign_id=campaign_id)
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
                 keep_alive, namespace, context):  # pylint: disable=redefined-outer-name
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
        with service_client(namespace, context) as (client, label):
            _echo_target(label)
            result = client.exec_in_container(ExecRequest(
                command=shell_command, workspace_id=workspace_id,
                config_path=config_path, campaign_id=campaign_id,
                config_name=config_name, keep_alive=keep_alive,
                backend=None))
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
def stop_container(namespace, context):  # pylint: disable=redefined-outer-name
    """Stop the held container-exec container, if there is one."""
    try:
        with service_client(namespace, context) as (client, label):
            _echo_target(label)
            result = client.stop_exec_container()
    except Exception as e:  # noqa: BLE001
        handle_cli_exception(e)
        return
    click.echo(f"stopped {result.target}" if result.stopped
               else "no exec container was running")


@execution.command('wait')
@click.argument('campaign')
@click.option('--interval', default=5.0, show_default=True,
              help='Seconds between status polls.')
@click.option('--timeout', type=float, default=None,
              help='Give up after this many seconds (default: wait indefinitely).')
@target_options
def wait(campaign, interval, timeout, namespace, context):  # noqa: F811
    """Block until CAMPAIGN is over: exit 0 (finished), 1 (failed/stopped), 3 (no phase).

    The lane-agnostic wait: the service drives every campaign, so its phase *is* the
    campaign's whichever backend the runs execute on. Prints each phase change as it
    happens and exits when the campaign reaches a terminal one — which now means past
    postprocessing, not merely past the last run.

    Exists so a *caller* can wait without holding a request open, and is why the MCP
    offers no campaign-wait tool: an agent harness can background this command and be
    notified when it exits, hours or days later, where a blocking tool call would occupy
    the conversation for as long as the campaign ran — and still not outlive the session.
    The loop itself is :func:`~robovast.execution.campaign_wait.wait_for_campaign_status`,
    shared with every other surface that waits.
    """
    from robovast.execution.campaign_wait import wait_for_campaign_status
    from robovast.execution.control_server import Phase
    try:
        with service_client(namespace, context) as (client, label):
            _echo_target(label)
            status = wait_for_campaign_status(
                campaign, client=client, interval=interval, timeout=timeout,
                feedback=click.echo)
    except TimeoutError as e:
        # Not a failure of the campaign, which is still running: the caller asked to stop
        # waiting. A distinct exit code keeps the two apart for a script branching on it.
        click.echo(str(e), err=True)
        raise SystemExit(2)
    except Exception as e:  # noqa: BLE001
        handle_cli_exception(e)
        return
    click.echo(f"{campaign}: {status.phase}")
    if status.phase == Phase.UNKNOWN:
        # `unknown` is terminal, so the wait ends -- but it does not mean the campaign
        # failed. The service has no phase for this id at all: either it is a typo, or the
        # campaign died before it ever wrote to the store. Exiting 1 made both read as "the
        # campaign ran and failed", sending the caller to look for a failure that never
        # happened. A distinct code, because 0/1/2 are taken and a script branches on it.
        click.echo(
            f"{campaign}: the service knows no phase for this campaign — check the id, "
            f"or see 'vast exec cluster log {campaign}' if it died before recording one.",
            err=True)
        raise SystemExit(3)
    if status.error:
        click.echo(f"{campaign}: {status.error}", err=True)
    if status.postprocessing_error:
        # A campaign whose runs passed but whose postprocessing failed still *finished*;
        # saying only "finished" here would send the caller looking for CSVs that a
        # successful exit code promised and nothing produced.
        click.echo(f"{campaign}: postprocessing failed: {status.postprocessing_error}",
                   err=True)
    raise SystemExit(0 if status.phase == Phase.FINISHED else 1)
