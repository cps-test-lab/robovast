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
NO_SERVICE = ("no robovast-service reachable — start one on this machine "
              "('vast serve'), or point at the deployed one "
              "('vast login https://robovast.<domain>'), so the "
              "MCP has an execution authority to drive. Report this and stop; do not "
              "substitute a local docker/script run, which produces no pinned image, "
              "no provenance and no repetitions, and answers a different question")


#: What an unreachable exec path costs, in the vocabulary of the tools. Composed here
#: because only this layer knows the tool names -- the service layer, which raises the
#: error, has no business naming them -- and once, because five tools saying it five ways
#: is what made a deployment property read as five separate defects. Named by class rather
#: than as a list to maintain: a tool that answers by asking an image cannot answer here,
#: whichever tool it is, and the examples are examples.
EXEC_PATH_CONSEQUENCE = (
    "Every tool that answers by asking a container cannot answer on this deployment -- "
    "exec_in_container, the image catalogs (list_scenario_actions, list_roqsim_plugins), "
    "describe_scenario, describe_world -- and validate_project reports its world and "
    "scenario checks as unchecked rather than passed. Everything that reads what a "
    "campaign produced is unaffected: status, logs, results, plots and SQL. Report this "
    "and carry on with those; nothing about a .vast changes it."
)


def exec_path_unavailable(e: BaseException) -> bool:
    """Whether *e* says no command can run in a container on this deployment.

    Two spellings of one fact, because the MCP runs in two places: mounted inside the
    service it is handed the :class:`~robovast.common.errors.ExecPathUnavailable` itself,
    and over HTTP it is handed a :class:`~robovast.service.interface.ServiceError` carrying
    the :data:`~robovast.service.interface.EXEC_PATH_UNAVAILABLE` code, the exception type
    being the one thing that cannot cross that boundary. Both are structural: neither reads
    the message, which is why the message stays free to be reworded.
    """
    from robovast.common.errors import ExecPathUnavailable
    from robovast.service.interface import EXEC_PATH_UNAVAILABLE
    return (isinstance(e, ExecPathUnavailable)
            or getattr(e, "code", "") == EXEC_PATH_UNAVAILABLE)


def error_result(e: BaseException) -> dict:
    """A tool's error dict, carrying the caller's next move when the error knows it.

    ``next_step`` is already the convention on the paths that *succeed* — a literal command
    with the ids filled in — because an answer that hands back only an id leaves "and now
    wait for it" to be remembered. A refusal is where the next move is least obvious, and
    carried nothing: the reported bug behind this helper was an agent told "the image is not
    built" right after it had built the image, with no way to learn that a sibling build was
    still running.

    So an :class:`~robovast.common.errors.ActionableError` passes its hint through here, and
    every other exception is reported exactly as before. Absence of ``next_step`` is
    meaningful: it says there is nothing obvious to do, not that someone forgot.

    A refusal whose *class* a caller must act on rather than print is answered with what
    that class costs here — see :func:`exec_path_unavailable` and
    :data:`EXEC_PATH_CONSEQUENCE`.
    """
    if exec_path_unavailable(e):
        # The consequence, not only the cause. The cause is already a complete sentence at
        # the source; what each tool could not say on its own is what is unavailable *here*,
        # which is a fact about the deployment and the same one for all of them.
        return {"error": f"{e}. {EXEC_PATH_CONSEQUENCE}"}
    result = {"error": str(e)}
    next_step = getattr(e, "next_step", "")
    if next_step:
        result["next_step"] = next_step
    return result


#: Set by :func:`use_in_process_service` when the MCP app is mounted inside the service.
_IN_PROCESS = None


def use_in_process_service(impl):
    """Serve tools from *impl* directly, for the MCP mounted inside the service.

    ``vast serve`` mounts the MCP app on its own port, so in that deployment the tools
    and the implementation are in **one process**. Without this they still went out over
    loopback HTTP and back in — a wasted round trip per tool call, and once the service
    required a token, a process authenticating to itself and only working because it
    happened to hold its own secret.

    Off-cluster and over stdio nothing calls this, and the HTTP path below is used.
    """
    global _IN_PROCESS  # noqa: PLW0603  # pylint: disable=global-statement
    # process-wide, set once at app construction
    _IN_PROCESS = impl


def service_client():
    """A client for a reachable service, or ``None``.

    In-process when the MCP app is mounted inside the service; otherwise the service
    answering on the conventional local port, or the one ``vast login`` stored.
    """
    if _IN_PROCESS is not None:
        return _IN_PROCESS
    from robovast.client.service_target import detected_service_url
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
    """An absolute URL for *route*, or ``""`` when nobody can name an origin for it.

    The address space is also the URL space, so pointing at the web API for a large
    payload costs nothing to build — and per AGENTS.md §4 a field that cannot be used is
    **omitted** rather than reported as null or, worse, guessed at.

    Two sources, in this order. An HTTP transport's ``base_url`` is *where this caller
    dialled, and it worked*, which beats any declaration — it stays right for a service
    reached through a tunnel or a port-forward, where what the service believes about
    itself would not be. Failing that, the service's own declaration, which is the answer
    for the case with no transport at all: the MCP mounted inside the service, where the
    client *is* the implementation.
    """
    base = getattr(client, "base_url", "") or _declared_base(client)
    return f"{base}{route}" if base else ""


def _declared_base(client) -> str:
    """The origin the service declares for its callers, or ``""``.

    Not cached: an HTTP transport never reaches this (its own base answers first), and
    ``version()`` is deliberately the cheapest call in the interface — neither lane dials
    anything to answer it — so in-process this is a local attribute read.

    Never raises. A link is an extra way to reach a payload; failing a tool call over one
    would trade the answer for the convenience.
    """
    try:
        return getattr(client.version(), "web_base", "") or ""
    except Exception:  # noqa: BLE001
        logger.debug("could not read the service's declared origin", exc_info=True)
        return ""
