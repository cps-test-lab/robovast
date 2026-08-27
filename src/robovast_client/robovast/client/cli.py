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

"""The ``vast`` command: the root group, and the verbs that only need a service.

The root group lives in the client distribution because every audience has one. A
service user installs ``robovast-client`` and gets ``vast login``, ``vast workspace``,
``vast files`` and ``vast campaign wait``; the core, the execution lanes and the operator
commands each attach their own verbs to this same group through the
``robovast.cli_plugins`` entry point. One command name for everybody, and the surface
grows with what is installed rather than listing verbs that cannot run.

Nothing here may import the core. That is what makes a client-only install a working
install rather than a broken one, and it is easy to lose to a single convenience import.
"""

import functools
import os
import sys
from importlib.metadata import entry_points

import click

from robovast.client.errors import handle_cli_exception
from robovast.client.logging_config import (get_logger, setup_default_logging,
                                            setup_logging)
from robovast.client.service_target import echo_target as _echo_target
from robovast.client.service_target import service_client, target_options
from robovast.client.tail import tail_chunks

logger = get_logger(__name__)


def configure_logging(ctx, param, value):
    """Callback to configure logging based on --log-level option."""
    if value is not None:
        # User explicitly specified log level, use it
        setup_logging(value)
        # Store in context for later use
        ctx.ensure_object(dict)
        ctx.obj['log_level'] = value
    else:
        setup_default_logging()
    return value


def _print_version(ctx, param, value):  # pylint: disable=unused-argument
    """``--version``, resolved from the running code rather than a named distribution.

    This was ``@click.version_option(package_name="robovast")``, which a client-only
    install does not have -- and click resolves that name lazily, in the callback, so the
    failure only appeared when someone asked. `running_version()` prefers the git revision
    (so "the fix I just made is loaded" is answerable), falls back to package metadata, and
    never raises. Computed in the callback, not at decoration time: a git call on every
    ``vast`` invocation is exactly the weight this distribution exists to avoid.
    """
    if not value or ctx.resilient_parsing:
        return
    from robovast.client.app_version import running_version
    click.echo(f"RoboVAST, version {running_version()}")
    ctx.exit()


@click.group()
@click.option('--log-level', '-l',
              type=click.Choice(['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'], case_sensitive=False),
              help='Set logging level (overrides project configuration)',
              callback=configure_logging,
              is_eager=True,
              expose_value=False)
@click.option('--version', is_flag=True, is_eager=True, expose_value=False,
              callback=_print_version,
              help='Show the version and exit.')
@click.pass_context
def cli(ctx):
    """VAST - RoboVAST Command-Line Interface.

    Main command for managing variations, executing scenarios,
    and analyzing results in the RoboVAST framework.

    The global ``--log-level`` option controls logging verbosity for any command.

    Every command names its own input: there is no ambient project, so nothing in a
    parent directory of your CWD decides which ``.vast`` a command reads or where its
    results go. A campaign runs a *workspace's* project (``vast workspace run``); the
    local verbs take the file as an argument (``vast config list my.vast``).

    Every command reads ``./.env`` first, so anything RoboVAST takes from the
    environment (share credentials, registry, ntfy, ``ROBOVAST_*_IMAGE``, …) can
    be kept there instead of exported by hand.

    Examples:
      vast --log-level DEBUG cluster cleanup
      vast config list my.vast
      vast workspace run my-experiment my.vast --description "pilot"

    See ``vast --help`` for a list of available commands.
    """
    # One .env read for the whole CLI, before any command runs: every variable in it
    # is simply part of the environment from here on, whichever command consumes it.
    # Per-command loaders drifted — a command that forgot to call one (or resolved a
    # value *before* calling it) silently ignored a configured .env line.
    #
    # Contributed rather than imported. Everything a `.env` carries -- share credentials,
    # ntfy, the registry, the image pins -- is consumed by the core and the lanes; a
    # client reads none of it, and making the root group import the reader would put
    # python-dotenv into a distribution whose whole point is three dependencies.
    run_startup_hooks()

    ctx.ensure_object(dict)


@cli.command()
@click.option('--flavor', default='', metavar='NAME',
              help='Also check what this cluster flavor needs (e.g. gcp).')
@click.option('--context', '-x', default=None, metavar='NAME',
              help='Kubernetes context to check (default: the active one).')
@click.option('--namespace', '-n', default='default', show_default=True, metavar='NAME',
              help='Namespace the robovast-service is deployed in. Checked for whether '
                   'that deployment can build experiment images.')
def doctor(flavor, context, namespace):
    """Check the prerequisites, before something else finds them the hard way.

    Reads only — safe to run at any time, which is what makes it usable both as the
    first step of an install and as the first step of debugging one.

    Every failure names its remedy: a check that reports "helm: missing" and stops has
    moved the problem rather than solved it.
    """
    from robovast.client.doctor import run_checks

    checks = run_checks(flavor=flavor, context=context, namespace=namespace)
    width = max(len(c.name) for c in checks)
    marks = {"ok": "✓", "warn": "⚠", "FAIL": "✗"}
    for check in checks:
        click.echo(f"  {marks[check.status]} {check.name:<{width}}  {check.detail}")

    failed = [c for c in checks if not c.ok and not c.optional]
    warned = [c for c in checks if not c.ok and c.optional]
    for check in failed + warned:
        click.echo(f"\n{marks[check.status]} {check.name}: {check.fix}")

    if failed:
        raise click.ClickException(
            f"{len(failed)} prerequisite(s) not met — see above.")
    click.echo("\n✓ ready")


def _login_remedy(exc):
    """The next thing to try, chosen by *why* the login failed.

    A single "check the URL and the token" line is actively misleading for the two
    failures a new user is most likely to hit. A certificate the CLI will not accept has
    nothing to do with the token -- and it is easy to conclude the opposite, because the
    same URL opens in a browser once the warning is clicked away, so the CLI looks like
    the broken part. It is not: the browser offers an exception, ``requests`` does not.
    """
    text = str(exc)
    if "certificate verify failed" in text or "SSLError" in text or "SSLCertVerificationError" in text:
        return ("The server's TLS certificate is not trusted, so this is not about the "
                "token — a browser can click past such a warning, this cannot.\n"
                "Ask the operator for a certificate your machine trusts; a publicly "
                "issued one (Let's Encrypt DNS-01) needs nothing installed here, while a "
                "private CA has to be added to this machine's trust store.")
    if "Connection refused" in text or "Name or service not known" in text \
            or "Failed to resolve" in text or "NewConnectionError" in text:
        return ("The address did not answer at all, so the token was never used. Check "
                "the URL, and that you are on the network the service is reachable from.")
    if "401" in text or "Unauthorized" in text:
        return "The service answered, but rejected the token. Ask the operator for the current one."
    return "Check the URL and the token the operator gave you."


