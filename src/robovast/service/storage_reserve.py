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

"""The free space this service keeps, and the sentence it refuses new work with.

A campaign, an image build, an import and a postprocessing run each write gigabytes, and none
of them can say beforehand how many. Starting one on a disk that is nearly full does not fail
only that operation: on a cluster it drives the node past the kubelet's hard eviction
threshold, and every pod there -- this service included -- is evicted. So the service keeps a
reserve and refuses *new* disk-consuming work below it, while work already running continues.

The reserve is an absolute amount rather than a fraction because what it has to stay clear of
is a fixed amount on one particular disk: the operator knows the disk and its eviction
threshold, and states the margin in the unit they measure it in.

It is judged on the readings :class:`~robovast.service.interface.ResourceUsage` already
carries (``disk`` and ``store``), so the refusal, the web UI's meter and the MCP tool are one
measurement and cannot disagree about what is free. A meter the backend could not read is not
a full disk: ``disk_unavailable`` already says why there is no reading.
"""

import math
import os
from typing import Optional

#: Where the reserve is configured: a number of gigabytes (10^9 bytes), ``0`` for none.
#: Set in the operator's ``.env``; ``vast cluster setup`` and ``vast service upgrade`` carry it
#: into the service Deployment.
RESERVE_ENV = "ROBOVAST_DISK_RESERVE_GB"

#: What an unset reserve means: none. The margin that matters is the one above a particular
#: disk's eviction threshold, which only its operator knows -- and any fixed default would
#: refuse every campaign on a disk smaller than it, a laptop's or a CI runner's, for a threat
#: that exists only where a kubelet evicts.
DEFAULT_RESERVE_GB = 0.0

_GB = 1000 ** 3


def reserve_gb() -> float:
    """The configured reserve, in gigabytes. Raises, naming the variable, on a value that is
    not a non-negative number -- a reserve that silently fell back to none would leave
    unprotected the disk its operator meant to protect."""
    raw = os.environ.get(RESERVE_ENV, "").strip()
    if not raw:
        return DEFAULT_RESERVE_GB
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{RESERVE_ENV} must be a number of gigabytes to keep free "
                         f"(for example 150), or 0 to keep none; it is {raw!r}")
    return value


def storage_refusal(usage) -> Optional[str]:
    """Why new disk-consuming work is refused on *usage*, or ``None`` while there is room.

    *usage* is a :class:`~robovast.service.interface.ResourceUsage`. The sentence names the
    meter and the amounts, and never a node or a path: it crosses the interface.
    """
    reserve = reserve_gb()
    if reserve <= 0:
        return None
    for label, space in (("the service's disk", usage.disk), ("the results store", usage.store)):
        if space is None or space.capacity_bytes <= 0:
            continue
        free = max(0, space.capacity_bytes - space.used_bytes)
        if free < reserve * _GB:
            return (f"New work that writes to disk is refused: {label} has "
                    f"{free / _GB:.0f} GB free, less than the {reserve:g} GB this service "
                    f"keeps free ({RESERVE_ENV}). Work already running continues.")
    return None
