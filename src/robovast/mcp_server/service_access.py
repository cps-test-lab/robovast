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

"""Reaching the ``robovast-service`` — one implementation for every MCP tool.

The service is the single execution authority: there is no local subprocess lane, so a
tool that cannot reach it must say so rather than do something else. That makes *how* a
tool obtains a client a shared decision, not a per-module one.

Returning ``None`` when nothing answers — rather than ``RobovastClient("")`` — is the
load-bearing part. The empty-URL client is a perfectly good in-process ``LocalTransport``,
so a caller that meant to reach a service and got that instead reads local disk and reports
success, which is the "no service" failure wearing the mask of an answer.
"""

import logging

logger = logging.getLogger(__name__)

#: Canonical failure when no ``robovast-service`` answers on the conventional local port.
NO_SERVICE = ("no robovast-service reachable — start a 'vast serve' (local) or "
              "'vast serve --attach' (cluster), or open a tunnel to one, so the "
              "MCP has an execution authority to drive. Report this and stop; do not "
              "substitute a local docker/script run, which produces no pinned image, "
              "no provenance and no repetitions, and answers a different question")


def service_client():
    """A ``RobovastClient`` bound to a reachable service, or ``None``.

    A service answers on the conventional local port when a ``vast serve`` runs locally,
    or a tunnel / ``vast serve --attach`` reaches a remote VM or cluster.
    """
    from robovast.common.cli.service_target import detected_service_url
    url = detected_service_url()
    if not url:
        return None
    from robovast.service.client import RobovastClient
    return RobovastClient(url)


def client_or_local():
    """The service when one answers, otherwise an explicit in-process transport.

    For the operations that are meaningful **without** a service — reading a local
    workspace, listing a campaign's declared plots — as opposed to driving execution,
    which requires one and reports :data:`NO_SERVICE` instead.

    Written as ``service_client() or LocalTransport()`` rather than
    ``RobovastClient(detected_service_url())``: the two behave identically today, but the
    second reads as "connect to the detected service" while silently doing something else
    when nothing was detected. Spelling the fallback out means a reader can see which of
    the two answered.
    """
    from robovast.service.local_transport import LocalTransport
    return service_client() or LocalTransport()


def web_url(client, route: str) -> str:
    """An absolute URL for *route*, or ``""`` when this caller has no URL to give.

    The address space is also the URL space, so pointing at the web API for a large
    payload costs nothing to build — but only an HTTP transport has a base to build it
    from. An in-process ``LocalTransport`` has none, and per AGENTS.md §4 a field that
    cannot be used is **omitted** rather than reported as null or, worse, guessed at.
    """
    base = getattr(client, "base_url", "")
    return f"{base}{route}" if base else ""
