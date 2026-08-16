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

import functools
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
from .service_target import _service_alive
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

    Every command reads ``./.env`` first, so anything RoboVAST takes from the
    environment (share credentials, registry, ntfy, ``ROBOVAST_*_IMAGE``, …) can
    be kept there instead of exported by hand.

    Examples:
      vast --log-level DEBUG execution cluster cleanup
      vast --log-level INFO init config.yaml
      vast -V other.vast config list
      vast -V other.vast exec cluster run

    See ``vast --help`` for a list of available commands.
    """
    # One .env read for the whole CLI, before any command runs: every variable in it
    # is simply part of the environment from here on, whichever command consumes it.
    # Per-command loaders drifted — a command that forgot to call one (or resolved a
    # value *before* calling it) silently ignored a configured .env line.
    from robovast.common.env_file import load_env_file
    try:
        load_env_file()
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

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
    """(Re)build the web UI's ``frontend/ui/dist`` for a source checkout, when needed.

    No-op unless run from a source tree: a packaged install / in-cluster pod has
    no ``frontend/ui/`` sources (its dist is baked in and pointed at by
    ``ROBOVAST_UI_DIST``), so there is nothing — and no ``npm`` — to build. In a
    checkout, build only when ``frontend/ui/dist`` is missing or older than the UI
    sources (or when *rebuild*), so a normal ``vast serve`` costs just an mtime scan.
    """
    import subprocess
    from pathlib import Path
    if os.environ.get('ROBOVAST_UI_DIST'):
        return  # packaged/baked dist — nothing to build here
    ui_dir = Path(__file__).resolve().parents[4] / 'frontend' / 'ui'
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
            "(or prebuild once with 'cd frontend/ui && npm run build')")
    if not (ui_dir / 'node_modules').is_dir():
        click.echo('Installing web UI dependencies (npm install)…')
        subprocess.run([npm, 'install'], cwd=str(ui_dir), check=True)  # noqa: S603
    click.echo('Building web UI (npm run build)…')
    subprocess.run([npm, 'run', 'build'], cwd=str(ui_dir), check=True)  # noqa: S603


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


def _one_workspace_dir(ctx, param, value):  # noqa: ARG001 - click callback signature
    """Collapse ``--workspace-dir`` to a single directory, refusing more than one.

    Declared ``multiple=True`` only so a second occurrence can be *reported*: click's
    single-value default would silently keep the last one, and a dropped pin is
    exactly the kind of quiet substitution that makes a service serve something the
    operator did not ask for.
    """
    if len(value) > 1:
        raise click.BadParameter(
            "takes one directory. A pinned directory holds as many .vast files as "
            "you like (selected per campaign with --config-path), so pin the "
            "collection — e.g. a repo root — rather than passing several.")
    return value[0] if value else None


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
@click.option('--context', '-x', default=None, metavar='NAME',
              help='With --backend cluster (run off-cluster): which '
                   'Kubernetes context (default: the active one). For --backend '
                   'cluster the cluster config is read from the deployed '
                   'robovast-service in that cluster — works from any host with '
                   'kubeconfig access.')
@click.option('--namespace', '-n', 'k8s_namespace', default='default',
              show_default=True,
              help='With --backend cluster: namespace the '
                   'robovast-service is deployed in.')
@click.option('--rebuild-ui', is_flag=True,
              help='Force a web UI rebuild even if frontend/ui/dist looks up to date '
                   '(source checkout only).')
@click.option('--mcp/--no-mcp', 'mount_mcp', default=True, show_default=True,
              help='Also expose the MCP server at /mcp on this same port, so one '
                   'tunnel covers the web UI, the REST API, and MCP tools together. '
                   'Pass --no-mcp to run MCP separately (e.g. via "vast mcp serve").')
@click.option('--workspace-dir', 'workspace_dir', multiple=True,
              callback=_one_workspace_dir,
              type=click.Path(exists=True, file_okay=False),
              help='Pin a directory as a read-only workspace, used in place. Skips '
                   'the "vast workspace init" upload — the workspace is present the '
                   'moment the service starts and survives restarts (edit the files '
                   'on disk to change it). One directory: it holds as many .vast '
                   'files as you like, selected per campaign with --config-path, so '
                   'pin the collection (e.g. a repo root) rather than each project. '
                   'Requires the service to run on this host, so it is refused '
                   'in-pod.')
def serve(host, port, backend, context, k8s_namespace, rebuild_ui,
          workspace_dir, mount_mcp):
    """Make a robovast-service reachable on the local port until Ctrl-C.

    This is the one command that puts a service on ``127.0.0.1:8800``; while it
    runs, the ``vast`` CLI, the MCP server, and ``vast ui`` all work against it,
    and campaigns survive client exit (unlike ``vast exec local run``). The
    service serves the web UI at the same URL — from a source checkout this
    (re)builds ``frontend/ui/dist`` first when it is missing or stale (needs ``npm``;
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

    Security: every request needs the shared token (``ROBOVAST_AUTH_TOKEN``). When
    none is configured one is generated at startup and printed as a login URL you
    can click — there is no unauthenticated mode. It still binds ``127.0.0.1`` by
    default; publishing it is ``vast exec cluster setup --ingress-host``, which
    also insists on TLS. Web UI + OpenAPI docs at ``/`` and ``/docs``; MCP tools at
    ``/mcp`` (see ``--no-mcp``) — one URL reaches all three.
    """
    from robovast.service.app import serve as _serve

    # The campaign driver runs in this same process (local backend, or an off-cluster
    # '--backend cluster' driver), so everything it reads from os.environ comes from
    # the ./.env the group callback loaded: share credentials for '--upload-to-share',
    # the registry, ROBOVAST_IMAGE / ROBOVAST_CONTROLLER_IMAGE. In-pod there is no
    # project .env, so the deployment env is the whole environment.

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

    # Pinning uses the directory in place, so it needs the service to run on the host
    # that holds it. That rules out a pod (no such directory) but NOT an off-cluster
    # '--backend cluster' driver, which runs here and reads project inputs from this
    # filesystem exactly as the local lane does.
    if workspace_dir and in_pod:
        raise click.ClickException(
            "--workspace-dir pins a directory on the serve host, and a Kubernetes "
            "pod has no such directory. Upload the project instead with "
            "'vast workspace init <dir>'.")

    if backend == 'cluster':
        from robovast.service.workspaces import WorkspaceStore
        store = WorkspaceStore(workspace_dir=workspace_dir)
        impl = _build_cluster_impl(in_pod, context, k8s_namespace, store=store)
        storage = "object store"
    elif backend == 'local+cluster':
        # Dev-host dual lane: one shared store so both lanes see the same results dir
        # and workspaces; the cluster lane is built exactly as '--backend cluster'.
        from robovast.service.multi_backend import MultiBackendService
        from robovast.service.workspaces import WorkspaceStore
        store = WorkspaceStore(workspace_dir=workspace_dir)
        cluster = _build_cluster_impl(in_pod, context, k8s_namespace, store=store)
        impl = MultiBackendService(cluster, store=store)
        storage = "local filesystem + object store"
    else:
        from robovast.service.client import LocalTransport
        impl = LocalTransport(workspace_dir=workspace_dir)
        storage = "local filesystem"

    mcp_note = ", MCP at /mcp" if mount_mcp else ""
    click.echo(f"Starting robovast-service on http://{host}:{port} "
               f"(OpenAPI at /docs{mcp_note})")
    click.echo(f"Backend: {backend} | storage: {storage} | Ctrl-C to stop")
    if workspace_dir:
        click.echo(f"Pinned read-only workspace: {workspace_dir}")
    _serve(impl, host=host, port=port, mount_mcp=mount_mcp)


@cli.command()
@click.option('--flavor', default='', metavar='NAME',
              help='Also check what this cluster flavor needs (e.g. gcp).')
@click.option('--context', '-x', default=None, metavar='NAME',
              help='Kubernetes context to check (default: the active one).')
def doctor(flavor, context):
    """Check the prerequisites, before something else finds them the hard way.

    Reads only — safe to run at any time, which is what makes it usable both as the
    first step of an install and as the first step of debugging one.

    Every failure names its remedy: a check that reports "helm: missing" and stops has
    moved the problem rather than solved it.
    """
    from robovast.common.cli.doctor import run_checks

    checks = run_checks(flavor=flavor, context=context)
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
def login(url, token, name):
    """Store the credentials for a robovast-service, so every command can reach it.

    \b
      vast login https://robovast.example.org

    The operator hands you the URL and the access token. Both are kept per **user** in
    ``~/.config/robovast/config.json`` (mode 0600), not in a project ``.env``: which
    instance you talk to follows you rather than a checkout, and a token inside a
    project directory is one ``git add -A`` from being committed.

    The name is optional and **self-declared** — with one shared secret nobody can prove
    who they are, so it is a label for "who started this run?", not an identity. Give an
    empty one and campaigns you start are recorded as unattributed rather than as
    somebody invented.

    Run it again to change any of the three. ``vast logout`` forgets them.
    """
    from robovast.common.cli import login as login_config

    stored_url, stored_token, stored_name = login_config.credentials()

    if not url:
        url = stored_url
        if not url:
            raise click.ClickException(
                "no service URL given and none stored — "
                "run 'vast login https://robovast.example.org'")
    url = url.rstrip('/')

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
    from robovast.service.client import RobovastClient
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
    # same person's CLI runs are labelled.
    click.echo("\nTo give an agent the same access over HTTP:")
    click.echo("  " + " \\\n      ".join(
        login_config.mcp_add_command(url, token, name)))
    click.echo("\n(A local 'vast mcp serve' over stdio needs none of that — it reads "
               "this login.)")


@cli.command()
def logout():
    """Forget the stored robovast-service credentials."""
    from robovast.common.cli import login as login_config
    if login_config.clear():
        click.echo("✓ logged out")
    else:
        click.echo("Not logged in.")


@cli.command()
@click.option('--port', default=0, type=int, metavar='PORT',
              help='Service port to open. 0 (default) uses the conventional port.')
@click.option('--no-browser', is_flag=True,
              help='Do not launch a browser.')
def ui(port, no_browser):
    """Open the RoboVAST web UI in your browser.

    A thin shortcut: it opens whichever service this machine talks to and does
    nothing else — the one answering on the conventional local port, or the one
    ``vast login`` stored.

    The service serves the web UI, the REST API and ``/mcp`` on one port, so what
    this opens is all a browser needs — the same place the CLI and the MCP server
    resolve.
    """
    import webbrowser  # pylint: disable=import-outside-toplevel

    from robovast.common.cli.service_target import detected_service_url
    from robovast.execution.cluster_execution.service_deploy import SERVICE_PORT

    if port:
        url = f'http://127.0.0.1:{port}'
        if not _service_alive(url):
            raise click.ClickException(f"no robovast-service answering at {url}.")
    else:
        url = detected_service_url()
        if not url:
            raise click.ClickException(
                "no robovast-service found. Either run one on this machine (it "
                f"answers on :{SERVICE_PORT}), or point at the deployed one with "
                "'vast login https://robovast.<domain>'.")
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
    command here prints the target it resolved: a service on the conventional local
    port if one answers, otherwise the one ``vast login`` stored.
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
def workspace_init(directory, name, excludes, namespace, context):
    """Create a workspace and upload every file from DIRECTORY into it.

    ``.vast``/``.osc`` are written inline; all other files go through the upload
    side channel (executability preserved). Hidden files/dirs (``.cache`` etc.) and
    output dirs (``results/``, plus any ``--exclude``) are skipped. Prints the new
    workspace id — open it in the web UI's Config tab.

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
def workspace_update(workspace, directory, excludes, prune, namespace, context):
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
    with service_client(namespace, context) as (client, target):
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
def workspace_world(workspace, path, targets, entities, as_json, namespace, context):
    """Describe the world this campaign's simulator will load.

    The other half of authoring a ``sim:`` override: ``vast workspace world`` says what the
    world *offers* — which plugins an override can address, and with ``--targets`` which model
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
        for plugin in described.plugins:
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


@workspace.command('delete')
@click.argument('workspace', metavar='WORKSPACE')
@target_options
def workspace_delete(workspace, namespace, context):
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
    """
    @functools.wraps(func)
    def _wrapped(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except (ValueError, KeyError) as e:
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
    command prints the target it resolved — a service on the conventional local port
    if one answers, otherwise the one ``vast login`` stored.
    """


@image.command('build')
@click.option('--workspace-id', required=True,
              help='Workspace whose project to build. Required: the service runs a '
                   "workspace's project, never a CWD one.")
@click.option('--config-path', default='', help='Which .vast when the workspace has several.')
@click.option('--wait/--no-wait', default=True, help='Wait for the build to finish (default).')
@target_options
def image_build(workspace_id, config_path, wait, namespace, context):
    """Build (or reuse) the experiment image from the project's ``build:`` section."""
    import time as _time

    from robovast.service.interface import BuildImageRequest
    with service_client(namespace, context) as (client, target):
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
