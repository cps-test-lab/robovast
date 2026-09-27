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


def _existing(path) -> Path:
    """*path*'s nearest existing ancestor, so a directory not created yet is judged by the
    disk it will be created on."""
    target = Path(path)
    while not target.exists() and target != target.parent:
        target = target.parent
    return target


def _usage(path):
    import psutil  # pylint: disable=import-outside-toplevel

    return psutil.disk_usage(str(_existing(path)))


def block_bytes(path) -> int:
    """The allocation unit of the filesystem *path* is or will be created on."""
    return os.statvfs(_existing(path)).f_frsize


def room_bytes(path) -> int:
    """Bytes that can be written under *path* before its filesystem falls below the reserve."""
    usage = _usage(path)
    return max(0, usage.free - reserve_bytes(usage.used + usage.free))


def unpacked_bytes(members, block: int) -> int:
    """What extracting the tar *members* takes on a filesystem allocating *block* bytes at a
    time: each member at least one block, a file its size rounded up to whole blocks."""
    return block * sum(max(1, -(-(m.size if m.isfile() else 0) // block)) for m in members)


def refuse_unless_room(archive_name: str, members, dest, volume: str, remedy: str) -> None:
    """Raise :class:`~robovast.common.errors.InsufficientStorageError` (507) when the tar
    *members* would take more than *dest*'s filesystem has room for above the reserve.

    Asked of the tar's index before extraction: the compressed size bounds nothing, since a
    crafted archive packs a terabyte of zeros, or a million empty entries, into kilobytes.
    *volume* names the filesystem in the refusal and *remedy* says what frees it.
    """
    from robovast.common.errors import \
        InsufficientStorageError  # pylint: disable=import-outside-toplevel

    unpacked = unpacked_bytes(members, block_bytes(dest))
    room = room_bytes(dest)
    if unpacked > room:
        raise InsufficientStorageError(
            f"{archive_name} unpacks to {unpacked / _GB:.1f} GB, and {volume} has "
            f"{room / _GB:.1f} GB free above its reserve. Nothing was extracted; {remedy}.")


def disk_shortfall(path, label: str = "the service's disk") -> Optional[str]:
    """The sentence saying the filesystem holding *path* is below the reserve, else ``None``.

    Capacity is ``used + free``, as on the meters: blocks a filesystem holds back for root
    are not room a write can use.
    """
    usage = _usage(path)
    return shortfall(label, usage.free, usage.used + usage.free)
