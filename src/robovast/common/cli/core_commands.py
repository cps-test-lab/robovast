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
``robovast.cli_plugins`` entry point. They live here
rather than with the root group because each needs something a client install does not
have -- a service implementation, a Docker daemon, the web UI build, the results reader.

Splitting them out is what lets ``pip install robovast-client`` produce a ``vast`` that
is complete rather than truncated: the verbs it cannot run are not registered, so they
are absent instead of present-and-failing.
"""

import os
import shutil

import click

from robovast.client.logging_config import get_logger
from robovast.client.service_target import _service_alive
from robovast.service.interface import DEFAULT_PORT


logger = get_logger(__name__)


def ensure_ui_built(rebuild: bool = False) -> None:
    """(Re)build the web UI's ``frontend/ui/dist`` for a source checkout, when needed.

    No-op unless run from a source tree: a packaged install / in-cluster pod has
    no ``frontend/ui/`` sources (its dist is baked in and pointed at by
    ``ROBOVAST_UI_DIST``), so there is nothing — and no ``npm`` — to build. In a
    checkout, build only when ``frontend/ui/dist`` is missing or older than the UI
    sources (or when *rebuild*), so a normal ``vast serve`` costs just an mtime scan.

    Public because ``vast serve`` is not its only caller: on a cold tree this is an
    npm install and a bundle, minutes of work that says nothing about whether the
    service comes up, so ``.github/test_vast.py`` runs it before it starts timing a
    service startup.
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
              help='Interface to bind. Keep 127.0.0.1 unless behind a tunnel or a proxy '
                   'that terminates TLS: a local service is plain HTTP by design, so the '
                   'access token would otherwise cross the network in clear text.')
@click.option('--port', default=DEFAULT_PORT, show_default=True, type=int,
              help='Port to listen on. The conventional one, which every client probes '
                   'before falling back to a stored login.')
@click.option('--uds', default=None, metavar='SOCKET',
              help='Listen on this Unix socket instead of --host/--port. The in-cluster '
                   'layout: a front owns the port and routes here, and the data routes '
                   'to their own process (vast serve-data).')
@click.option('--rebuild-ui', is_flag=True,
              help='Force a web UI rebuild even if frontend/ui/dist looks up to date '
                   '(source checkout only).')
@click.option('--mcp/--no-mcp', 'mount_mcp', default=True, show_default=True,
              help='Also expose the MCP server at /mcp on this same port, so one '
                   'URL and one token cover the web UI, the REST API, and the MCP '
                   'tools together. Pass --no-mcp to serve the API without them.')