@cli.command()
@click.argument('url', required=False)
@click.option('--token', default=None,
              help='The access token. Prompted for (hidden) when omitted.')
@click.option('--name', default=None,
              help='Display name shown on campaigns you start. Pass "" for none.')
@click.option('--link/--no-link', 'link', default=True, show_default=True,
              help='Also make the "vast" command resolvable outside this venv, by '
                   'symlinking it into a directory already on your login shell\'s PATH. '
                   'This is what lets an agent (or any new terminal) run it without '
                   'activating anything.')
def login(url, token, name, link):
    """Store the credentials for a robovast-service, so every command can reach it.

    \b
      vast login robovast.example.org

    The operator hands you the URL and the access token. A bare host is enough — the
    scheme is filled in (``https``, or ``http`` for loopback), so the address as the
    operator says it out loud is the address you can type. Both are kept per **user** in
    ``~/.config/robovast/config.json`` (mode 0600), not in a project ``.env``: which
    instance you talk to follows you rather than a checkout, and a token inside a
    project directory is one ``git add -A`` from being committed.

    The name is optional and **self-declared** — with one shared secret nobody can prove
    who they are, so it is a label for "who started this run?", not an identity. Give an
    empty one and campaigns you start are recorded as unattributed rather than as
    somebody invented.

    Run it again to change any of the three. ``vast logout`` forgets them.
    """
    from robovast.client import login as login_config

    stored_url, stored_token, stored_name = login_config.credentials()

    if not url:
        url = stored_url
        if not url:
            raise click.ClickException(
                "no service URL given and none stored — "
                "run 'vast login robovast.example.org'")
    try:
        url = login_config.normalize_url(url)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    if token is None:
        # Only offer the stored token as a default when the URL is unchanged; a token
        # is per-instance, so silently reusing one against a different service would
        # send someone's secret somewhere it does not belong.
        reuse = stored_token if url == stored_url else ''
        token = click.prompt('Access token', hide_input=True, default=reuse,
                             show_default=False) if not reuse else reuse
    if name is None:
        default_name = stored_name or login_config.default_name()
        name = click.prompt('Your name (optional)', default=default_name,
                            show_default=bool(default_name))

    # Verify before storing, so a typo is caught here rather than surfacing as a 401
    # from the next unrelated command.
    from robovast.service.http_client import RobovastClient
    client = RobovastClient(url, token=token, user=name)
    try:
        client.version()
    except Exception as exc:  # noqa: BLE001 - every failure means "not logged in"
        raise click.ClickException(
            f"could not reach {url}: {exc}\n{_login_remedy(exc)}") from exc

    path = login_config.save(url, token, name)
    click.echo(f"✓ logged in to {url}")
    click.echo(f"  as {name}" if name else "  without a name (campaigns stay unattributed)")
    click.echo(f"  stored in {path}")

    # An agent over HTTP does not read this config, so it needs the same three facts
    # spelled out -- the name included, or its campaigns arrive unattributed while the
    # same person's CLI runs are labeled.
    click.echo("\nTo give an agent the same access over HTTP:")
    click.echo("  " + " \\\n      ".join(
        login_config.mcp_add_command(url, token, name)))

    # An agent's shell is not the one you ran this in: it is started from your profile,
    # with no venv activated. Storing credentials it can use, while leaving the command
    # it must run unreachable, gets it exactly halfway.
    if link:
        linked, message = login_config.link_cli()
        click.echo(f"\n{'✓' if linked else '✗'} {message}", err=not linked)


@cli.command()
def logout():
    """Forget the stored robovast-service credentials."""
    from robovast.client import login as login_config
    if login_config.clear():
        click.echo("✓ logged out")
    else:
        click.echo("Not logged in.")


# ---------------------------------------------------------------------------
# workspace — server-side editable project inputs (thin client of the service)
# ---------------------------------------------------------------------------


@cli.group()
def workspace():
    """Manage server-side workspaces (editable project inputs).

    A workspace holds a self-contained project (``.vast`` + scenario/run files) on
    the service, independent of any CWD. These commands mirror the MCP workspace
    tools and drive the same interface.

    A workspace lives in the store of whichever service you talk to, so every
    command here prints the target it resolved: a service on the conventional local
    port if one answers, otherwise the one ``vast login`` stored.
    """


#: Directory names skipped by ``workspace init`` — campaign outputs, not project inputs.
#: Only the conventional name; a results tree under any OTHER name is caught by content
#: instead (``is_campaign_results_dir``), which is what covers a downloaded campaign
#: sitting at the project root under its campaign id. Both are reported, never silent.
_INIT_EXCLUDE_DIRS = {'results'}


@workspace.command('init')
@click.argument('directory', type=click.Path(exists=True, file_okay=False))
@click.option('--name', default='', help='Workspace name (default: the directory name).')
@click.option('--exclude', 'excludes', multiple=True, metavar='NAME',
              help='Directory name to skip (repeatable). Adds to the default '
                   f"{sorted(_INIT_EXCLUDE_DIRS)}.")
@click.option('--include-results', is_flag=True,
              help='Upload campaign results trees too. Off by default: they are a '
                   "campaign's OUTPUT, so pushing them back as project input uploads "
                   'every past campaign on disk. What was skipped is always reported.')
