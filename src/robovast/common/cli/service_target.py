# Copyright (C) 2026 Frederik Pasch
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

"""Shared "which robovast-service, and how to reach it" resolution.

Every service-touching command — ``ui``, ``workspace …``, the ``exec cluster``
verbs, ``results download`` — answers one question: local or cluster? This module
holds the single resolver they all use, so the answer (and the way it is printed)
is identical everywhere.

A campaign, a workspace and the web UI all live in whichever service you talk to,
so the target is explicit: ``--cluster`` says "the in-cluster service", reached by
an ephemeral ``kubectl port-forward`` opened and closed around the call.
``--context/-x`` does not select the target — it only disambiguates *which*
cluster, exactly as it does for ``vast exec cluster setup``.

There are exactly two ways in, no environment variables and no fallback chain:

* ``--cluster`` — open an **ephemeral** port-forward to the in-cluster service for
  the call and close it after (no tunnel to babysit).
* otherwise — **the service already answering on the conventional local port**: a
  local ``vast serve`` *or* a ``vast serve --attach`` tunnel to the in-cluster
  service. This one convention is what lets a flagless command **follow whatever is
  serving**, and it is the only way to reach a remote service — bring up your own
  tunnel to ``127.0.0.1:8800`` and every command auto-detects it.

When nothing answers there and ``--cluster`` was not given: ``workspace`` acts on
this machine in-process (its local store), while a cluster verb
(``require_service=True``) errors rather than silently running local Docker.
"""

import contextlib

import click

#: How long to wait for ``kubectl port-forward`` to report the tunnel is up.
_UI_FORWARD_TIMEOUT = 15.0


def _start_port_forward(namespace, context, local_port=0, echo=True):
    """Start ``kubectl port-forward`` to the service; return ``(proc, url)``.

    Waits (bounded) for kubectl to report the tunnel is up: an unreachable
    cluster otherwise leaves kubectl blocked with no output, and an unbounded
    readline would hang the command silently. ``local_port=0`` lets kubectl pick
    a free port — the actual one is parsed back out of its "Forwarding from"
    line, which is what makes an ephemeral per-call tunnel possible.
    """
    import re  # pylint: disable=import-outside-toplevel
    import selectors  # pylint: disable=import-outside-toplevel
    import subprocess  # pylint: disable=import-outside-toplevel
    import time  # pylint: disable=import-outside-toplevel

    from robovast.execution.cluster_execution.service_deploy import (
        SERVICE_NAME, SERVICE_PORT)

    cmd = ['kubectl']
    if context:
        cmd += ['--context', context]
    cmd += ['port-forward', '-n', namespace, f'svc/{SERVICE_NAME}',
            f'{local_port or ""}:{SERVICE_PORT}']

    if echo:
        click.echo(f"$ {' '.join(cmd)}")
    try:
        proc = subprocess.Popen(  # noqa: S603 - args are constructed, not shell
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    except FileNotFoundError:
        raise click.ClickException(
            "kubectl not found — it is needed to tunnel to the in-cluster service.")

    port, last_line = 0, ''
    deadline = time.time() + _UI_FORWARD_TIMEOUT
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    while time.time() < deadline and not port:
        if proc.poll() is not None:  # kubectl gave up / errored
            # kubectl's own message is the useful reason — report it rather than
            # guessing at the cause.
            detail = (proc.stdout.read() or '').strip() or last_line
            raise click.ClickException(
                f"kubectl port-forward failed: {detail[:300] or 'no output'}")
        if not sel.select(timeout=0.5):
            continue
        line = proc.stdout.readline()
        if not line:
            continue
        if echo:
            click.echo(line.rstrip())
        last_line = line.strip()
        match = re.search(r'Forwarding from 127\.0\.0\.1:(\d+)', line)
        if match:
            port = int(match.group(1))
    if not port:
        _stop_port_forward(proc)
        raise click.ClickException(
            f"timed out after {_UI_FORWARD_TIMEOUT:.0f}s forwarding to "
            f"svc/{SERVICE_NAME} in namespace {namespace!r} — is the cluster "
            "reachable and the service deployed ('vast exec cluster setup')?")
    return proc, f'http://127.0.0.1:{port}'


def _stop_port_forward(proc):
    import subprocess  # pylint: disable=import-outside-toplevel
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _service_alive(url):
    import urllib.error  # pylint: disable=import-outside-toplevel
    import urllib.request  # pylint: disable=import-outside-toplevel
    try:
        with urllib.request.urlopen(f'{url}/healthz', timeout=1) as resp:  # noqa: S310
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def detected_service_url():
    """The service answering on the conventional local port, or ``''``.

    The one convention every client shares: a local ``vast serve``, a ``vast serve
    --attach`` / SSH / ``kubectl port-forward`` tunnel binds ``127.0.0.1:8800``,
    and everything auto-detects it there. No environment variable, no guessing.
    """
    from robovast.execution.cluster_execution.service_deploy import SERVICE_PORT
    probe = f'http://127.0.0.1:{SERVICE_PORT}'
    return probe if _service_alive(probe) else ''


def target_options(func):
    """Add the target switches every service-touching command shares."""
    func = click.option(
        '--context', '-x', default=None, metavar='NAME',
        help='With --cluster: which Kubernetes context (default: the active '
             'one). Only needed to disambiguate between clusters.')(func)
    func = click.option(
        '--namespace', '-n', default='default', show_default=True,
        help='With --cluster: namespace the robovast-service runs in.')(func)
    func = click.option(
        '--cluster', is_flag=True,
        help='Talk to the in-cluster robovast-service (tunnels in for the call). '
             'Default: this machine.')(func)
    return func


@contextlib.contextmanager
def service_client(cluster=False, namespace='default', context=None, *,
                   require_service=False):
    """Yield ``(client, label)`` for the selected target.

    See the module docstring for the two ways in. ``--cluster`` only matters when
    you want the cluster and *no* ``vast serve --attach`` tunnel is open. Every caller
    prints the resolved target (with ``[detected]`` for the auto-follow case), so the
    auto-follow is announced, never silent.

    ``require_service=True`` makes a command that finds no service (and no
    ``--cluster``) raise instead of yielding a local-Docker client — used by the
    cluster verbs, which must never silently run locally.
    """
    from robovast.service.client import RobovastClient
    from robovast.service.workspaces import default_workspaces_root

    proc = None
    detected = False
    if cluster:
        proc, url = _start_port_forward(namespace, context, echo=False)
    else:
        url = detected_service_url()
        detected = bool(url)

    if not url and require_service:
        raise click.ClickException(
            "No robovast-service found. Cluster operations go through the service.\n"
            "Bring one up and it is auto-detected — 'vast serve --backend cluster' or "
            "'vast serve --attach' (tunnel to the deployed service) — or pass "
            "--cluster to tunnel to the in-cluster service for this call.")

    if cluster:
        label = f"in-cluster service ({url})"
    elif detected:
        label = f"service ({url}) [detected — following a running vast serve]"
    else:
        label = f"this machine, in-process (store: {default_workspaces_root()})"
    try:
        yield RobovastClient(url), label
    finally:
        if proc is not None:
            _stop_port_forward(proc)


def echo_target(label):
    """Say which store we resolved.

    Never leave this implicit: a workspace created on this machine is invisible
    to a web UI served by the cluster, and vice versa — the one trap this whole
    surface has. Auto-detection still prints, so it is announced.
    """
    click.echo(f"Target: {label}")
    if label.startswith('this machine'):
        click.echo("  (no running service found; pass --cluster to use the "
                   "in-cluster service — that is the store its web UI reads)")


