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

"""The one way to block until image builds are over — :mod:`campaign_wait` for builds.

The MCP used to answer this with a blocking ``wait_for_image_build`` tool, on the argument
that a build is minutes rather than days so holding the caller costs nothing. Two things
were wrong with that. The wait was capped at 600s, so a ROS build doing apt + pip + colcon
came back unfinished and had to be re-called — blocking again, with dead air in between,
in exactly the case where blocking costs most. And the surface already had the single-read
half (``get_image_build_status``); the blocking loop was a third thing beside it.

So a build waits the way a campaign does: a shell command an agent harness can background
and be notified about, over a shared loop, costing no MCP surface at all. The properties
:mod:`campaign_wait` documents hold here for the same reasons — **a failed poll is not a
failed wait** (a service restart drops one read; the build is untouched), and a timeout
leaves the build running, so a bounded wait is safe to retry rather than a partial failure.

What differs is arity. A project builds one image per container that adds packages, so a
caller normally has several ids and needs *all* of them; waiting for the first says nothing
about the rest. This waits for every id and reports the first failure among them.
"""

import logging
import time
from typing import Callable, Iterable, Optional

logger = logging.getLogger(__name__)

#: How often to ask, when a caller expresses no preference. Builds publish phase changes
#: faster than campaigns do, and a build is minutes rather than days.
DEFAULT_POLL_INTERVAL_S = 5.0

#: Phases meaning the image exists. Anything else terminal is a failure.
SUCCESS_PHASES = ("succeeded", "cached")


def wait_for_image_builds(build_ids: Iterable[str], *, client=None,
                          service_url: str = "",
                          interval: float = DEFAULT_POLL_INTERVAL_S,
                          timeout: Optional[float] = None,
                          feedback: Optional[Callable[[str], None]] = None) -> dict:
    """Block until every id in *build_ids* is done; return ``{build_id: ImageBuildStatus}``.

    Args:
        build_ids: The builds to wait for — ``build_experiment_image``'s ``builds`` values,
            or a single ``build_id``.
        client: A pre-built service client. Built from *service_url* when omitted.
        service_url: The robovast-service to poll (ignored when *client* is given).
        interval: Poll interval in seconds.
        timeout: Overall timeout in seconds; ``None`` waits indefinitely.
        feedback: Optional ``str -> None`` sink called once per *changed* phase, so a
            caller can narrate the wait without polling for the narration.

    Raises:
        ValueError: if *build_ids* is empty. Returning "all done" for nothing to wait on
            would report success for a build that was never started.
        TimeoutError: if *timeout* elapses first. The builds are unaffected.
    """
    pending = list(dict.fromkeys(bid for bid in build_ids if bid))
    if not pending:
        raise ValueError("no build ids to wait for")
    if client is None:
        from robovast.service.http_client import RobovastClient
        client = RobovastClient(service_url)
    deadline = None if timeout is None else (time.monotonic() + timeout)
    say = feedback or (lambda _msg: None)
    last_report: dict = {}
    done: dict = {}

    while pending:
        for build_id in list(pending):
            status = None
            try:
                status = client.get_image_build_status(build_id)
            except Exception as e:  # noqa: BLE001 - transient service/network hiccup
                # As in campaign_wait: not fatal. One dropped read must not end a wait on
                # a build that is still going.
                logger.debug("build status poll for %s failed: %s", build_id, e)
            if status is None:
                continue
            if status.phase != last_report.get(build_id):
                say(f"{build_id}: {status.phase}")
                last_report[build_id] = status.phase
            if status.done:
                done[build_id] = status
                pending.remove(build_id)

        if not pending:
            break
        if deadline is not None and time.monotonic() > deadline:
            raise TimeoutError(
                f"image builds {', '.join(pending)} did not finish within {timeout}s")
        time.sleep(interval)
    return done