@target_options
def workspace_init(directory, name, excludes, include_results, namespace, context):
    """Create a workspace and upload every file from DIRECTORY into it.

    ``.vast``/``.osc`` are written inline; all other files go through the upload
    side channel (executability preserved). Hidden files/dirs (``.cache`` etc.),
    output dirs (``results/``, plus any ``--exclude``) and any campaign results tree
    — recognised by its contents, so a downloaded campaign under its own id is caught
    too — are skipped, and every skip is reported. Prints the new workspace id.

    \b
      vast workspace init configs/examples/growth_sim   # into the resolved service
    """
    from pathlib import Path

    from robovast.service.interface import CreateWorkspaceRequest
    from robovast.service.project_push import sync_directory_to_workspace
    root = Path(directory).resolve()
    skip_dirs = _INIT_EXCLUDE_DIRS | set(excludes)
    with service_client(namespace, context) as (client, target):
        _echo_target(target)
        requested = name or root.name
        ws = client.create_workspace(CreateWorkspaceRequest(name=requested))
        wid = ws.workspace_id
        if ws.name != requested:
            # Server auto-suffixes a colliding name (foo → foo-2). Let the user
            # accept the suggestion (default) or back out; on decline, drop the
            # just-created workspace so nothing half-initialized is left behind.
            if sys.stdin.isatty() and not click.confirm(
                    f"name {requested!r} already exists — use {ws.name!r} instead?",
                    default=True):
                client.delete_workspace(wid)
                raise click.ClickException("aborted; workspace not created")
            click.echo(f"note: name {requested!r} already exists — using {ws.name!r}")

        stats = sync_directory_to_workspace(
            client, wid, root, skip_dirs=skip_dirs,
            include_results=include_results, echo=click.echo)
        count = stats['written'] + stats['uploaded']
        click.echo(
            f"workspace {wid} ({ws.name}) initialized from {root} ({count} files)")


@workspace.command('update')
@click.argument('workspace', metavar='WORKSPACE')
@click.argument('directory', type=click.Path(exists=True, file_okay=False))
@click.option('--exclude', 'excludes', multiple=True, metavar='NAME',
              help='Directory name to skip (repeatable). Adds to the default '
                   f"{sorted(_INIT_EXCLUDE_DIRS)}.")
@click.option('--prune', is_flag=True,
              help='Also delete workspace files that are absent from DIRECTORY '
                   '(full mirror). Off by default: update only adds/overwrites.')
@click.option('--include-results', is_flag=True,
              help='Upload campaign results trees too. Off by default: they are a '
                   "campaign's OUTPUT, so pushing them back as project input uploads "
                   'every past campaign on disk. What was skipped is always reported.')
@target_options
def workspace_update(workspace, directory, excludes, prune, include_results, namespace, context):  # pylint: disable=redefined-outer-name
    """Re-sync DIRECTORY into an EXISTING workspace (id or name).

    Uploads every file from DIRECTORY, overwriting in place — ``.vast``/``.osc``
    inline, everything else via the upload side channel. Hidden files/dirs,
    output dirs (``results/``, plus any ``--exclude``) and any campaign results tree
    (recognised by its contents) are skipped, and every skip is reported. With
    ``--prune`` it also removes workspace files that no longer exist locally, so
    the workspace mirrors DIRECTORY exactly.

    \b
      vast workspace update ws-ab12 configs/examples/growth_sim
      vast workspace update growth_sim configs/examples/growth_sim --prune
    """
    from pathlib import Path

    from robovast.service.project_push import _resolve_workspace_id, sync_directory_to_workspace
    root = Path(directory).resolve()
    skip_dirs = _INIT_EXCLUDE_DIRS | set(excludes)
    with service_client(namespace, context) as (client, target):
        _echo_target(target)
        wid = _resolve_workspace_id(client, workspace)
        stats = sync_directory_to_workspace(
            client, wid, root, skip_dirs=skip_dirs, prune=prune,
            include_results=include_results, echo=click.echo)
        count = stats['written'] + stats['uploaded']
        summary = f"{count} written"
        if prune:
            summary += f", {stats['pruned']} pruned"
        click.echo(f"workspace {wid} updated from {root} ({summary})")


@workspace.command('download')
@click.argument('workspace_id')
@click.argument('directory', type=click.Path(file_okay=False))
@click.option('--overwrite', is_flag=True,
              help='Replace local files that already exist. Off by default: pulling over an '
                   'edited copy of the same project would lose those edits irrecoverably.')
@target_options
def workspace_download(workspace_id, directory, overwrite, namespace, context):
    """Fetch every file in WORKSPACE_ID into DIRECTORY.

    The other direction of ``workspace init`` / ``update``, so a project can be taken off a
    remote service and worked on locally -- and so a workspace somebody else authored can be
    inspected without the web UI.

    File by file over the existing calls rather than as one archive: a workspace is a source
    project, where that is adequate. A campaign is the case that needs an archive, because it
    holds rosbags -- see ``vast results download``.

    \b
      vast workspace download growth-sim ./growth-sim
    """
    from robovast.service.project_push import pull_workspace_to_directory

    with service_client(namespace, context) as (client, target):
        _echo_target(target)
        try:
            counts = pull_workspace_to_directory(client, workspace_id, directory,
                                                 overwrite=overwrite, echo=click.echo)
        except FileExistsError as e:
            raise click.ClickException(str(e)) from e
    click.echo(f"fetched {counts['fetched']} file(s) into {directory}")


@workspace.command('list')
@target_options
def workspace_list(namespace, context):
    """List workspaces (newest first)."""
    with service_client(namespace, context) as (client, target):
        _echo_target(target)
        workspaces = client.list_workspaces().workspaces
        if not workspaces:
            click.echo("(none)")
        for w in workspaces:
            click.echo(f"{w.workspace_id}  {w.name or '-':20}  {w.created_at or ''}")


@workspace.command('world')
@click.argument('workspace', metavar='WORKSPACE')
@click.option('--path', default='', metavar='VAST',
              help='Which .vast in a multi-.vast workspace (default: the only one).')
@click.option('--targets', default='', metavar='GLOB',
              help='Also report the objects matching GLOB whose model values a run may '
                   'override, with their current values. Costs a model build.')
@click.option('--entities', is_flag=True,
              help='Also list the entities the world compiles. Costs a model build.')
