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

"""A run and the job that ran it: what cuts the job's artifacts to the run's trial.

Some of what a run produced is not written by the run. Container logs, ``/rosout``, the
clock map and the resource monitor's samples are written per **job**, under
``_jobs/[<batch>/]job-N/``. A job runs exactly one run, so all of it is that run's; what this
module adds is **when the run was executing its scenario**:

* ``start_epoch``/``end_epoch`` — the run's TRIAL window, from its ``test.xml``. Inside it the
  run was executing its scenario; outside it, the job was bringing the containers up, writing
  the verdict or shutting down. Rows are kept either way and marked ``in_window``.

And which clock maps the run's wall stamps to sim time.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from . import clock_map
from .junit import read_test_result
from .layout import MAIN_CONTAINER

#: ``system.log`` / ``resource_usage_main.csv`` / ``system_usage_main.csv`` — the main
#: container's artifacts are named for their role, not for the container, and the producers
#: disagree about the word (``main`` vs nothing). All resolve to :data:`MAIN_CONTAINER` so that
#: every derived table names the container the same way and can be joined on it.
_MAIN_ARTIFACTS = ("system.log", "resource_usage_main.csv", "system_usage_main.csv")

#: ``system_<container>.log``, ``resource_usage_<container>.csv`` and its per-container sibling
#: ``system_usage_<container>.csv``. The two CSVs are alternatives rather than one pattern with
#: an optional prefix: ``system_usage_`` has to be tried before the bare ``system_`` log branch
#: would ever see it, and spelling them out is what keeps that ordering visible.
_SIDECAR_ARTIFACT_RE = re.compile(
    r"^(?:system_usage_(?P<sys>.+)\.csv"
    r"|resource_usage_(?P<csv>.+)\.csv"
    r"|system_(?P<log>.+)\.log)$")


def container_of(filename: str) -> Optional[str]:
    """The container a job artifact belongs to, or ``None`` if it is not one.

    One function for every per-container artifact, deliberately. The main container has
    three names in this system — ``scenario`` in the config, ``robovast`` as the compose
    service, ``main`` in the monitor's filename — and each producer that maps its own
    filenames is a chance for two derived tables to disagree about what to call the same
    container. They are joined on that string, so a disagreement does not raise; it
    silently returns nothing.
    """
    base = os.path.basename(filename)
    if base in _MAIN_ARTIFACTS:
        return MAIN_CONTAINER
    m = _SIDECAR_ARTIFACT_RE.match(base)
    if not m:
        return None
    return m.group("sys") or m.group("csv") or m.group("log") or None


def in_window(wall: Optional[float], start_epoch: Optional[float],
              end_epoch: Optional[float]) -> int:
    """1 when *wall* is inside the run's trial window, 0 outside it.

    Unknown counts as inside: a row with no stamp, or a run whose window could not be
    read, is not evidence that it happened outside the trial.
    """
    if wall is None or start_epoch is None:
        return 1
    if wall < start_epoch:
        return 0
    if end_epoch is not None and wall > end_epoch:
        return 0
    return 1


@dataclass
class SliceStats:
    """Runs the traversal could not fully serve, for the caller's summary message.

    Every one of these is a real degradation that a reader would otherwise meet as an empty
    column with no explanation, so they are collected rather than logged and dropped.
    """
    #: No clock map — derived sim times will be empty for this run.
    without_clock: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class RunSlice:
    """One run, and everything needed to place its job's artifacts on its trial."""
    config_name: str
    run_dir: Path
    job_dir: str
    clock: clock_map.ClockMap
    #: The trial window from ``test.xml``; ``None`` when it could not be read.
    start_epoch: Optional[float]
    end_epoch: Optional[float]

    @property
    def run_id(self) -> int:
        return int(self.run_dir.name)

    @property
    def job_name(self) -> str:
        return f"{self.config_name}/{self.run_dir.name}"

    def in_window(self, wall: Optional[float]) -> int:
        return in_window(wall, self.start_epoch, self.end_epoch)


def describe_missing(label: str, items: Sequence[str], noun: str = "run(s)",
                     limit: int = 5) -> str:
    """``"; <label> N <noun>: a, b, c (+2 more)"`` — or ``""`` when there are none.

    Truncated because these lists are unbounded (a broken campaign degrades in every run),
    and a message that grows with the campaign stops being read at all.
    """
    if not items:
        return ""
    shown = ", ".join(items[:limit])
    more = f" (+{len(items) - limit} more)" if len(items) > limit else ""
    return f"; {label} {len(items)} {noun}: {shown}{more}"


def _read_window(run_dir: Path) -> Tuple[Optional[float], Optional[float]]:
    """The run's trial window from its ``test.xml``, or ``(None, None)``.

    A run killed mid-flight never wrote ``test.xml``. That is not an error here: it has no
    window, and the consumer decides what to do with a run whose extent is unknown.
    """
    try:
        result = read_test_result(run_dir)
    except (FileNotFoundError, ValueError, OSError):
        return None, None
    start = result.get("start_epoch")
    if start is None:
        return None, None
    return start, start + (result.get("duration_sec") or 0.0)


def run_slice(job_dir: str, config_name: str, run_dir, clock: clock_map.ClockMap,
              stats: SliceStats) -> RunSlice:
    """The run of one job, with its clock and its trial window.

    *clock* is the job's map; a run whose simulator recorded its own map beside its output
    uses that instead.
    """
    run_dir = Path(run_dir)
    start_epoch, end_epoch = _read_window(run_dir)
    run_clock = clock if clock else clock_map.find_run_clock_map(str(run_dir))
    if not run_clock:
        stats.without_clock.append(f"{config_name}/{run_dir.name}")
        run_clock = clock_map.NO_CLOCK_MAP
    return RunSlice(config_name=config_name, run_dir=run_dir, job_dir=job_dir,
                    clock=run_clock, start_epoch=start_epoch, end_epoch=end_epoch)
