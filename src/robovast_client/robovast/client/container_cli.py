# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""``vast container`` -- run a command in an experiment container, and stop the held one.

Named for what it acts on, like every other group. It was ``vast exec``, which named a
*verb* and then accumulated things that were not one: two launch paths and a set of
campaign operations, none of which are "execute a command in a container". Those moved to
the object each belongs to -- ``workspace run``, ``campaign``, ``service``, ``cluster`` --
and what is left is exactly what the name describes.

Everything here drives the service over HTTP and needs nothing else, so it ships with the
client.
"""

import sys

import click

from robovast.client.errors import handle_cli_exception
from robovast.client.service_target import echo_target as _echo_target
from robovast.client.service_target import service_client, target_options

@click.group()
def container():
    """Run a command in an experiment container, and stop the held one.

    For testing a container and its setup -- an import, a ``ros2 pkg list``, a file check
    -- before a campaign spends compute finding the same thing out.
    """


@container.command('exec')
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
    ``vast workspace run`` to run the experiment itself.

    Omit ``--config-name`` to check the bare image (``python3 -c 'import x'``,
    ``ros2 pkg list``, file checks). Name one to stage that configuration; an empty
    SHELL_COMMAND then starts its scenario, and its output goes to a log file inside the
    container rather than to stdout (the path is printed).

    There is at most one such container at a time; ``vast container stop``
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


@container.command('stop')
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
