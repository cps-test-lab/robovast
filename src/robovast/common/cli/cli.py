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

"""Main CLI entry point for RoboVAST."""

import logging
import os
import shutil
import sys
import tarfile
from importlib.metadata import entry_points

import click

from ..common import load_config
from ..execution import is_campaign_dir
from ..logging_config import (get_logger, setup_logging,
                              setup_logging_from_project_config)
from .checks import check_docker_access
from .project_config import ProjectConfig, get_project_config
from .service_target import (_service_alive, _start_port_forward,
                             _stop_port_forward)
from .service_target import echo_target as _echo_target
from .service_target import service_client, target_options

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
        # No log level specified, use project config
        setup_logging_from_project_config()
    return value


@click.group()
@click.option('--log-level', '-l',
              type=click.Choice(['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'], case_sensitive=False),
              help='Set logging level (overrides project configuration)',
              callback=configure_logging,
              is_eager=True,
              expose_value=False)
@click.option('--vast-file', '-V', type=click.Path(exists=True), default=None,
              help='Override the .vast configuration file (instead of project default)')
@click.version_option(package_name="robovast", prog_name="RoboVAST")
@click.pass_context
def cli(ctx, vast_file):
    """VAST - RoboVAST Command-Line Interface.

    Main command for managing variations, executing scenarios,
    and analyzing results in the RoboVAST framework.

    The global ``--log-level`` option can be used to control logging verbosity
    for any command, overriding the project configuration.

    Use ``--vast-file`` / ``-V`` to temporarily use a different ``.vast``
    configuration file instead of the one stored in the project.

    Examples:
      vast --log-level DEBUG execution cluster cleanup
      vast --log-level INFO init config.yaml
      vast -V other.vast config list
      vast -V other.vast exec cluster run

    See ``vast --help`` for a list of available commands.
    """
    # Ensure context object exists
    ctx.ensure_object(dict)
    if vast_file:
        ctx.obj['vast_file'] = os.path.abspath(vast_file)


@cli.command()
@click.argument('config', type=click.Path(exists=True))
@click.option('--results-dir', '-r', default="results", type=click.Path(),
              help='Directory for storing results')
@click.option('--project-log-level', default="INFO",
              type=click.Choice(['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'], case_sensitive=False),
              help='Default logging level for the project (saved to project config)')
@click.option('--force', '-f', is_flag=True,
              help='Skip Docker and Kubernetes accessibility checks')
def init(config, results_dir, project_log_level, force):
    """Initialize a VAST project.

    Creates a `.vast_project` file in the current directory that stores
    the configuration file path, results directory, and default logging level.
    These settings will be used by other VAST commands automatically.

    The default log level can be overridden for any command using the global
    ``--log-level`` flag (e.g., ``vast --log-level DEBUG <command>``).

    By default, performs the following checks before initialization:

    * Docker daemon accessibility and version
    * Kubernetes cluster connectivity and version
    * robovast pod is running in the default namespace

    Use the ``--force`` flag to skip all these checks if needed.
    """
    # Check Docker and Kubernetes access unless --force is used
    # Check Docker access
    if force:
        click.echo("⚠ Warning: Skipping checks (--force enabled)")

    # check integrity of config file
    try:
        load_config(config)
    except Exception as e:
        click.echo(f"✗ Error: Failed to load configuration file: {e}", err=True)
        if not force:
            sys.exit(1)

    logger.debug("Checking Docker daemon access...")
    docker_ok, docker_msg = check_docker_access()
    if not docker_ok and not force:
        click.echo(f"✗ Error: {docker_msg}", err=True)
        click.echo("  Docker is required for RoboVAST execution.", err=True)
        sys.exit(1)

    # Convert to absolute paths
    project_file_dir = os.path.abspath(os.getcwd())
    if not os.path.isabs(config):
        config_path = os.path.abspath(os.path.join(project_file_dir, config))
    else:
        config_path = config
    if not os.path.isabs(results_dir):
        results_path = os.path.abspath(os.path.join(project_file_dir, results_dir))
    else:
        results_path = results_dir

    # Validate config file exists
    if not os.path.isfile(config_path):
        click.echo(f"✗ Error: Configuration file not found: {config_path}", err=True)
        sys.exit(1)

    # Create ProjectConfig and save it
    project_config = ProjectConfig(config_path=config_path, results_dir=results_path, log_level=project_log_level)

    # Validate the configuration
    is_valid, error = project_config.validate()
    if not is_valid:
        click.echo(f"✗ Error: {error}", err=True)
        sys.exit(1)

    # Check if .vast_project already exists
    existing_file = ProjectConfig.find_project_file()
    if existing_file:
        click.echo(f"⚠ Warning: Overwriting existing project file: {existing_file}")

    # Save the project file
    project_file = project_config.save()

    click.echo(f"✓ Project initialized successfully!")
    logging.debug(f"Configuration: {config_path}")
    logging.debug(f"Results directory: {results_path}")
    logging.debug(f"Log level: {project_log_level}")
    logging.debug(f"Project file: {project_file}")


