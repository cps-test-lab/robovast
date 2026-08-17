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