@click.option('--json', 'as_json', is_flag=True, help='Print the raw description as JSON.')
@target_options
def workspace_world(workspace, path, targets, entities, as_json, namespace, context):  # pylint: disable=redefined-outer-name
    """Describe the world this campaign's simulator will load.

    The other half of authoring a ``sim:`` override: ``vast workspace world`` says what the
    world *offers* — which components an override can address, and with ``--targets`` which model
    values a run may change at all. Both are otherwise only refused inside the container, after
    the image pull.

    Answered by the simulator, in the image the campaign runs, so the reply names that image:
    which world a ref resolves to depends on what is installed there.

    \b
      vast workspace world tiago_pick
      vast workspace world tiago_pick --targets 'gripper_right*' --json
    """
    import json as json_mod

    from robovast.service.project_push import _resolve_workspace_id
    with service_client(namespace, context) as (client, target):
        _echo_target(target)
        wid = _resolve_workspace_id(client, workspace)
        described = client.describe_world(wid, path, targets, entities)
        if as_json:
            click.echo(json_mod.dumps(described.model_dump(), indent=2))
            return
        click.echo(f"world:   {described.world}"
                   f"{' (packaged)' if described.packaged else ''}")
        click.echo(f"asked:   {described.backend} in {described.image} "
                   f"({described.duration_s:.1f}s)")
        for plugin in described.components:
            click.echo(f"  plugin {plugin.get('key')}  ({len(plugin.get('paths') or [])} paths)")
        if described.entities is not None:
            click.echo(f"  entities: {', '.join(described.entities) or '(none)'}")
        fields = (described.overridable or {}).get("fields") or []
        if fields:
            click.echo(f"  overridable fields: {', '.join(f['field'] for f in fields)}")
        for namespace_name, rows in ((described.overridable or {}).get("targets") or {}).items():
            for row in rows:
                values = {k: v for k, v in row.items() if k not in ("name", "body")}
                click.echo(f"  {namespace_name} {row.get('name')}: {values}")


@workspace.command('validate')
@click.argument('workspace', metavar='WORKSPACE')
@click.argument('vast_path', metavar='[VAST]', required=False, default='')
@click.option('--no-world-check', is_flag=True,
              help='Skip the world check, which builds the model in a container. Faster, '
                   'and the only part of validation that costs more than a moment.')
@target_options
def workspace_validate(workspace, vast_path, no_world_check, namespace, context):  # pylint: disable=redefined-outer-name
    """Check a project before spending any compute on it.

    Reports **every** problem at once rather than the first, because they fail
    independently and fixing them one launch at a time is the expensive way to find out.

    Computed service-side: the checks need the config schema, the scenario parser and
    (unless ``--no-world-check``) the simulator, none of which a client install has. So
    this works the same whether the service is local or remote.
    """
    try:
        from robovast.service.project_push import \
            _resolve_workspace_id  # pylint: disable=import-outside-toplevel

        with service_client(namespace, context) as (client, target):
            _echo_target(target)
            workspace_id = _resolve_workspace_id(client, workspace)
            report = client.validate_project(
                workspace_id, path=vast_path, check_world=not no_world_check)

            for problem in report.problems:
                where = " ".join(p for p in (problem.config, problem.field) if p)
                location = f" [{where}]" if where else ""
                click.echo(f"  {problem.stage}{location}: {problem.message}")

            if not report.valid:
                raise click.ClickException(
                    f"{len(report.problems)} problem(s) — fix them and validate again.")
            click.echo(
                f"✓ valid: {report.configs} configuration(s) × "
                f"{report.runs_per_config} run(s) = {report.total_trials} trial(s)")
    # pylint: disable-next=try-except-raise
    except (click.UsageError, click.ClickException):
        raise
    except Exception as e:
        handle_cli_exception(e)


@workspace.command('preview')
@click.argument('workspace', metavar='WORKSPACE')
@click.argument('vast_path', metavar='[VAST]', required=False, default='')
@click.option('--max-configs', type=int, default=0, show_default=True,
              help='Show at most this many configurations (0 = all).')
@target_options
def workspace_preview(workspace, vast_path, max_configs, namespace, context):  # pylint: disable=redefined-outer-name
    """Show what the sweep expands to, without running any of it.

    The answer to "how many configurations will this be" — asked before a launch rather
    than discovered from a campaign that turned out to be ten times the intended size.
    """
    try:
        from robovast.service.project_push import \
            _resolve_workspace_id  # pylint: disable=import-outside-toplevel

        with service_client(namespace, context) as (client, target):
            _echo_target(target)
            workspace_id = _resolve_workspace_id(client, workspace)
            preview = client.preview_configurations(
                workspace_id, max_configs=max_configs, path=vast_path)
            for configuration in preview.configurations:
                click.echo(f"  {configuration.name}")
            if preview.truncated:
                click.echo(f"  … (--max-configs {max_configs} reached)")
            click.echo(f"{preview.configs} configuration(s) × "
                       f"{preview.runs_per_config} run(s) = "
                       f"{preview.total_trials} trial(s)")
    # pylint: disable-next=try-except-raise
    except (click.UsageError, click.ClickException):
        raise
    except Exception as e:
        handle_cli_exception(e)


@workspace.command('run')
@click.argument('workspace', metavar='WORKSPACE')
@click.argument('vast_path', metavar='[VAST]', required=False, default='')
@click.option('--push', 'push_dir', default=None, metavar='DIR',
              type=click.Path(exists=True, file_okay=False),
              help='Sync DIR into WORKSPACE before launching, creating the workspace if '
                   'it does not exist. The two-step form (workspace init/update, then '
                   'run) is the same thing spelled out.')
@click.option('--filter', 'config_filter', default='', metavar='GLOB',
              help='Run only configurations matching this name or glob (e.g. hall*).')
@click.option('--runs', '-r', type=int, default=None,
              help="Override execution.runs (default: the value in the .vast).")
@click.option('--campaign-name', default=None,
              help='Override the campaign name; the id becomes <name>-<timestamp>.')
@click.option('--description', default=None, metavar='TEXT',
              help='One line saying what this run is for. It is what tells two '
                   'same-day <name>-<timestamp> campaigns apart in the web UI.')
@click.option('--upload-to-share', 'upload_to_share', is_flag=True,
              help='Stream a raw (pre-postprocess) archive to the configured share '
                   'when the campaign finishes.')
@click.option('--show-gui', 'show_gui', is_flag=True,
              help="Watch ONE run in the simulator's window (never a sweep). Honoured "
                   'only by a service on a local Docker lane, which is the only '
                   'deployment whose docker process sits at a screen; every other lane '
                   'refuses rather than running windowless.')
