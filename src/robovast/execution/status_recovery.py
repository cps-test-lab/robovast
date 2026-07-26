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

"""The one way to recover a campaign's :class:`Status` from disk.

While a controller drives a campaign, its live :class:`Status` lives in the
in-memory :class:`~robovast.execution.control_server.ControllerState`. Once no
process is driving it (a past campaign, or one lost to a service restart), the
status has to be reconstructed from what is on disk. This module is the *single*
implementation of that reconstruction — it was previously duplicated (with
subtly different results) in ``service/client.py`` and the ``campaign_control``
MCP plugin.

Precedence, loud and fixed:

1. ``_execution/outcome.json`` — the durable terminal record the controller
   writes on any terminal exit (finished / failed / stopped / crashed). This is
   the canonical status journal and always wins when present.
2. Otherwise, derive an optimistic ``finished`` from the on-disk result
   artifacts (how many runs produced a ``test.xml``).
3. A campaign directory that does not exist is ``unknown`` — genuinely
   unrecoverable, reported as such rather than guessed.

This module depends only *downward* on ``robovast.common`` (the outcome/data
readers and the results store); nothing here reaches back up into ``service`` or
``mcp_server``.
"""

from pathlib import Path
from typing import Optional

from robovast.common.campaign_data import (get_vast_configuration_info,
                                           read_execution_outcome)
from robovast.common.store import read_campaign_mode
from robovast.execution.control_server import Phase, Status


def reconstruct_status_from_disk(campaign_dir: str | Path,
                                 *, expected_total: Optional[int] = None) -> Status:
    """Recover a campaign's :class:`Status` from its on-disk artifacts.

    Args:
        campaign_dir: The ``campaign-<id>`` directory.
        expected_total: The number of runs the campaign was expected to produce,
            when a caller knows it (e.g. the MCP registry entry). Used only for the
            derived-from-artifacts case to report ``runs.total``; the durable
            ``outcome.json`` carries its own totals and ignores this.

    Returns:
        The durable ``outcome.json`` Status when present; otherwise a derived
        ``finished`` Status counting the runs that produced results; otherwise an
        ``unknown`` Status for a missing directory.
    """
    campaign_dir = Path(campaign_dir)
    campaign_id = campaign_dir.name
    if not campaign_dir.is_dir():
        return Status(phase=Phase.UNKNOWN, campaign_id=campaign_id)

    # ``postprocessed`` is a fact about the campaign, not about who last drove it:
    # the built ``data.db`` is the ground truth (postprocessing can chain *after*
    # ``outcome.json`` is written, so the durable record can say False while the
    # derived data is present). Recover it here so every disk-recovered Status
    # reports it consistently — the single recovery path stays authoritative.
    postprocessed = (campaign_dir / "_execution" / "data.db").is_file()

    # The durable terminal record wins — prefer it over reconstructing an
    # optimistic "finished" (it also carries the real phase: failed / stopped).
    outcome = read_execution_outcome(campaign_dir)
    if outcome is not None:
        outcome.postprocessed = outcome.postprocessed or postprocessed
        return outcome

    try:
        info = get_vast_configuration_info(campaign_dir)
        total = info.get("num_runs", 0)
    except (FileNotFoundError, OSError, ValueError, TypeError):
        total = 0
    return Status(phase=Phase.FINISHED, campaign_id=campaign_id,
                  mode=read_campaign_mode(campaign_dir),
                  postprocessed=postprocessed,
                  runs={"completed": total, "total": expected_total or total})
