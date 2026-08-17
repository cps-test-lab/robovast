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

"""``vast exec local`` -- the local Docker execution lane.

Only the *lane* lives here. The ``vast exec`` group shell itself, and the two verbs that
merely drive a service (``command``, ``stop-container``), are in
``robovast.client.exec_cli``: they need nothing the core provides, and while they lived
here they put ``robovast.common.config`` (pydantic), ``host_display`` and
``execute_local`` on every ``vast`` invocation, because `load_plugins()` imports each
``robovast.cli_plugins`` entry point eagerly.

This group attaches to ``vast exec`` through the ``robovast.exec_plugins`` entry-point
group, so it is listed without being imported and loads only when someone types
``vast exec local`` -- which is what keeps Docker out of ``vast login``.
"""

import os
import tempfile

import click

from robovast.client.errors import handle_cli_exception
from robovast.client.project_config import get_project_config
from robovast.common.common import load_config
from robovast.common.config import validate_config
from robovast.common.host_display import gui_by_default

from .execute_local import initialize_local_execution


@click.group()
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
              help='Use a custom Docker image (overrides execution.image and the '
                   'RoboVAST family default)')
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
        from robovast.execution.controller import (campaign_id_for, run_batch_campaign,
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
            # run.sh already bakes in the resolved image (config, else the family default).
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
                campaign_data = generate_scenario_variations(
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