@click.option('--allow-opaque-image', is_flag=True,
              help='Launch even though a container names an image that declares no '
                   "provenance:. Refused by default because nothing in the results "
                   'could then say what ran; the exemption is recorded on the campaign.')
@click.option('--image-project', 'image_project', default=None, metavar='PROJECT',
              help='Registry/namespace to take the RoboVAST images from for this run, '
                   'overriding ROBOVAST_PROJECT. Affects only images RoboVAST '
                   'publishes — a container image your .vast names is run as written.')
@click.option('--image-project-tag', 'image_project_tag', default=None, metavar='TAG',
              help='Tag to take those images at (default: ROBOVAST_PROJECT_TAG, else '
                   'latest).')
@click.option('--wait-and-download', 'wait_and_download', is_flag=True,
              help='Block until the campaign finishes and its results are uploaded, '
                   'then download its archive into the current directory.')
@click.option('--poll-interval', type=float, default=5.0, show_default=True,
              help='Seconds between status polls when --wait-and-download is set.')
@target_options
def workspace_run(workspace, vast_path, push_dir, config_filter, runs,  # pylint: disable=redefined-outer-name
                  campaign_name, description, upload_to_share, show_gui,
                  allow_opaque_image, image_project, image_project_tag,
                  wait_and_download, poll_interval, namespace, context):
    """Run a ``.vast`` — the one way to start a campaign from a project.

    WORKSPACE is a ``ws-…`` id or a workspace name. VAST is the path to the ``.vast``
    *within* that workspace; omit it when the workspace holds exactly one and the
    service will resolve it, naming the candidates if there are several.

    A workspace is a directory holding files and possibly several ``.vast`` files; a
    project is one ``.vast`` and the files it references. So the pair (workspace, path)
    names the project to run, which is exactly what the service accepts —
    ``workspace_id`` is the only project binding it takes.

    Fire-and-forget by default: it returns once the campaign is launched. Track it with
    ``vast campaign wait <campaign-id>`` or the web UI. ``--wait-and-download`` instead blocks
    until it finishes and pulls the archive down.

    \b
      vast workspace init .                      # push a workspace
      vast workspace run my-experiment my.vast --description "pilot"
      vast workspace run my-experiment --push .  # push and launch in one step
    """
    try:
        from robovast.execution.campaign_wait import \
            wait_for_campaign_outcome  # pylint: disable=import-outside-toplevel
        from robovast.service.interface import (  # pylint: disable=import-outside-toplevel
            DESCRIPTION_MAX_LEN, CreateCampaignRequest, CreateWorkspaceRequest)
        from robovast.service.project_push import (  # pylint: disable=import-outside-toplevel
            _resolve_workspace_id, download_campaign_archive,
            sync_directory_to_workspace)

        # Checked here rather than left to the request model: this says what to do
        # instead of surfacing a pydantic validation string, and it refuses before
        # anything is pushed or launched.
        if description and len(description) > DESCRIPTION_MAX_LEN:
            raise click.ClickException(
                f"--description is {len(description)} characters; the limit is "
                f"{DESCRIPTION_MAX_LEN} — shorten it to one line.")

        # The flag beats ROBOVAST_PROJECT; unset means "whatever the .env said".
        # Resolved client-side because the service cannot see the client's .env, and
        # echoed because an override leaves no trace in the .vast.
        project = image_project if image_project is not None else os.environ.get(
            'ROBOVAST_PROJECT', '')
        project_tag = image_project_tag if image_project_tag is not None else os.environ.get(
            'ROBOVAST_PROJECT_TAG', '')

        with service_client(namespace, context) as (client, target):
            _echo_target(target)

            if push_dir is not None:
                workspace_id = _resolve_or_create_workspace(
                    client, workspace, CreateWorkspaceRequest)
                click.echo(f"Pushing {push_dir} into workspace {workspace_id} ...")
                sync_directory_to_workspace(
                    client, workspace_id, push_dir,
                    skip_dirs=_INIT_EXCLUDE_DIRS, echo=click.echo)
            else:
                workspace_id = _resolve_workspace_id(client, workspace)

            if project:
                click.echo(f"Images from {project} at "
                           f"{project_tag or 'latest'} (this run only).")

            ref = client.create_campaign(CreateCampaignRequest(
                workspace_id=workspace_id, config_path=vast_path,
                config_filter=config_filter,
                # 0, not 1: the service reads a non-positive count as "use the .vast's
                # execution.runs", and a substitute for "unset" would shrink the
                # campaign without failing anything.
                runs=runs or 0,
                campaign_name=campaign_name or "", description=description or "",
                upload_to_share=upload_to_share, show_gui=show_gui,
                allow_opaque_image=allow_opaque_image,
                image_project=project, image_project_tag=project_tag))
            cid = ref.campaign_id

            if not wait_and_download:
                click.echo(f"Launched campaign '{cid}'. "
                           f"Track it with 'vast campaign wait {cid}' or the web UI.")
                return

            click.echo(f"Launched campaign '{cid}'. Waiting for it to finish...")
            outcome = wait_for_campaign_outcome(
                cid, client=client, interval=poll_interval, feedback=click.echo)
            if outcome == "failed":
                raise click.ClickException(
                    f"Campaign '{cid}' failed. Its status carries the failure reason: "
                    f"see 'vast campaign log {cid}' or the web UI.")

            click.echo(f"Campaign '{cid}' finished. Downloading its archive...")
            # The service streams the campaign from the object store — no external
            # share needed for delivery. An archive, not an unpacked tree: what to do
            # with it is the caller's, exactly as for `vast results download`.
            dest = download_campaign_archive(
                client, cid, os.path.join(os.getcwd(), f"{cid}.tar.gz"))
            click.echo(f"Wrote {dest}")
    # The bare re-raise is deliberate: click handles UsageError/ClickException itself,
    # printing usage and setting the exit code, so they must pass the broad handler below.
    # pylint: disable-next=try-except-raise
    except (click.UsageError, click.ClickException):
        raise
    except Exception as e:
        handle_cli_exception(e)


