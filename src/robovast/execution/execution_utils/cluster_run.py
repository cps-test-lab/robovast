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

"""Host-side helper for ``vast exec cluster run --wait-and-download``.

Launching is no longer here: cluster campaigns are started through the
``robovast-service`` (``service.project_push.run_project_via_service``), which
drives them in-process. The old ``launch_cluster_campaign`` — which built a dev
wheel, created a per-campaign controller pod and ``kubectl cp``'d the inputs in —
is gone with that pod.

What remains is the poll loop that backs ``--wait-and-download``, expressed
against the service's status contract like every other client surface.
"""

import logging

logger = logging.getLogger(__name__)

#: Phases that mean the campaign is over, one way or another.
_TERMINAL = {"finished", "failed"}


def wait_for_cluster_campaign(campaign_id, *, service_url="", interval=5.0,
                              timeout=None, feedback=None, client=None):
    """Block until a cluster campaign reaches a terminal phase.

    Polls the service's ``get_status`` — the service drives the campaign, so its
    phase *is* the campaign's. ``phase == "finished"`` is genuinely terminal now:
    delivery is the object store (the service streams the download from it), so
    unlike the old controller-pod flow there is no separate "uploaded" stage to
    wait for.

    Args:
        campaign_id: The campaign to wait for.
        service_url: The robovast-service to poll (ignored when *client* is given).
        interval: Poll interval in seconds.
        timeout: Optional overall timeout in seconds (None = wait forever).
        feedback: Optional ``str -> None`` progress sink (e.g. ``click.echo``).
        client: Optional pre-built client (used by tests).

    Returns:
        ``"succeeded"`` or ``"failed"``.

    Raises:
        TimeoutError: if *timeout* elapses first.
    """
    import time

    from robovast.service.client import RobovastClient

    say = feedback or (lambda _msg: None)
    client = client or RobovastClient(service_url)
    deadline = None if timeout is None else (time.monotonic() + timeout)
    last_report = None

    while True:
        try:
            status = client.get_status(campaign_id)
        except Exception as e:  # noqa: BLE001 - transient service/network hiccup
            logger.debug("status poll for %s failed: %s", campaign_id, e)
            status = None

        if status is not None:
            report = status.phase + (f"/{status.stage}" if status.stage else "")
            if report != last_report:
                say(f"{campaign_id}: {report}")
                last_report = report
            if status.phase in _TERMINAL:
                if status.phase == "failed" and status.error:
                    say(f"{campaign_id}: {status.error}")
                return "succeeded" if status.phase == "finished" else "failed"

        if deadline is not None and time.monotonic() > deadline:
            raise TimeoutError(
                f"Campaign {campaign_id!r} did not finish within {timeout}s "
                f"(last state: {last_report})")
        time.sleep(interval)
