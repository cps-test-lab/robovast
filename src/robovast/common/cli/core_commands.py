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

"""``vast`` verbs that need the core: running a service, and building for one.

These attach to the root group (:mod:`robovast.client.cli`) through the
``robovast.cli_plugins`` entry point, exactly as the execution lanes do. They live here
rather than with the root group because each needs something a client install does not
have -- a service implementation, a Docker daemon, the web UI build, the results reader.

Splitting them out is what lets ``pip install robovast-client`` produce a ``vast`` that
is complete rather than truncated: the verbs it cannot run are not registered, so they
are absent instead of present-and-failing.
"""

import logging
import os
import shutil
import sys
import tarfile

import click

from robovast.client.logging_config import get_logger
from robovast.client.project_config import ProjectConfig, get_project_config
from robovast.client.service_target import _service_alive
from robovast.client.service_target import echo_target as _echo_target

from .checks import check_docker_access

logger = get_logger(__name__)


@click.command()
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
        from ..common import load_config  # pylint: disable=import-outside-toplevel
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


@click.command()
@click.option('--host', default='127.0.0.1', show_default=True,
              help='Interface to bind. Keep 127.0.0.1 unless behind a tunnel/proxy: '
                   'the service is unauthenticated in v1.')
@click.option('--port', default=8800, show_default=True, type=int,
              help='Port to listen on.')
@click.option('--backend', type=click.Choice(['auto', 'local', 'cluster']),
              default='auto', show_default=True,
              help="Execution backend. 'auto' picks 'cluster' when running inside "
                   "a Kubernetes pod, else 'local' Docker.")
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
                   'URL and one token cover the web UI, the REST API, and the MCP '
                   'tools together. Pass --no-mcp to serve the API without them.')
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

    if context is not None and backend != 'cluster':
        raise click.ClickException(
            "--context/-x only applies to '--backend cluster' — it selects which "
            "Kubernetes context to dispatch campaigns into.")

    # Pinning uses the directory in place, so it needs the service to run on the host
    # that holds it. That rules out a pod (no such directory) but NOT an off-cluster
    # '--backend cluster' driver, which runs here and reads project inputs from this
    # filesystem exactly as the local lane does.
    if workspace_dir and in_pod:
        raise click.ClickException(
            "--workspace-dir pins a directory on the serve host, and a Kubernetes "
            "pod has no such directory. Upload the project instead with "
            "'vast workspace init <dir>'.")

    from robovast.service.serve_backends import resolve as resolve_backend
    try:
        lane = resolve_backend(backend)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    store = None
    if backend == 'cluster':
        from robovast.service.workspaces import WorkspaceStore
        store = WorkspaceStore(workspace_dir=workspace_dir)
    impl = lane.build(in_pod=in_pod, context=context, namespace=k8s_namespace,
                      store=store, workspace_dir=workspace_dir)
    storage = lane.storage

    mcp_note = ", MCP at /mcp" if mount_mcp else ""
    click.echo(f"Starting robovast-service on http://{host}:{port} "
               f"(OpenAPI at /docs{mcp_note})")
    click.echo(f"Backend: {backend} | storage: {storage} | Ctrl-C to stop")
    if workspace_dir:
        click.echo(f"Pinned read-only workspace: {workspace_dir}")
    _serve(impl, host=host, port=port, mount_mcp=mount_mcp)


@click.command()
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

    from robovast.client.service_target import detected_service_url
    from robovast.service.interface import DEFAULT_PORT as SERVICE_PORT

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


@click.command()
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
                from ..execution import is_campaign_dir  # pylint: disable=import-outside-toplevel
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