def _resolve_or_create_workspace(client, workspace, create_request_cls):
    """The id for *workspace*, creating it if the name is not taken yet.

    Only for ``--push``: pushing into a name that does not exist yet is the
    one-command form of ``workspace init``, so refusing would make the flag useless for
    a first launch. A ``ws-…`` id that does not exist is still an error — that is a typo,
    not a name to claim.
    """
    from robovast.service.project_push import \
        _resolve_workspace_id  # pylint: disable=import-outside-toplevel
    try:
        return _resolve_workspace_id(client, workspace)
    except ValueError:
        if workspace.startswith("ws-"):
            raise
        created = client.create_workspace(create_request_cls(name=workspace))
        click.echo(f"Created workspace {created.workspace_id} ({created.name}).")
        return created.workspace_id


@workspace.command('delete')
@click.argument('workspace', metavar='WORKSPACE')
@target_options
def workspace_delete(workspace, namespace, context):  # pylint: disable=redefined-outer-name
    """Delete a workspace and its inputs (existing campaigns are unaffected).

    WORKSPACE may be a ``ws-…`` id or a workspace name (unique names resolve;
    ambiguous ones must be deleted by id).
    """
    with service_client(namespace, context) as (client, target):
        _echo_target(target)
        res = client.delete_workspace(workspace)
        click.echo(res.message or ('deleted' if res.ok else 'failed'))


# ---------------------------------------------------------------------------
# files — one address space over campaign results and workspace inputs
# ---------------------------------------------------------------------------


def _file_errors(func):
    """Report the address space's refusals as messages, not tracebacks.

    Every one of them is a *caller* error with an actionable message already attached —
    a read-only namespace, a malformed address, a binary file, a missing campaign. The
    interface raises ``ValueError``/``KeyError`` so the HTTP layer can map them to
    400/404; on the CLI the same information belongs on one line.

    ``ServiceError`` is in the list for the same refusals arriving over HTTP. Catching only
    the in-process types meant a local ``vast files cat`` on a missing path printed one
    clean line while the identical command against a remote service printed a 40-frame
    traceback ending in ``ServiceError: Not Found`` — the same refusal, told two ways,
    decided by where the service happened to be.
    """
    @functools.wraps(func)
    def _wrapped(*args, **kwargs):
        from robovast.service.interface import \
            ServiceError  # pylint: disable=import-outside-toplevel
        try:
            return func(*args, **kwargs)
        except (ValueError, KeyError, ServiceError) as e:
            # ``str(KeyError("x"))`` is ``"'x'"`` — take the argument itself, or the
            # message would be shown wrapped in stray quotes.
            message = e.args[0] if isinstance(e, KeyError) and e.args else str(e)
            raise click.ClickException(str(message)) from e
    return _wrapped


@cli.group()
def files():
    """Read and write files by address.

    Every file the service can reach has one address, which is also the URL that
    serves it:

    \b
      /results/<campaign_id>/<path>    a campaign's outputs — read-only
      /sources/<workspace_id>/<path>   a workspace's inputs — writable

    The path after the owner is the real on-disk path: a run artifact is
    ``<config_name>/<run_id>/<file>``. A trailing slash lists a directory.

    \b
      vast files ls  /results/nav-2026-03-04-152130/
      vast files cat /results/nav-2026-03-04-152130/_execution/outcome.json
      vast files put /sources/ws-ab12/demo.vast ./demo.vast
    """


@files.command('ls')
@click.argument('address')
@click.option('--recursive', '-r', is_flag=True,
              help='Walk the whole subtree (files only).')
@click.option('--detail', '-l', is_flag=True, help='Show sizes.')
@click.option('--limit', default=100, show_default=True, help='Maximum entries.')
@click.option('--offset', default=0, help='First entry to show.')
@target_options
@_file_errors
def files_ls(address, recursive, detail, limit, offset, namespace, context):
    """List the directory at ADDRESS (a trailing slash is optional)."""
    with service_client(namespace, context) as (client, target):
        _echo_target(target)
        listing = client.list_files(address, recursive=recursive, detail=detail,
                                    offset=offset, limit=limit)
        if detail:
            for e in listing.detailed:
                size = '-' if e.bytes is None else str(e.bytes)
                click.echo(f"{size:>12}  {e.name}{'/' if e.is_dir else ''}")
        else:
            for name in listing.entries:
                click.echo(name)
        shown = len(listing.detailed if detail else listing.entries)
        if listing.truncated:
            click.echo(f"({shown} of {listing.total} — raise --limit or page with "
                       f"--offset {offset + shown})")


@files.command('cat')
@click.argument('address')
@click.option('--lines', default=200, show_default=True, help='Maximum lines.')
@click.option('--offset', default=0, help='First line to print.')
@target_options
@_file_errors
def files_cat(address, lines, offset, namespace, context):
    """Print a page of the text file at ADDRESS (binary files → ``vast files get``)."""
    with service_client(namespace, context) as (client, target):
        _echo_target(target)
        page = client.read_file(address, lines=lines, offset=offset)
        click.echo(page.content)
        if page.offset + page.returned_lines < page.total_lines:
            click.echo(f"({page.offset + page.returned_lines} of {page.total_lines} "
                       f"lines — continue with --offset "
                       f"{page.offset + page.returned_lines})", err=True)


@files.command('get')
@click.argument('address')
@click.argument('destination', type=click.Path(dir_okay=False))
@target_options
@_file_errors
def files_get(address, destination, namespace, context):
    """Download the file at ADDRESS to DESTINATION, bytes intact.

    The way to fetch one binary artifact (a rosbag, a mesh, a rendered plot) without
    downloading the campaign archive around it.
    """
    from pathlib import Path
    with service_client(namespace, context) as (client, target):
        _echo_target(target)
        data = client.read_file_bytes(address)
    Path(destination).write_bytes(data)
    click.echo(f"wrote {len(data)} bytes to {destination}")


@files.command('put')
@click.argument('address')
@click.argument('source', type=click.Path(exists=True, dir_okay=False))
@target_options
@_file_errors
def files_put(address, source, namespace, context):
    """Upload SOURCE to ADDRESS (``/sources/…`` only — results are immutable).

    ``.vast``/``.osc`` are written inline; every other type goes through the upload
    side channel with its executable bit preserved.
    """
    from pathlib import Path

    from robovast.service.project_push import push_file
    with service_client(namespace, context) as (client, target):
        _echo_target(target)
        kind = push_file(client, address, Path(source))
    click.echo(f"{kind} {address}")