def _ensure_ui_built(rebuild: bool = False) -> None:
    """(Re)build the web UI's ``ui/dist`` for a source checkout, when needed.

    No-op unless run from a source tree: a packaged install / in-cluster pod has
    no ``ui/`` sources (its dist is baked in and pointed at by ``ROBOVAST_UI_DIST``),
    so there is nothing — and no ``npm`` — to build. In a checkout, build only when
    ``ui/dist`` is missing or older than the UI sources (or when *rebuild*), so a
    normal ``vast serve`` costs just an mtime scan.
    """
    import subprocess
    from pathlib import Path
    if os.environ.get('ROBOVAST_UI_DIST'):
        return  # packaged/baked dist — nothing to build here
    ui_dir = Path(__file__).resolve().parents[4] / 'ui'
    if not (ui_dir / 'package.json').is_file():
        return  # not a source checkout
    dist_index = ui_dir / 'dist' / 'index.html'
    src_dir = ui_dir / 'src'
    fresh = dist_index.is_file() and src_dir.is_dir() and not any(
        p.stat().st_mtime > dist_index.stat().st_mtime
        for p in src_dir.rglob('*') if p.is_file())
    if fresh and not rebuild:
        return
    npm = shutil.which('npm')
    if npm is None:
        raise click.ClickException(
            "the web UI needs building but 'npm' was not found; install Node.js "
            "(or prebuild once with 'cd ui && npm run build')")
    if not (ui_dir / 'node_modules').is_dir():
        click.echo('Installing web UI dependencies (npm install)…')
        subprocess.run([npm, 'install'], cwd=str(ui_dir), check=True)  # noqa: S603
    click.echo('Building web UI (npm run build)…')
    subprocess.run([npm, 'run', 'build'], cwd=str(ui_dir), check=True)  # noqa: S603


def _load_project_dotenv() -> None:
    """Load the project ``.env`` into ``os.environ`` (share creds, ntfy, etc.).

    Mirrors the results CLI's ``_load_share_dotenv``: prefer the ``.env`` next to
    the project config, then the discovered project dir, then the CWD. ``override``
    is left at its default (False) so a real environment variable always wins over
    a ``.env`` line, and the call is a harmless no-op when no ``.env`` exists.
    """
    from dotenv import load_dotenv
    project_file = ProjectConfig.find_project_file()
    if project_file:
        project_dir = os.path.dirname(os.path.abspath(project_file))
        pc = ProjectConfig.load()
        if pc and pc.config_path:
            load_dotenv(os.path.join(os.path.dirname(pc.config_path), ".env"))
        load_dotenv(os.path.join(project_dir, ".env"))
    else:
        load_dotenv()


