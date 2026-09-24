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

"""The free space RoboVAST keeps on the disk its campaigns land on.

A campaign, a postprocessing run and an import each write gigabytes, and none of them can say
beforehand how many. Writing past what a disk can spare does not fail only that operation: on a
cluster it drives the node past the kubelet's hard eviction threshold, and every pod there --
the service included -- is evicted, taking every campaign it drives with it. So a reserve is
kept, and honoured two ways:

* **New work is refused** while the disk is below it: the service will not start a campaign,
  a re-run, an image build, an import or a postprocessing run
  (:mod:`robovast.service.storage_reserve`, judged on its meters).
* **Accepted work stops starting Jobs**: the cluster's admission queue creates nothing while
  the disk is below it (``node_admission.AdmissionController``'s space gate, measured with
  :func:`disk_shortfall`). Jobs already running go on and deliver -- the reserve is the room
  their results land in -- and admission resumes by itself once space is freed.

Unset, the reserve is a fraction of the disk being written to, chosen to sit above the kubelet's
default hard eviction threshold (``nodefs.available<10%``) with room for what running Jobs still
deliver. It is a fraction because that threshold is one: an absolute default would be below it
on a large disk and refuse everything on a small one. An operator who knows their disk states an
absolute amount instead, or ``0`` for none.

Here rather than in the service because the execution backend measures the same disk, and it
must not reach up into the service.
"""

import math
import os
from pathlib import Path
from typing import Optional

#: Where the reserve is configured: a number of gigabytes (10^9 bytes), ``0`` for none.
#: Set in the operator's ``.env``; ``vast cluster setup`` and ``vast service upgrade`` carry it
#: into the service Deployment.
RESERVE_ENV = "ROBOVAST_DISK_RESERVE_GB"

#: The reserve while :data:`RESERVE_ENV` is unset, as a fraction of the disk's capacity. Above
#: the kubelet's default hard eviction threshold of 10%, by a margin for what running Jobs still
#: deliver once admission stops.
DEFAULT_RESERVE_FRACTION = 0.15

_GB = 1000 ** 3


def configured_reserve_gb() -> Optional[float]:
    """The reserve an operator stated, in gigabytes, or ``None`` while it is unset.

    Raises, naming the variable, on a value that is not a non-negative number -- a reserve that
    silently fell back to the default would not be the one its operator meant.
    """
    raw = os.environ.get(RESERVE_ENV, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{RESERVE_ENV} must be a number of gigabytes to keep free "
                         f"(for example 150), or 0 to keep none; it is {raw!r}")
    return value


def reserve_disabled() -> bool:
    """Whether an operator switched the reserve off (``0``)."""
    return configured_reserve_gb() == 0


def reserve_bytes(capacity_bytes: int) -> int:
    """The free space to keep on a disk of *capacity_bytes*."""
    configured = configured_reserve_gb()
    if configured is None:
        return int(capacity_bytes * DEFAULT_RESERVE_FRACTION)
    return int(configured * _GB)


def shortfall(label: str, free_bytes: int, capacity_bytes: int) -> Optional[str]:
    """*label* has less free than the reserve: the sentence that says so, else ``None``."""
    need = reserve_bytes(capacity_bytes)
    if need <= 0 or free_bytes >= need:
        return None
    if configured_reserve_gb() is None:
        how = (f"{DEFAULT_RESERVE_FRACTION:.0%} of that disk; set {RESERVE_ENV} to "
               "change it")
    else:
        how = RESERVE_ENV
    return (f"{label} has {free_bytes / _GB:.0f} GB free, below the {need / _GB:.0f} GB "
            f"reserve ({how}).")


def disk_shortfall(path, label: str = "the service's disk") -> Optional[str]:
    """The sentence saying the filesystem holding *path* is below the reserve, else ``None``.

    Measured at the nearest existing ancestor, so a directory a campaign has not created yet
    is judged by the disk it will be created on. Capacity is ``used + free``, as on the meters:
    blocks a filesystem holds back for root are not room a write can use.
    """
    import psutil  # pylint: disable=import-outside-toplevel

    target = Path(path)
    while not target.exists() and target != target.parent:
        target = target.parent
    usage = psutil.disk_usage(str(target))
    return shortfall(label, usage.free, usage.used + usage.free)