@files.command('rm')
@click.argument('address')
@target_options
@_file_errors
def files_rm(address, namespace, context):
    """Delete the file at ADDRESS (``/sources/…`` only)."""
    with service_client(namespace, context) as (client, target):
        _echo_target(target)
        res = client.delete_file(address)
        click.echo(res.message or ('deleted' if res.ok else 'failed'))


@cli.command()
def install_completion():
    """Install shell completion for the vast command.

    Auto-detects your shell and installs completion to the appropriate config file.

    """

    # Auto-detect shell from SHELL environment variable
    shell_env = os.environ.get('SHELL', '')
    if 'zsh' in shell_env:
        shell = 'zsh'
    elif 'fish' in shell_env:
        shell = 'fish'
    else:
        shell = 'bash'

    # Generate completion script based on shell
    script = None
    if shell == 'bash':
        script = 'eval "$(_VAST_COMPLETE=bash_source vast)"'
        config_file = os.path.expanduser('~/.bashrc')
    elif shell == 'zsh':
        script = 'eval "$(_VAST_COMPLETE=zsh_source vast)"'
        config_file = os.path.expanduser('~/.zshrc')
    elif shell == 'fish':
        script = '_VAST_COMPLETE=fish_source vast | source'
        config_file = os.path.expanduser('~/.config/fish/config.fish')
    else:
        raise click.ClickException(f"Unsupported shell for completion installation: {shell}")

    # Install to the config file
    try:
        # Ensure directory exists
        os.makedirs(os.path.dirname(config_file), exist_ok=True)

        # Check if completion is already installed
        if os.path.exists(config_file):
            with open(config_file, 'r') as f:
                content = f.read()
                if script in content:
                    click.echo(f"✓ Completion already installed in {config_file}")
                    return

        # Append completion script to config file
        with open(config_file, 'a') as f:
            f.write(f"\n# VAST CLI completion\n{script}\n")

        click.echo(f"✓ Completion installed successfully!")
        click.echo(f"  Shell: {shell}")
        click.echo(f"  Added to: {config_file}")
        click.echo()
        click.echo("Restart your shell or run:")
        click.echo(f"  source {config_file}")
    except Exception as e:
        # `click.Exit` does not exist -- raising it turned any completion-install
        # failure into an AttributeError that hid the failure it was reporting.
        raise click.ClickException(f"Failed to install completion: {e}") from e


@cli.group()
def image():
    """Build the derived images a project's containers declare.

    Mirrors the ``build_experiment_image`` MCP tools and drives the same interface.
    Registry-free: you name a project; the service builds every container in
    ``execution.containers`` that adds ``system_packages`` or ``python_packages``,
    and each image is referenced as ``build:<container>`` in ``execution.image``.
    Every command prints the target it resolved — a service on the conventional local
    port if one answers, otherwise the one ``vast login`` stored.
    """


@image.command('build')
@click.option('--workspace-id', required=True,
              help='Workspace whose project to build. Required: the service runs a '
                   "workspace's project, never a CWD one.")
@click.option('--config-path', default='', help='Which .vast when the workspace has several.')
@click.option('--wait/--no-wait', default=True, help='Wait for the build to finish (default).')
@target_options
def image_build(workspace_id, config_path, wait, namespace, context):  # pylint: disable=redefined-outer-name
    """Build (or reuse) the images the project's containers declare.

    Zero or more: one per container in ``execution.containers`` that adds packages.
    The workspace is what names the project, and the project is what decides which
    containers build -- there is no CWD project and no single "the" image.
    """
    from robovast.service.interface import BuildImageRequest
    with service_client(namespace, context) as (client, target):
        _echo_target(target)
        ref = client.build_image(BuildImageRequest(
            workspace_id=workspace_id, config_path=config_path))
        # Report every container, not just the one the handle happens to name. Both lines
        # below used to say only `ref.tag`, so a project building two images printed one
        # cache-hit line for the scenario image and never mentioned the other — a reader
        # could not tell whether the second was covered, still building, or absent. The
        # per-container verdict is in `cached_builds`; the aggregate `ref.cached` is now
        # its conjunction, so a cache-hit line means every image, which is what it reads as.
        cached_builds = getattr(ref, "cached_builds", None) or {}
        for name in sorted(cached_builds):
            if cached_builds[name]:
                click.echo(f"✓ image 'build:{name}' already up to date (cache hit)")
        if ref.cached:
            if not cached_builds:      # a service predating the per-container verdicts
                click.echo(f"✓ image 'build:{ref.tag}' already up to date (cache hit)")
            return
        # Wait only on what is actually building. Waiting on a cache hit is harmless but
        # says "building" about an image that is already there.
        pending = {name: bid for name, bid in (ref.builds or {}).items()
                   if not cached_builds.get(name)}
        ids = list(pending.values()) or list((ref.builds or {}).values()) or [ref.build_id]
        for name, bid in sorted(pending.items()):
            click.echo(f"building 'build:{name}' (build_id={bid}) ...")
        if not pending:
            click.echo(f"building 'build:{ref.tag}' (build_id={' '.join(ids)}) ...")
        if not wait:
            click.echo(f"started; wait with 'vast image wait {' '.join(ids)}'")
            return
        _wait_for_builds(client, ids, interval=2.0, timeout=None)


def _wait_for_builds(client, build_ids, *, interval, timeout):
    """Wait, report, and exit non-zero on failure — shared by ``image build`` and ``image wait``.

    Failure detail is the point of doing this in one place: ``error.entry`` and
    ``fixable_by`` say *what to change*, and a caller that prints only "build failed"
    sends the reader to the builder log for something the status already knew.
    """
    from robovast.execution.image_build_wait import (SUCCESS_PHASES,
                                                     wait_for_image_builds)
    from robovast.execution.poll_health import PollsStopped
    try:
        done = wait_for_image_builds(build_ids, client=client, interval=interval,
                                     timeout=timeout, feedback=click.echo)
    except TimeoutError as e:
        # As `vast campaign wait`: the caller stopped waiting, the builds did not stop building.
        # A distinct code keeps that apart from a build that actually failed.
        click.echo(str(e), err=True)
        raise SystemExit(2) from e
    except PollsStopped as e:
        # Also "stopped waiting", hence the same code -- but for the opposite reason, and
        # exiting 1 here would report a perfectly healthy build as failed.
        click.echo(str(e), err=True)
        raise SystemExit(2) from e
    failed = False
    for build_id, status in done.items():
        if status.phase in SUCCESS_PHASES:
            click.echo(f"✓ built 'build:{status.tag}'")
            continue
        failed = True
        err = status.error
        if err:
            click.echo(f"✗ {build_id} failed [{err.phase}] {err.message}", err=True)
            # ``fixable_by`` used to print only alongside an ``entry``, which meant it never
            # printed for an infra failure -- the one case where "this is not yours to fix"
            # is the whole message. It is the more important half of the two, so it is
            # unconditional and the entry rides along when there is one.
            where = f", offending entry: {err.entry}" if err.entry else ""
            click.echo(f"  fixable_by={err.fixable_by}{where}", err=True)
        else:
            click.echo(f"✗ {build_id} failed", err=True)
    if failed:
        sys.exit(1)


