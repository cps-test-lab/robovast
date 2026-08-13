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

What remains is the ``--wait-and-download`` outcome mapping. The poll loop behind it
moved to :mod:`robovast.execution.campaign_wait`, since nothing about waiting on the
service's status contract is cluster-specific and every other surface needs it too.
"""

import logging

from robovast.execution.control_server import Phase

logger = logging.getLogger(__name__)


def wait_for_cluster_campaign(campaign_id, *, service_url="", interval=5.0,
                              timeout=None, feedback=None, client=None):
    """Block until a cluster campaign is over; report ``"succeeded"``/``"failed"``.

    The waiting itself is :func:`~robovast.execution.campaign_wait.wait_for_campaign_status`
    — nothing about polling the service's status is cluster-specific, and a second copy
    here is how the terminal test drifted across surfaces before. What stays is this
    entry point's own contract: the two-value outcome ``--wait-and-download`` branches
    on, and echoing the failure reason to the operator watching the command.
    """
    from robovast.execution.campaign_wait import wait_for_campaign_status

    say = feedback or (lambda _msg: None)
    status = wait_for_campaign_status(
        campaign_id, client=client, service_url=service_url, interval=interval,
        timeout=timeout, feedback=feedback)
    if status.phase == Phase.FAILED and status.error:
        say(f"{campaign_id}: {status.error}")
    return "succeeded" if status.phase == Phase.FINISHED else "failed"
