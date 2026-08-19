# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""``vast exec`` -- the group shell, and the two verbs that only drive a service.

Lives in the client, not in the core, because the *launch* path needs nothing the core
provides: pushing a project into a workspace and asking the service to run it is four HTTP
verbs. What does need the core is ``vast exec local`` (Docker, the config schema), and what
needs a kubeconfig is the operator half of ``vast exec cluster`` -- so those attach from
their own distributions through ``robovast.exec_plugins`` and stay unimportable until
typed.

The pay-off is not tidiness: ``load_plugins()`` imports every ``robovast.cli_plugins``
entry point on *every* ``vast`` invocation. While this shell lived in the core it dragged
``robovast.common.config`` (pydantic), ``host_display`` and ``execute_local`` into
``vast login``.
"""

import sys

import click

from robovast.client.errors import handle_cli_exception
from robovast.client.lazy_group import LazyPluginGroup
from robovast.client.service_target import echo_target as _echo_target
from robovast.client.service_target import service_client, target_options

#: Entry-point group for subcommands that attach to ``vast exec``.
EXEC_PLUGIN_GROUP = "robovast.exec_plugins"


@click.group(cls=LazyPluginGroup, plugin_group=EXEC_PLUGIN_GROUP)
def execution():
    """Execute scenarios locally or on a cluster.

    Run scenario configurations either locally using Docker or on a
    Kubernetes cluster for distributed execution.

    What this lists depends on what is installed: ``cluster`` comes with the client
    (its service-driven verbs) and grows its operator verbs with ``robovast-cluster``;
    ``local`` needs Docker and so comes with ``robovast``.
    """


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


#: How a verdict is shown. The symbols exist so a five-line report can be scanned at a glance;
#: the words stay because a symbol alone is not something anyone can act on or search for.
_VERDICT_MARKS = {
    "ok": ("ok", "green"),
    "upgradable": ("upgradable", "yellow"),
    "unknown": ("unknown", "yellow"),
    "blocked": ("BLOCKED", "red"),
}


@execution.command('check-retrigger')
@click.argument('campaign_id')
@target_options
def check_retrigger(campaign_id, namespace, context):  # pylint: disable=redefined-outer-name
    """Can this campaign be re-run? Reports every axis, and costs nothing.

    Answers before a retrigger rather than after: config version, host/container protocol,
    recorded images, third-party plugins and asset providers -- all five at once, because they
    fail independently and fixing one to discover the next is the thing this replaces.

    ``unknown`` is not a failure. A campaign recorded before a given field existed cannot say
    what it used, and refusing it on that basis would refuse exactly the old campaigns worth
    re-running. Exits non-zero only when an axis is genuinely blocked.
    """
    try:
        with service_client(namespace, context) as (client, label):
            _echo_target(label)
            report = client.check_retrigger(campaign_id)
    except Exception as e:  # noqa: BLE001
        handle_cli_exception(e)
        return

    click.echo(f"campaign: {report.campaign_id}")
    for name, axis in sorted(report.axes.items()):
        word, colour = _VERDICT_MARKS.get(axis.verdict, (axis.verdict, None))
        click.echo(f"  {name:<10} {click.style(word, fg=colour):<20} {axis.detail}")
    click.echo("")
    if report.runnable:
        click.echo(click.style("re-runnable", fg="green")
                   + f" -- vast exec retrigger {report.campaign_id}")
        return
    click.echo(click.style(f"NOT re-runnable: {', '.join(report.blocking)}", fg="red"))
    sys.exit(1)


@execution.command('retrigger')
@click.argument('campaign_id')
@click.option('--force', is_flag=True,
              help='Launch even when the pre-flight reports a blocking axis.')
@click.option('--to-workspace', 'to_workspace', default='', metavar='NAME',
              help='Do not launch. Materialise the campaign as a workspace with its config '
                   'migrated as far as it could be and a marker at every decision left, to '
                   'finish by hand. For a config no ladder step can carry forward.')
@target_options
def retrigger(campaign_id, force, to_workspace,  # pylint: disable=redefined-outer-name
              namespace, context):
    """Launch a NEW campaign from what CAMPAIGN_ID recorded. The source is not modified.

    Reuses the frozen config and the image the source recorded, so it runs the same code rather
    than today's. A config older than the current version is migrated into the staging copy;
    the archived one is left exactly as its author wrote it.

    The pre-flight runs first, because launching to discover the image is gone wastes the launch
    -- and its refusal names what is missing. ``--force`` proceeds anyway, which is worth having
    for an axis you have decided you understand.
    """
    try:
        with service_client(namespace, context) as (client, label):
            _echo_target(label)
            if to_workspace:
                _materialize_work_order(client, campaign_id, to_workspace)
                return
            report = client.check_retrigger(campaign_id)
            if not report.runnable and not force:
                for name in report.blocking:
                    click.echo(click.style(f"  {name}: ", fg="red")
                               + report.axes[name].detail, err=True)
                click.echo("", err=True)
                click.echo("refusing to retrigger. Run 'vast exec check-retrigger "
                           f"{campaign_id}' for the full report, or --force to proceed anyway.",
                           err=True)
                sys.exit(1)
            for name, axis in sorted(report.axes.items()):
                if axis.verdict in ("upgradable", "unknown"):
                    click.echo(f"note: {name}: {axis.detail}", err=True)
            ref = client.retrigger_campaign(campaign_id)
    except Exception as e:  # noqa: BLE001
        handle_cli_exception(e)
        return

    click.echo(f"retriggered {campaign_id} as {ref.campaign_id}")
    if getattr(ref, "note", ""):
        click.echo(f"note: {ref.note}")
    click.echo(f"  next: vast wait {ref.campaign_id}")


def _materialize_work_order(client, campaign_id: str, workspace_name: str):
    """Hand the campaign over as a workspace to finish by hand.

    Separate from the launch path because it is the opposite outcome: nothing is started, and what
    the caller gets is a file with work left in it. Printing the markers here rather than only in
    the service log is the point -- each one is a decision somebody has to make, and they are
    usually in different places in the file.
    """
    result = client.materialize_retrigger_workspace(campaign_id, workspace_name)
    click.echo(f"workspace '{result.workspace_id}' created from {campaign_id}")
    click.echo(f"  {result.config_path}: migrated as far as version {result.reached}")
    if result.capability:
        click.echo(f"  stopped at: {result.capability}")
    if not result.markers:
        click.echo(click.style("  no unresolved markers — validate and launch it normally",
                               fg="green"))
        return
    click.echo(click.style(f"  {len(result.markers)} decision(s) left:", fg="yellow"))
    for marker in result.markers:
        click.echo(f"    {marker.path}: {marker.reason}")
    click.echo("")
    click.echo("This will NOT validate until every marker is resolved, which is deliberate: a "
               "partly-migrated config that loaded would run a different experiment.")
    click.echo(f"  next: edit {result.config_path}, then 'vast configuration validate'")


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