@image.command('wait')
@click.argument('build_ids', metavar='BUILD_ID...', nargs=-1, required=True)
@click.option('--interval', default=5.0, show_default=True,
              help='Seconds between status polls.')
@click.option('--timeout', type=float, default=None,
              help='Give up after this many seconds (default: wait indefinitely).')
@target_options
def image_wait(build_ids, interval, timeout, namespace, context):
    """Block until every BUILD_ID is built: exit 0 (built), 1 (failed), 2 (stopped waiting:
    --timeout, or the service stopped answering).

    A build whose *pod* cannot start -- its own image unpullable, nowhere to schedule it --
    is a failure (exit 1) reported within a minute, not something this waits out. It used to
    hang here indefinitely, because Kubernetes leaves such a Job ``active`` forever.

    Exists so a *caller* can wait without holding a request open, and is why the MCP
    offers no image-build-wait tool — it did, and the cap on how long a tool call may
    block turned a long ROS build into repeated blocking calls. An agent harness
    backgrounds this instead and is notified when it exits; ``build_experiment_image``
    hands back the command with the ids already filled in.

    Takes **several** ids because a project builds one image per container that adds
    packages, and waiting for the first says nothing about the rest.
    """
    with service_client(namespace, context) as (client, target):
        _echo_target(target)
        _wait_for_builds(client, list(build_ids), interval=interval, timeout=timeout)


@image.command('status')
@click.argument('build_id')
@target_options
def image_status(build_id, namespace, context):
    """Show an image build's status."""
    with service_client(namespace, context) as (client, target):
        _echo_target(target)
        s = client.get_image_build_status(build_id)
        click.echo(f"{s.build_id}: phase={s.phase} done={s.done} cached={s.cached} "
                   f"image={s.image_ref}")
        if s.error:
            click.echo(f"  error [{s.error.phase}] {s.error.message} "
                       f"(entry={s.error.entry!r}, fixable_by={s.error.fixable_by})")


@image.command('log')
@click.argument('build_id')
@target_options
def image_log(build_id, namespace, context):
    """Print an image build's raw builder log."""
    with service_client(namespace, context) as (client, target):
        _echo_target(target)
        tail_chunks(lambda o: client.get_image_build_log(build_id, o),
                    lambda text: click.echo(text, nl=False))


#: Entry-point group for work that must happen once, before any command runs, and that
#: belongs to a distribution other than this one. Each entry point is a zero-argument
#: callable. There is exactly one today -- the core's ``.env`` read -- and the group
#: exists so the root group does not have to import the core to get it.
STARTUP_HOOK_GROUP = "robovast.cli_startup"


def run_startup_hooks():
    """Run every registered startup hook, in entry-point order.

    A hook that raises ``ValueError`` is a configuration error the user must fix -- an
    unusable ``.env`` -- and is reported as such. Anything else propagates: a hook is
    installed capability, not an optional extra, so a broken one is a broken install and
    must not be swallowed into a CLI that then behaves subtly differently.

    **A missing hook is checked for, not assumed absent.** Entry points are baked into a
    distribution's installed metadata, so adding one to `pyproject.toml` does nothing
    until the package is reinstalled -- and an editable checkout looks completely normal
    in the meantime. That silence cost real damage once: with the core installed but its
    hook unregistered, no `.env` was read, and `vast service upgrade` concluded the
    registry and git credentials "configuration is gone" and deleted both Secrets. So if
    the core is installed and contributed nothing, say so instead of running on.
    """
    hooks = list(entry_points(group=STARTUP_HOOK_GROUP))
    if not hooks and _core_installed():
        raise click.ClickException(
            "robovast is installed but registered no startup hooks, so ./.env was not "
            "read -- every image pin and credential in it is invisible to this command. "
            "The entry points are stale: re-run 'pip install -e .' (or 'make venv') in "
            "the robovast checkout.")
    for ep in hooks:
        try:
            ep.load()()
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc


def _core_installed() -> bool:
    """Whether the ``robovast`` distribution is present, as opposed to the client alone."""
    from importlib.metadata import PackageNotFoundError  # pylint: disable=import-outside-toplevel
    from importlib.metadata import distribution
    try:
        distribution("robovast")
        return True
    except PackageNotFoundError:
        return False


def load_plugins():
    """Dynamically load all VAST CLI plugins from entry points."""
    # Map of long plugin names to their short aliases. `execution`/`exec` is gone with the
    # group: every group is now named after what it acts on, and none of those names is
    # long enough to want a short form.
    aliases = {
        'configuration': 'config',
    }
    try:
        eps = entry_points(group='robovast.cli_plugins')

        for ep in eps:
            try:
                # Load the entry point (should return a Click group or command)
                plugin_group = ep.load()
                # Add it as a subcommand to the main CLI
                cli.add_command(plugin_group, name=ep.name)

                # Add short alias if defined
                if ep.name in aliases:
                    cli.add_command(plugin_group, name=aliases[ep.name])
            except Exception as e:
                click.echo(f"Warning: Failed to load plugin '{ep.name}': {e}", err=True)
    except Exception as e:
        click.echo(f"Warning: Failed to load plugins: {e}", err=True)


def main():
    """Main entry point for the VAST CLI."""
    # Load all plugins before running the CLI
    load_plugins()

    # Run the CLI (logging is configured via the --log-level callback)
    cli()  # pylint: disable=no-value-for-parameter


if __name__ == '__main__':
    main()
