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

"""The free space RoboVAST keeps on a disk it writes to, and what it does when a disk runs short.

A campaign's result download, a postprocessing run and an import each write gigabytes, and none
of them can say beforehand how many. Writing past what a disk can spare does not fail only that
operation: on a cluster it drives the node past the kubelet's hard eviction threshold, and every
pod there -- the service included -- is evicted, taking every campaign it drives with it. So a
reserve is kept, and honoured two ways:

* **Work already running pauses** before it writes into it (:func:`wait_for_room`,
  :func:`pausing`): a download stops between two files, says so, and carries on once space is
  freed.
* **A write somebody is waiting on is refused** (:func:`require_room`), because blocking a
  request until somebody frees space turns a clear refusal into a timeout. The service's
  admission of *new* work is the same refusal, judged on its meters
  (:mod:`robovast.service.storage_reserve`).

Unset, the reserve is a fraction of the disk being written to, chosen to sit above the kubelet's
default hard eviction threshold (``nodefs.available<10%``) with room for what is in flight. It is
a fraction because that threshold is one: an absolute default would be below it on a large disk
and refuse everything on a small one. An operator who knows their disk states an absolute amount
instead, or ``0`` for none.

Here rather than in the service because both sides of the service/engine boundary write to that
disk, and the engine must not reach up into the service.
"""

import logging
import math
import os
import time
from pathlib import Path
from typing import Callable, Optional

from robovast.common.errors import InsufficientStorageError

logger = logging.getLogger(__name__)

#: Where the reserve is configured: a number of gigabytes (10^9 bytes), ``0`` for none.
#: Set in the operator's ``.env``; ``vast cluster setup`` and ``vast service upgrade`` carry it
#: into the service Deployment.
RESERVE_ENV = "ROBOVAST_DISK_RESERVE_GB"

#: The reserve while :data:`RESERVE_ENV` is unset, as a fraction of the disk's capacity. Above
#: the kubelet's default hard eviction threshold of 10%, by a margin for writes already in flight
#: when a pause begins.
DEFAULT_RESERVE_FRACTION = 0.15

#: How often a paused download looks again. A person frees space in minutes, not seconds; a
#: statvfs is cheap either way.
PAUSE_POLL_SECONDS = 30.0

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


def disk_shortfall(path) -> Optional[str]:
    """The sentence saying the filesystem holding *path* is below the reserve, else ``None``.

    Measured at the nearest existing ancestor, because a download's destination is created by
    the download. Capacity is ``used + free``, as on the meters: blocks a filesystem holds back
    for root are not room a write can use.
    """
    import psutil  # pylint: disable=import-outside-toplevel

    target = Path(path)
    while not target.exists() and target != target.parent:
        target = target.parent
    usage = psutil.disk_usage(str(target))
    return shortfall("the service's disk", usage.free, usage.used + usage.free)


def require_room(path, *, action: str) -> None:
    """Refuse to *action* while the disk holding *path* is below the reserve.

    For a write a caller is waiting on: blocking a request until somebody frees space would
    turn a clear refusal into a timeout.
    """
    short = disk_shortfall(path)
    if short:
        raise InsufficientStorageError(
            f"Cannot {action}: {short} Delete campaigns no longer needed, then retry.")


def wait_for_room(path, *, action: str,
                  on_pause: Optional[Callable[[Optional[str]], None]] = None,
                  should_stop: Optional[Callable[[], bool]] = None,
                  sleep: Optional[Callable[[float], None]] = None) -> None:
    """Block while the disk holding *path* is below the reserve, then return.

    For work already running, whose next write would otherwise land in the reserve. *on_pause*
    is told the sentence when the pause begins and ``None`` when it ends, so a caller can put it
    on the status a person reads; the log gets both either way.

    Raises :class:`~robovast.common.errors.InsufficientStorageError` when *should_stop* turns
    true during the pause: a stopped campaign must not sit waiting for space it no longer needs.
    *sleep* is ``time.sleep``, looked up when the pause begins.
    """
    short = disk_shortfall(path)
    if not short:
        return
    message = f"{action} paused: {short} It continues once space is freed."
    logger.warning(message)
    if on_pause is not None:
        on_pause(message)
    sleep = sleep or time.sleep
    started = time.monotonic()
    while short:
        if should_stop is not None and should_stop():
            raise InsufficientStorageError(f"{action} stopped while paused: {short}")
        sleep(PAUSE_POLL_SECONDS)
        short = disk_shortfall(path)
    logger.info("%s resumed after %.0f s: the disk is above the reserve again.",
                action, time.monotonic() - started)
    if on_pause is not None:
        on_pause(None)


def pausing(path, *, action: str, on_file: Optional[Callable[[], None]] = None,
            **wait_kwargs) -> Callable[[], None]:
    """An ``on_file`` callback for ``download_prefix`` that pauses between files.

    ``download_prefix`` calls it once per file fetched, so a download that reaches the reserve
    stops before its next file: what lands in the reserve is at most the one file in flight.
    *on_file*, when given, still runs first. Call :func:`wait_for_room` once before the download
    starts, so a disk already short does not take even that one.
    """
    def callback():
        if on_file is not None:
            on_file()
        wait_for_room(path, action=action, **wait_kwargs)
    return callback