def _build_cluster_impl(in_pod, context, k8s_namespace, store=None):
    """Build the cluster-lane service for ``vast serve``.

    In-pod the config/cluster come from the pod env; off-cluster the config is read
    from the deployed robovast-service (the authoritative record), so no local setup
    is needed. Shared by ``--backend cluster`` and the cluster lane of
    ``--backend local+cluster`` (``store`` lets the latter pass its shared store).
    """
    from robovast.service.cluster_service import ClusterService
    if in_pod:
        return ClusterService(kube_context=context, store=store)
    from robovast.execution.cluster_execution.service_deploy import \
        read_service_config_from_cluster
    name, kwargs = read_service_config_from_cluster(k8s_namespace, context)
    if not name:
        for_ctx = f" in context {context!r}" if context else ""
        raise click.ClickException(
            f"no robovast-service found{for_ctx} (namespace "
            f"{k8s_namespace!r}) to read the cluster config from — deploy "
            "one with 'vast exec cluster setup <cluster-config>"
            f"{f' -x {context}' if context else ''}', or check "
            "--context/--namespace.")
    # Off-cluster the driver reaches the cluster's object store through a kubectl
    # port-forward, which is fragile under the large per-file result transfers a big
    # campaign produces. This mode is a dev convenience; the deployed in-cluster
    # service reads the store directly (no tunnel).
    click.secho(
        "WARNING: running the cluster backend off-cluster — campaigns are "
        "driven from this host through a kubectl port-forward to the cluster "
        "object store, which is fragile under large result transfers. This "
        "mode is a dev convenience; run large campaigns via the deployed "
        "in-cluster robovast-service.",
        fg="yellow")
    return ClusterService(namespace=k8s_namespace, cluster_config_name=name,
                          cluster_config_kwargs=kwargs, kube_context=context,
                          store=store)


@cli.command()
@click.option('--host', default='127.0.0.1', show_default=True,
              help='Interface to bind. Keep 127.0.0.1 unless behind a tunnel/proxy: '
                   'the service is unauthenticated in v1.')
@click.option('--port', default=8800, show_default=True, type=int,
              help='Port to listen on.')
@click.option('--backend', type=click.Choice(['auto', 'local', 'cluster',
                                              'local+cluster']),
              default='auto', show_default=True,
              help="Execution backend. 'auto' picks 'cluster' when running inside "
                   "a Kubernetes pod, else 'local' Docker. 'local+cluster' offers "
                   "BOTH lanes in one serve and chooses per campaign (dev-host only "
                   "— needs Docker AND kubeconfig; the default lane is cluster).")
@click.option('--attach', is_flag=True,
              help='Do not run a service — hold a kubectl port-forward to the '
                   'already-deployed in-cluster robovast-service and keep it '
                   'reachable on the local port until Ctrl-C. The tunnel every '
                   'other client (CLI, MCP, "vast ui") then auto-detects on the '
                   'conventional port. This is how you "serve" a cluster you '
                   'deployed with "vast exec cluster setup".')
@click.option('--context', '-x', default=None, metavar='NAME',
              help='With --backend cluster (run off-cluster) or --attach: which '
                   'Kubernetes context (default: the active one). For --backend '
                   'cluster the cluster config is read from the deployed '
                   'robovast-service in that cluster — works from any host with '
                   'kubeconfig access.')
@click.option('--namespace', '-n', 'k8s_namespace', default='default',
              show_default=True,
              help='With --backend cluster or --attach: namespace the '
                   'robovast-service is deployed in.')
@click.option('--rebuild-ui', is_flag=True,
              help='Force a web UI rebuild even if ui/dist looks up to date '
                   '(source checkout only).')
@click.option('--workspace-dir', 'workspace_dirs', multiple=True,
              type=click.Path(exists=True, file_okay=False),
              help='Pin a directory as a read-only workspace, used in place '
                   '(repeatable). Skips the "vast workspace init" upload — the '
                   'workspace is present the moment the service starts and '
                   'survives restarts (edit the files on disk to change it). '
                   'Local backend only.')
