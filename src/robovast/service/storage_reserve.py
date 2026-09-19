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

"""The service's verdict on new disk-consuming work, judged on its own meters.

Below the free-space reserve (:mod:`robovast.common.disk_reserve`) the service refuses to start a
campaign, a re-run, an image build, an import or a postprocessing run. The verdict is judged on
the readings :class:`~robovast.service.interface.ResourceUsage` already carries (``disk`` and
``results``), so the refusal, the web UI's meter and the MCP tool are one measurement and cannot
disagree about what is free. A meter the backend could not read is not a full disk:
``disk_unavailable`` already says why there is no reading.
"""

from typing import Optional

from robovast.common.disk_reserve import shortfall


def storage_refusal(usage) -> Optional[str]:
    """Why new disk-consuming work is refused on *usage*, or ``None`` while there is room.

    *usage* is a :class:`~robovast.service.interface.ResourceUsage`. The sentence names the
    meter and the amounts, and never a node or a path: it crosses the interface.
    """
    for label, space in (("the service's disk", usage.disk),
                         ("the results volume", usage.results)):
        if space is None or space.capacity_bytes <= 0:
            continue
        short = shortfall(label, max(0, space.capacity_bytes - space.used_bytes),
                          space.capacity_bytes)
        if short:
            return f"New work is refused: {short}"
    return None
