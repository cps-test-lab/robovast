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

"""The one way to block until a campaign is over.

Polling the service's ``get_status`` until :func:`~robovast.client.status.is_terminal`
is not lane-specific: the service drives every campaign, so its phase *is* the
campaign's, whether the runs execute in local Docker or as Kubernetes Jobs. This lived
in ``execution_utils/cluster_run`` under a cluster-flavoured name, which is why the MCP
was about to grow a fourth hand-rolled poll loop beside the CLI's monitor and this one.
:data:`~robovast.client.status.TERMINAL_PHASES` records what that costs: the terminal
test itself was previously re-inlined, with divergent membership, across the CLI, the
service and the MCP plugins.

Two properties every caller depends on and none should re-implement:

* **A failed poll is not a failed wait** -- until polls stop succeeding altogether.
  Restarting the service mid-run, or a network hiccup, drops one status read; the campaign
  is untouched, so the loop keeps going. But a service that is simply gone fails every
  poll identically and silently, and waiting forever on that is not tolerance, it is a
  hang: see :mod:`poll_health`.
* **Terminal means terminal.** It became true to say so only once a campaign stopped
  publishing ``finished`` before its share and postprocessing had run — see
  ``controller.end_campaign``.
"""

import logging
import time
from typing import Callable, Optional

from robovast.client.status import Status, is_terminal

from .poll_health import STALE_POLL_LIMIT_S, StalePolls

logger = logging.getLogger(__name__)

#: How often to ask, when a caller expresses no preference.
DEFAULT_POLL_INTERVAL_S = 5.0


def wait_for_campaign_status(campaign_id: str, *, client=None, service_url: str = "",
                             interval: float = DEFAULT_POLL_INTERVAL_S,
                             timeout: Optional[float] = None,
                             feedback: Optional[Callable[[str], None]] = None,
                             stop_when: Optional[Callable[[Status], bool]] = None,
                             stale_limit_s: float = STALE_POLL_LIMIT_S
                             ) -> Status:
    """Block until *campaign_id* reaches a terminal phase; return its final Status.

    Args:
        campaign_id: The campaign to wait for.
        client: A pre-built service client. Built from *service_url* when omitted.
        service_url: The robovast-service to poll (ignored when *client* is given).
        interval: Poll interval in seconds.
        timeout: Overall timeout in seconds; ``None`` waits indefinitely.
        feedback: Optional ``str -> None`` sink called once per *changed* phase, so a
            caller can narrate the wait without polling for the narration.
        stale_limit_s: How long *every* poll may fail before giving up. Exposed for
            tests; no command line reaches it, because the default is not a preference.
        stop_when: Optional extra predicate to return early on, for the states that are
            not terminal but are not worth waiting through either — a wedged run being
            the one that matters. The caller distinguishes the two by testing the
            returned phase, since only it knows what it asked to stop for.

    Raises:
        TimeoutError: if *timeout* elapses first. The campaign is unaffected — it is
            still running and can be waited on again, which is what makes a bounded
            wait safe to retry rather than a partial failure to recover from.
        PollsStopped: if *every* poll failed for :data:`~.poll_health.STALE_POLL_LIMIT_S`.
            Also leaves the campaign running, but points at the service rather than at it.
    """
    if client is None:
        from robovast.service.http_client import RobovastClient
        client = RobovastClient(service_url)
    deadline = None if timeout is None else (time.monotonic() + timeout)
    say = feedback or (lambda _msg: None)
    last_report = None
    polls = StalePolls(stale_limit_s)

    while True:
        status = None
        try:
            status = client.get_status(campaign_id)
        except Exception as e:  # noqa: BLE001 - transient service/network hiccup
            # Deliberately not fatal: a service restart mid-wait is an expected event
            # on the attach lane, and treating it as the end of the wait would report a
            # live campaign as unreachable for the price of one dropped read.
            logger.debug("status poll for %s failed: %s", campaign_id, e)
            polls.failed(e)
        else:
            polls.succeeded()

        if status is not None:
            report = status.phase + (f"/{status.stage}" if status.stage else "")
            if report != last_report:
                say(f"{campaign_id}: {report}")
                last_report = report
            if is_terminal(status.phase) or (stop_when and stop_when(status)):
                return status

        if deadline is not None and time.monotonic() > deadline:
            raise TimeoutError(
                f"Campaign {campaign_id!r} did not finish within {timeout}s "
                f"(last state: {last_report})")
        polls.check(f"campaign {campaign_id!r}")
        time.sleep(interval)


def wait_for_campaign_outcome(campaign_id: str, *, client=None, service_url: str = "",
                              interval: float = DEFAULT_POLL_INTERVAL_S,
                              timeout: Optional[float] = None,
                              feedback: Optional[Callable[[str], None]] = None) -> str:
    """Block until *campaign_id* is over; report ``"succeeded"`` or ``"failed"``.

    The two-value reduction of :func:`wait_for_campaign_status`, for callers that branch
    rather than inspect -- ``vast exec cluster run --wait-and-download`` is the one that
    exists. It also echoes the failure reason, which is on the Status and would otherwise
    be dropped by the reduction.

    This was ``execution_utils.cluster_run.wait_for_cluster_campaign``, in the core, under
    a cluster-flavoured name its own docstring called a mistake: nothing about waiting on
    the service's status contract is lane-specific, and leaving it there made the launch
    verb -- otherwise pure client code -- unable to move out of ``robovast-cluster``.
    """
    from robovast.client.status import Phase
    say = feedback or (lambda _msg: None)
    status = wait_for_campaign_status(
        campaign_id, client=client, service_url=service_url, interval=interval,
        timeout=timeout, feedback=feedback)
    if status.phase == Phase.FAILED and status.error:
        say(f"{campaign_id}: {status.error}")
    return "succeeded" if status.phase == Phase.FINISHED else "failed"
