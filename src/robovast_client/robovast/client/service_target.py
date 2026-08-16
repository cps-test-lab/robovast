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

Every service-touching command — ``ui``, ``workspace …``, the ``exec cluster`` verbs,
``results download`` — answers one question: which service? This module holds the single
resolver they all use, so the answer (and the way it is printed) is identical everywhere.

A campaign, a workspace and the web UI all live in whichever service you talk to, so the
resolution is announced rather than assumed. Two ways in, in order:

* **the service answering on the conventional local port** — a local ``vast serve``, or
  any tunnel someone put there. First, so a service started on this machine is what
  commands follow;
* **the service ``vast login`` stored** — the deployed one behind its Ingress, which is
  how a user with no kubeconfig reaches it.

``--cluster`` used to be a third, opening an ephemeral ``kubectl port-forward`` per call.
It is gone: with the service published and authenticated, an operator uses the same path
as everybody else, and a tunnel is no longer part of anyone's normal flow. When the
Ingress itself is broken, ``kubectl port-forward svc/robovast-service 8800:8800`` still
puts a service on the conventional port, and the first rule above finds it — that is
kubectl's feature, not one this module has to wrap.

When nothing answers: ``workspace`` acts on this machine in-process (its local store),
while a cluster verb (``require_service=True``) errors rather than silently running
local Docker.
"""

import contextlib

import click

def _service_alive(url):
    import urllib.error  # pylint: disable=import-outside-toplevel
    import urllib.request  # pylint: disable=import-outside-toplevel
    try:
        with urllib.request.urlopen(f'{url}/healthz', timeout=1) as resp:  # noqa: S310
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def detected_service_url():
    """The service this machine should talk to, or ``''``.

    Two ways in, tried in this order:

    1. **A service answering on the conventional local port.** A local ``vast serve``,
       or any tunnel someone has put there. First, so the dev workflow is unchanged:
       start a service on this machine and every command follows it, exactly as before.
    2. **A stored ``vast login``.** The deployed instance behind its Ingress — how a
       user with no kubeconfig reaches it.

    This is a deliberate widening of a contract that used to be "the local port, full
    stop, no environment variable, no guessing". That was right while the only way to
    reach a remote service was a tunnel *to* the local port; it cannot express "the
    service is at https://robovast.example.org and here is my token". The narrowness is
    preserved where it mattered — there is still no ambient env var naming a service,
    and ``echo_target`` still prints what was resolved, so the choice is never silent.
    """
    from robovast.service.interface import DEFAULT_PORT
    probe = f'http://127.0.0.1:{DEFAULT_PORT}'
    if _service_alive(probe):
        return probe
    from robovast.client.login import credentials
    url, _token, _name = credentials()
    return url or ''


def target_options(func):
    """Add the target switches every service-touching command shares.

    ``--cluster`` used to be here, opening a ``kubectl port-forward`` for the call.
    There is one way in now — the service on the conventional local port, or the one
    ``vast login`` stored — so an operator uses the same path as everybody else.
    """
    func = click.option(
        '--context', '-x', default=None, metavar='NAME',
        help='Kubernetes context, when a command has to name one.')(func)
    func = click.option(
        '--namespace', '-n', default='default', show_default=True,
        help='Namespace the robovast-service runs in.')(func)
    return func


@contextlib.contextmanager
def service_client(namespace='default', context=None, *, require_service=False):
    """Yield ``(client, label)`` for the resolved service.

    See the module docstring for the two ways in. Every caller prints the resolved
    target, so which service answered is never left implicit.

    ``require_service=True`` makes a command that finds none raise instead of yielding a
    local-Docker client — used by the cluster verbs, which must never silently run
    locally.

    *namespace* and *context* are accepted and unused here; commands still take them for
    the Kubernetes operations they perform themselves.
    """
    del namespace, context
    # The factory lives in http_client, which is part of this distribution.
    # `robovast.service.client` is core's re-export of it, and importing through there
    # made every client command need the core installed -- while still failing only at
    # call time, so an import check could not see it.
    from robovast.service.http_client import RobovastClient

    url = detected_service_url()

    if not url and require_service:
        raise click.ClickException(
            "No robovast-service found. Cluster operations go through the service.\n"
            "Either start one here ('vast serve --backend cluster'), or point at the "
            "deployed one ('vast login https://robovast.<domain>').")

    client = RobovastClient(url)
    if url:
        label = f"service ({url}) [detected]"
    else:
        # Only a full install reaches here -- with no URL, `RobovastClient` returns the
        # in-process transport, and refuses outright when there is none. So the store
        # path, which describes that transport and not this layer, is read *after* the
        # client exists rather than imported by a distribution that has no store.
        from robovast.service.workspaces import default_workspaces_root
        label = f"this machine, in-process (store: {default_workspaces_root()})"
    yield client, label


def echo_target(label):
    """Say which store we resolved.

    Never leave this implicit: a workspace created on this machine is invisible
    to a web UI served by the cluster, and vice versa — the one trap this whole
    surface has. Auto-detection still prints, so it is announced.
    """
    click.echo(f"Target: {label}")
    if label.startswith('this machine'):
        click.echo("  (no service found; point at the deployed one with "
                   "'vast login <url>' — that is the store its web UI reads)")