def serve(host, port, backend, attach, context, k8s_namespace, rebuild_ui,
          workspace_dirs):
    """Make a robovast-service reachable on the local port until Ctrl-C.

    This is the one command that puts a service on ``127.0.0.1:8800``; while it
    runs, the ``vast`` CLI, the MCP server, and ``vast ui`` all work against it,
    and campaigns survive client exit (unlike ``vast exec local run``). The
    service serves the web UI at the same URL — from a source checkout this
    (re)builds ``ui/dist`` first when it is missing or stale (needs ``npm``;
    ``--rebuild-ui`` forces it). Ways it makes the port live:

    * **local** (default off-cluster) — runs the app in-process: local Docker +
      local filesystem (mode 2). Run it on your machine or a remote VM reached
      over an SSH tunnel.
    * **cluster** (default in-pod) — runs the app in-process, driving each
      campaign against Kubernetes Jobs (mode 3); this is what the in-cluster
      ``robovast-service`` Deployment runs. Run it **off-cluster** with
      ``--backend cluster -x <context>`` to debug the driver locally while
      scenarios execute in that cluster — the cluster config is read from the
      deployed robovast-service in that cluster, so it works from any host with
      kubeconfig access (no local setup needed).
    * **local+cluster** — offers *both* lanes in one service and chooses per
      campaign (``start_campaign``/``CreateCampaignRequest`` ``backend``; the
      default lane is cluster). A **dev-host mode**: it needs both Docker and
      kubeconfig, so it is off-cluster only (refused in a pod — the deployed
      service is cluster-only). Lets an agent pilot a campaign locally and scale
      the same session to the cluster without re-pointing serve.
    * **--attach** — runs *nothing* locally: just holds a ``kubectl
      port-forward`` to the service you already deployed with ``vast exec cluster
      setup`` and keeps it reachable here until Ctrl-C. This is how you drive a
      deployed cluster from your laptop (cluster lane only).

    Security: unauthenticated in v1, so it binds ``127.0.0.1`` by default and
    must stay behind localhost / SSH tunnel / port-forward (see
    ``docs/deployment.rst``). Web UI + OpenAPI docs at ``/`` and ``/docs``.
    """
    if attach:
        conflicts = [name for name, bad in (
            ('--backend', backend != 'auto'),
            ('--host', host != '127.0.0.1'),
            ('--workspace-dir', bool(workspace_dirs)),
            ('--rebuild-ui', rebuild_ui),
        ) if bad]
        if conflicts:
            verb = 'does not' if len(conflicts) == 1 else 'do not'
            raise click.ClickException(
                f"--attach only tunnels to the already-deployed in-cluster "
                f"service; it runs no service of its own, so "
                f"{', '.join(conflicts)} {verb} apply.")
        _serve_attach(port, k8s_namespace, context)
        return

    from robovast.service.app import serve as _serve

    # The campaign driver runs in this same process (local backend, or an
    # off-cluster '--backend cluster' driver), so its share upload reads *this*
    # os.environ. Load the project .env now — the same convention the results CLI
    # uses — so ROBOVAST_SHARE_TYPE / provider credentials kept there make
    # '--upload-to-share' work without exporting them by hand. override=False
    # keeps real environment variables authoritative; in-pod there is no project
    # .env, so this is a no-op and the deployment env wins.
    _load_project_dotenv()

    # Build the SPA the service serves, so a source checkout needs one command
    # (no-op for a packaged/in-cluster install — see _ensure_ui_built).
    _ensure_ui_built(rebuild=rebuild_ui)

    in_pod = bool(os.environ.get('KUBERNETES_SERVICE_HOST'))
    if backend == 'auto':
        backend = 'cluster' if in_pod else 'local'

    if context is not None and backend not in ('cluster', 'local+cluster'):
        raise click.ClickException(
            "--context/-x only applies to '--backend cluster' / 'local+cluster' — "
            "it selects which Kubernetes context to dispatch campaigns into.")

    if backend == 'local+cluster' and in_pod:
        raise click.ClickException(
            "--backend local+cluster is a dev-host mode (both lanes in one serve) "
            "and needs local Docker, which a Kubernetes pod does not have; the "
            "in-cluster service is cluster-only.")

    if backend == 'cluster':
        if workspace_dirs:
            raise click.ClickException(
                "--workspace-dir is only supported by the local backend "
                "(the cluster service stores workspaces in the object store)")
        impl = _build_cluster_impl(in_pod, context, k8s_namespace)
        storage = "object store"
    elif backend == 'local+cluster':
        # Dev-host dual lane: one shared store so both lanes see the same results dir
        # and workspaces; the cluster lane is built exactly as '--backend cluster'.
        from robovast.service.multi_backend import MultiBackendService
        from robovast.service.workspaces import WorkspaceStore
        store = WorkspaceStore(workspace_dirs=list(workspace_dirs) or None)
        cluster = _build_cluster_impl(in_pod, context, k8s_namespace, store=store)
        impl = MultiBackendService(cluster, store=store)
        storage = "local filesystem + object store"
    else:
        from robovast.service.client import LocalTransport
        impl = LocalTransport(workspace_dirs=list(workspace_dirs))
        storage = "local filesystem"

    click.echo(f"Starting robovast-service on http://{host}:{port} (OpenAPI at /docs)")
    click.echo(f"Backend: {backend} | storage: {storage} | Ctrl-C to stop")
    for wid_dir in workspace_dirs:
        click.echo(f"Pinned read-only workspace: {wid_dir}")
    _serve(impl, host=host, port=port)