@click.option('--results-dir', 'results_dir', default=None, metavar='DIR',
              type=click.Path(file_okay=False),
              help='Where campaigns this service runs land, on the serve host. Omitted, a '
                   'service-owned directory beside the workspaces store is used.')
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
def serve(host, port, uds, rebuild_ui,
          results_dir, workspace_dir, mount_mcp):
    """Run the robovast-service process: what the in-cluster Deployment starts.

    The service is the implementation ``robovast-cluster`` ships: it drives each
    campaign against Kubernetes Jobs, and is what the ``robovast-service`` Deployment
    runs. It reads which cluster from its own pod, and is refused outside one: the
    campaigns' pods deliver their results back to this process, which they cannot do
    across a developer's machine. A developer runs a service with ``vast cluster
    setup minikube``; to debug the driver against a real cluster, run this command in
    the cluster's network with the Service's traffic steered to it (``mirrord exec
    --target deployment/robovast-service --steal -- vast serve``).

    The service serves the web UI at the same URL — from a source checkout this
    (re)builds ``frontend/ui/dist`` first when it is missing or stale (needs ``npm``;
    ``--rebuild-ui`` forces it).

    Security: every request needs the shared token (``ROBOVAST_AUTH_TOKEN``). When
    none is configured one is generated at startup and printed as a login URL you
    can click — there is no unauthenticated mode. It still binds ``127.0.0.1`` by
    default; publishing it is ``vast cluster setup --ingress-host``, which
    also insists on TLS. Web UI + OpenAPI docs at ``/`` and ``/docs``; MCP tools at
    ``/mcp`` (see ``--no-mcp``) — one URL reaches all three.
    """
    from robovast.service.app import serve as _serve

    # The campaign driver runs in this same process, so everything it reads from
    # os.environ comes from the ./.env the group callback loaded: share credentials for
    # '--upload-to-share', the registry, ROBOVAST_PROJECT. In-pod there is neither a
    # project .env nor a user config, so the deployment env is the whole environment.
    # Build the SPA the service serves, so a source checkout needs one command
    # (no-op for a packaged/in-cluster install — see ensure_ui_built).
    ensure_ui_built(rebuild=rebuild_ui)

    in_pod = bool(os.environ.get('KUBERNETES_SERVICE_HOST'))

    # Pinning uses the directory in place, so it needs the service to run on the host
    # that holds it, which rules out a pod: there is no such directory there.
    if workspace_dir and in_pod:
        raise click.ClickException(
            "--workspace-dir pins a directory on the serve host, and a Kubernetes "
            "pod has no such directory. Upload the project instead with "
            "'vast workspace init <dir>'.")

    from robovast.service.serve_backends import resolve as resolve_backend
    try:
        backend, provider = resolve_backend()
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    from robovast.service.workspaces import WorkspaceStore
    store = WorkspaceStore(workspace_dir=workspace_dir)
    impl = provider.build(in_pod=in_pod,
                      store=store, workspace_dir=workspace_dir,
                      results_dir=os.path.abspath(results_dir) if results_dir else None)
    storage = provider.storage

    mcp_note = ", MCP at /mcp" if mount_mcp else ""
    click.echo(f"Starting robovast-service on {uds or f'http://{host}:{port}'} "
               f"(OpenAPI at /docs{mcp_note})")
    click.echo(f"Backend: {backend} | storage: {storage} | Ctrl-C to stop")
    if workspace_dir:
        click.echo(f"Pinned read-only workspace: {workspace_dir}")
    _serve(impl, host=host, port=port, mount_mcp=mount_mcp, uds=uds)


@click.command(name='serve-data')
@click.option('--results-dir', 'results_dir', required=True, metavar='DIR',
              type=click.Path(file_okay=False),
              help='The results root this plane reads and writes: the same directory '
                   'the control plane serves campaigns from.')
@click.option('--uds', default=None, metavar='SOCKET',
              help='Listen on this Unix socket, behind the front that owns the port.')
@click.option('--host', default='127.0.0.1', show_default=True,
              help='With --port: interface to bind.')
@click.option('--port', default=None, type=int, metavar='PORT',
              help='Listen on a TCP port by itself, for a data plane run without a front.')
def serve_data(results_dir, uds, host, port):
    """Serve the data plane on its own: the tar routes under /data, nothing else.

    The in-cluster service pod runs this beside ``vast serve``, so a pod delivering
    gigabytes of run output never shares a process with the run view or the admission
    loop. It verifies the same ``ROBOVAST_AUTH_TOKEN`` the control plane enforces and
    refuses to start without one. A ``vast serve`` on its own already serves these
    routes in-process; this command exists for the layout where a front splits them off.
    """
    from robovast.service.data_app import serve_data as _serve_data
    if not uds and port is None:
        raise click.ClickException("pass --uds SOCKET or --port PORT")
    try:
        _serve_data(os.path.abspath(results_dir), uds=uds, host=host, port=port)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


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

    if port:
        url = f'http://127.0.0.1:{port}'
        if not _service_alive(url):
            raise click.ClickException(f"no robovast-service answering at {url}.")
    else:
        url = detected_service_url()
        if not url:
            raise click.ClickException(
                "no robovast-service found. Either run one on this machine (it "
                f"answers on :{DEFAULT_PORT}), or point at the deployed one with "
                "'vast login https://robovast.<domain>'.")
    click.echo(f"✓ robovast-service: {url}   (web UI + REST API + /docs)")
    if not no_browser:
        webbrowser.open(url)