def _serve_attach(port, namespace, context):
    """Hold a port-forward to the deployed in-cluster service until Ctrl-C.

    Runs no service locally — the deployed ``robovast-service`` Deployment *is*
    the service; this just keeps it reachable on the conventional local port so
    the CLI, the MCP server and ``vast ui`` auto-detect it there. This is the
    ``--attach`` mode of ``vast serve``.
    """
    import time  # pylint: disable=import-outside-toplevel

    from robovast.execution.cluster_execution.service_deploy import SERVICE_PORT

    # Bind the *conventional* port (not a random free one): this tunnel is held
    # open for a human, and flagless `vast workspace …`/`vast ui` auto-detect it
    # by probing this same port. (The ephemeral per-call `--cluster` on other
    # commands can use a random port; nothing else needs to find those.)
    local_port = port or SERVICE_PORT
    proc, url = _start_port_forward(namespace, context, local_port)
    click.echo(f"\n✓ robovast-service (in-cluster): {url}   (web UI + REST API + /docs)")
    if local_port == SERVICE_PORT:
        click.echo("  Other clients follow this tunnel automatically — no "
                   "--cluster/--attach needed. Open it with 'vast ui'.")
    else:
        click.echo(f"  (non-default port {local_port}: other clients look for the "
                   f"conventional port {SERVICE_PORT}, so keep this at the default "
                   "for them to auto-detect it)")
    click.echo("  Ctrl-C to close the tunnel")

    # kubectl port-forward drops its tunnel on any transient network hiccup or
    # pod-side reset ("connection reset by peer" → "lost connection to pod").
    # This is held open for a human, so a drop should *reconnect*, not end the
    # command. Loop: stream kubectl output until the tunnel dies, then rebuild
    # it (with backoff while the cluster stays unreachable). Only Ctrl-C exits.
    backoff = 1.0
    try:
        while True:
            for line in iter(proc.stdout.readline, ''):  # stream until it dies
                click.echo(line.rstrip())
            proc.wait()
            # Tunnel dropped (a Ctrl-C would have raised instead). Rebuild it.
            click.echo("  ⚠ tunnel dropped — reconnecting…")
            _stop_port_forward(proc)
            while True:
                try:
                    proc, _ = _start_port_forward(
                        namespace, context, local_port, echo=False)
                    break
                except click.ClickException as exc:
                    click.echo(f"  reconnect failed ({exc.message}); retrying "
                               f"in {backoff:.0f}s…")
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 15.0)
            backoff = 1.0
            click.echo(f"  ✓ tunnel re-established: {url}")
    except KeyboardInterrupt:
        click.echo("\nClosing tunnel...")
    finally:
        _stop_port_forward(proc)


@cli.command()
@click.option('--port', default=0, type=int, metavar='PORT',
              help='Service port to open. 0 (default) uses the conventional port.')
@click.option('--no-browser', is_flag=True,
              help='Do not launch a browser.')
@click.option('--cluster', is_flag=True, hidden=True,
              help='Removed — hold the tunnel with "vast serve --attach" instead.')
def ui(port, no_browser, cluster):
    """Open the RoboVAST web UI in your browser.

    A thin shortcut: it opens a browser at the ``robovast-service`` on the
    conventional local port and does nothing else. Something must already be
    serving there — start it (in another terminal) with one of:

    \b
      vast serve            local service on this machine
      vast serve --attach   tunnel to the in-cluster service you deployed
      vast ui               ...then open a browser at whatever is serving on :8800

    Any SSH / ``kubectl port-forward`` tunnel to ``127.0.0.1:8800`` works too —
    ``vast ui`` just auto-detects the service already answering there. Because
    the service serves the web UI and the REST API on the same port, what this
    opens is all a browser needs — the same place the CLI and MCP server detect.
    """
    import webbrowser  # pylint: disable=import-outside-toplevel

    from robovast.execution.cluster_execution.service_deploy import SERVICE_PORT

    if cluster:
        raise click.ClickException(
            "'vast ui' no longer opens tunnels. Hold the tunnel to the "
            "in-cluster service with 'vast serve --attach' (in another terminal), "
            "then run 'vast ui'.")

    local_port = port or SERVICE_PORT
    url = f'http://127.0.0.1:{local_port}'
    if not _service_alive(url):
        raise click.ClickException(
            f"no robovast-service answering at {url}. Start one first (in another "
            "terminal):\n"
            "  vast serve            local service\n"
            "  vast serve --attach   tunnel to the in-cluster service\n"
            "or bring up your own tunnel to 127.0.0.1:8800.")
    click.echo(f"✓ robovast-service: {url}   (web UI + REST API + /docs)")
    if not no_browser:
        webbrowser.open(url)


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
    command here takes ``--cluster`` and prints the target it resolved. Without
    it they act on this machine (the store a local ``vast serve`` uses); with it,
    on the in-cluster service — the store *its* web UI reads.
    """


#: Directory names skipped by ``workspace init`` — campaign outputs, not project inputs.
_INIT_EXCLUDE_DIRS = {'results'}


@workspace.command('init')
@click.argument('directory', type=click.Path(exists=True, file_okay=False))
@click.option('--name', default='', help='Workspace name (default: the directory name).')
@click.option('--exclude', 'excludes', multiple=True, metavar='NAME',
              help='Directory name to skip (repeatable). Adds to the default '
                   f"{sorted(_INIT_EXCLUDE_DIRS)}.")
@target_options
def workspace_init(directory, name, excludes, cluster, namespace, context):
    """Create a workspace and upload every file from DIRECTORY into it.

    ``.vast``/``.osc`` are written inline; all other files go through the upload
    side channel (executability preserved). Hidden files/dirs (``.cache`` etc.) and
    output dirs (``results/``, plus any ``--exclude``) are skipped. Prints the new
    workspace id — open it in the web UI's Config tab.

    \b
      vast workspace init configs/examples/growth_sim            # this machine
      vast workspace init configs/examples/growth_sim --cluster  # the cluster UI
    """
    from pathlib import Path
    from robovast.service.interface import CreateWorkspaceRequest
    from robovast.service.project_push import sync_directory_to_workspace
    root = Path(directory).resolve()
    skip_dirs = _INIT_EXCLUDE_DIRS | set(excludes)
    with service_client(cluster, namespace, context) as (client, target):
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
            client, wid, root, skip_dirs=skip_dirs, echo=click.echo)
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
@target_options
def workspace_update(workspace, directory, excludes, prune, cluster, namespace, context):
    """Re-sync DIRECTORY into an EXISTING workspace (id or name).

    Uploads every file from DIRECTORY, overwriting in place — ``.vast``/``.osc``
    inline, everything else via the upload side channel. Hidden files/dirs and
    output dirs (``results/``, plus any ``--exclude``) are skipped. With
    ``--prune`` it also removes workspace files that no longer exist locally, so
    the workspace mirrors DIRECTORY exactly.

    \b
      vast workspace update ws-ab12 configs/examples/growth_sim
      vast workspace update growth_sim configs/examples/growth_sim --prune
    """
    from pathlib import Path
    from robovast.service.project_push import (_resolve_workspace_id,
                                               sync_directory_to_workspace)
    root = Path(directory).resolve()
    skip_dirs = _INIT_EXCLUDE_DIRS | set(excludes)
    with service_client(cluster, namespace, context) as (client, target):
        _echo_target(target)
        wid = _resolve_workspace_id(client, workspace)
        stats = sync_directory_to_workspace(
            client, wid, root, skip_dirs=skip_dirs, prune=prune, echo=click.echo)
        count = stats['written'] + stats['uploaded']
        summary = f"{count} written"
        if prune:
            summary += f", {stats['pruned']} pruned"
        click.echo(f"workspace {wid} updated from {root} ({summary})")


@workspace.command('list')
@target_options
def workspace_list(cluster, namespace, context):
    """List workspaces (newest first)."""
    with service_client(cluster, namespace, context) as (client, target):
        _echo_target(target)
        workspaces = client.list_workspaces().workspaces
        if not workspaces:
            click.echo("(none)")
        for w in workspaces:
            click.echo(f"{w.workspace_id}  {w.name or '-':20}  {w.created_at or ''}")


@workspace.command('delete')
@click.argument('workspace', metavar='WORKSPACE')
@target_options
def workspace_delete(workspace, cluster, namespace, context):
    """Delete a workspace and its inputs (existing campaigns are unaffected).

    WORKSPACE may be a ``ws-…`` id or a workspace name (unique names resolve;
    ambiguous ones must be deleted by id).
    """
    with service_client(cluster, namespace, context) as (client, target):
        _echo_target(target)
        res = client.delete_workspace(workspace)
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
        click.echo(f"✗ Failed to install completion: {e}", err=True)
        raise click.Exit(1)


@cli.command()
@click.argument('archive', type=click.Path(exists=True))
@click.option('--output', '-o', default=None,
              help='Directory where results will be extracted (uses project results dir if not specified)')
@click.option('--force', '-f', is_flag=True,
              help='Force extraction even if campaign directory already exists')
def import_results(archive, output, force):
    """Import results from a downloaded archive.

    Extracts a tar.gz archive (created by ``vast execution cluster download``)
    to the results directory. This is useful for importing results that were
    downloaded on a different machine or for re-importing previously downloaded results.

    The archive should be in the format ``<campaign-name>-<timestamp>.tar.gz`` and contain
    a campaign directory with all run results.

    Requires project initialization with ``vast init`` first (unless ``--output`` is specified).
    """
    # Get output directory
    project_config = None
    if output is None:
        # Get from project configuration
        try:
            project_config = get_project_config()
            output = project_config.results_dir
        except Exception as e:
            click.echo("Error: Could not load project configuration.", err=True)
            click.echo(f"Details: {e}", err=True)
            click.echo("Use --output to specify the extraction directory.", err=True)
            sys.exit(1)

    # Validate output parameter
    if not output:
        click.echo("Error: --output parameter is required (or use 'vast init' to set default)", err=True)
        click.echo("Use --help for usage information", err=True)
        sys.exit(1)

    # Create output directory
    os.makedirs(output, exist_ok=True)

    try:
        archive_path = os.path.abspath(archive)
        click.echo(f"Importing results from: {archive_path} to results directory '{output}'...")

        # Validate the archive
        click.echo(f"Validating archive...")
        try:
            with tarfile.open(archive_path, 'r:gz') as tar:
                # Get the list of members to check structure
                members = tar.getnames()
                if not members:
                    click.echo("Error: Archive is empty", err=True)
                    sys.exit(1)

                # Extract run ID from archive contents
                top_level_dirs = set()
                for member in members:
                    parts = member.split('/')
                    if parts:
                        top_level_dirs.add(parts[0])

                if len(top_level_dirs) != 1:
                    click.echo(f"Warning: Archive contains multiple top-level directories: {top_level_dirs}")

                campaign = list(top_level_dirs)[0] if top_level_dirs else None
                if campaign and not is_campaign_dir(campaign):
                    click.echo(
                        f"Warning: Archive does not contain a recognized campaign directory "
                        f"(expected '<campaign-name>-YYYY-MM-DD-HHMMSS', found '{campaign}')")
            click.echo(f"Archive validation successful")
        except (tarfile.TarError, OSError) as e:
            click.echo(f"Error: Archive validation failed: {e}", err=True)
            sys.exit(1)

        # Check if campaign directory already exists
        if campaign:
            campaign_output_dir = os.path.join(output, campaign)
            if os.path.exists(campaign_output_dir):
                if not force:
                    click.echo(f"Error: Campaign directory already exists: {campaign_output_dir}", err=True)
                    click.echo(f"Use --force to overwrite existing run", err=True)
                    sys.exit(1)
                else:
                    click.echo(f"Removing existing campaign directory...")
                    shutil.rmtree(campaign_output_dir)

        # Extract the archive
        logger.debug(f"Extracting archive...")
        with tarfile.open(archive_path, 'r:gz') as tar:
            tar.extractall(path=output)

        logger.debug(f"Successfully extracted to: {output}")

        click.echo(f"✓ Import completed successfully!")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@cli.group()
def image():
    """Build the experiment image declared by a project's ``build:`` section.

    Mirrors the ``build_experiment_image`` MCP tools and drives the same interface.
    Registry-free: you name a project; the service builds from its ``build:`` section
    and the image is referenced as ``build:<tag>`` in ``execution.image``. Every
    command takes ``--cluster`` and prints the target it resolved (this machine's
    ``vast serve`` by default, or the in-cluster service with ``--cluster``).
    """


@image.command('build')
@click.option('--workspace-id', default='', help='Workspace to build (empty = CWD project).')
@click.option('--config-path', default='', help='Which .vast when the workspace has several.')
@click.option('--wait/--no-wait', default=True, help='Wait for the build to finish (default).')
@target_options
def image_build(workspace_id, config_path, wait, cluster, namespace, context):
    """Build (or reuse) the experiment image from the project's ``build:`` section."""
    import time as _time

    from robovast.service.interface import BuildImageRequest
    with service_client(cluster, namespace, context) as (client, target):
        _echo_target(target)
        ref = client.build_image(BuildImageRequest(
            workspace_id=workspace_id, config_path=config_path))
        if ref.cached:
            click.echo(f"✓ image 'build:{ref.tag}' already up to date (cache hit)")
            return
        click.echo(f"building 'build:{ref.tag}' (build_id={ref.build_id}) ...")
        if not wait:
            click.echo("started; poll 'vast image status'")
            return
        while True:
            status = client.get_image_build_status(ref.build_id)
            if status.done:
                break
            _time.sleep(2.0)
        if status.phase in ('succeeded', 'cached'):
            click.echo(f"✓ built 'build:{status.tag}'")
        else:
            err = status.error
            if err:
                click.echo(f"✗ build failed [{err.phase}] {err.message}", err=True)
                if err.entry:
                    click.echo(f"  offending entry: {err.entry} (fixable_by={err.fixable_by})",
                               err=True)
            else:
                click.echo("✗ build failed", err=True)
            sys.exit(1)


@image.command('status')
@click.argument('build_id')
@target_options
def image_status(build_id, cluster, namespace, context):
    """Show an image build's status."""
    with service_client(cluster, namespace, context) as (client, target):
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
def image_log(build_id, cluster, namespace, context):
    """Print an image build's raw builder log."""
    with service_client(cluster, namespace, context) as (client, target):
        _echo_target(target)
        offset = 0
        while True:
            chunk = client.get_image_build_log(build_id, offset)
            if chunk.text:
                click.echo(chunk.text, nl=False)
            offset = chunk.next_offset
            if chunk.eof:
                break
            import time as _time
            _time.sleep(1.0)


def load_plugins():
    """Dynamically load all VAST CLI plugins from entry points."""
    # Map of long plugin names to their short aliases
    aliases = {
        'configuration': 'config',
        'execution': 'exec',
        'evaluation': 'eval',
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
