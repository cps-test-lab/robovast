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
``_jobs/[<batch>/]job-N/``. What this module adds is **when the run was executing its
scenario**:

* ``start_epoch``/``end_epoch`` — the run's TRIAL window, from its ``test.xml``. Inside it the
  run was executing its scenario; outside it, the job was bringing the containers up, writing
  the verdict or shutting down.

A job of one run: all of its records are that run's, kept whole and marked ``in_window``, so a
query can tell the trial from the bring-up around it. A job that held several runs one after
another (``execution.runs_per_job``): each run takes the rows from its window's start up to the
next run's start (:func:`claims`), so its verdict, logged just after its window, and the reset
after it are its own, marked outside the window; the job's last run keeps the job's teardown,
and the bring-up before the first run is no run's. A run with no readable window, one the
job's process never reached, takes nothing. The cut is on the runs' own ``test.xml`` stamps,
written by the process that wrote the rows, on one clock.

And which clock maps the run's wall stamps to sim time.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from . import clock_map
from .junit import read_test_result, trial_window
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
    #: The job held other runs too: a row is this run's from its window's start up to
    #: :attr:`claim_end_epoch`, and the rest are its neighbours' or the job's own and are left
    #: out rather than marked.
    shared: bool = False
    #: Where a shared run's rows end: the start of the run that followed it in the job, or
    #: ``None`` for the job's last run. Past the window rather than at its end, because a
    #: run's verdict and teardown are logged after its window closes.
    claim_end_epoch: Optional[float] = None

    @property
    def run_id(self) -> int:
        return int(self.run_dir.name)

    @property
    def job_name(self) -> str:
        return f"{self.config_name}/{self.run_dir.name}"

    def in_window(self, wall: Optional[float]) -> int:
        return in_window(wall, self.start_epoch, self.end_epoch)

    def keeps(self, wall: Optional[float]) -> bool:
        """Whether a row stamped *wall* is this run's: every row of a job of one run; of a
        run that shared its job, the rows from its window's start up to the next run's."""
        return not self.shared or claims(wall, self.start_epoch, self.claim_end_epoch)


def claims(wall: Optional[float], start_epoch: Optional[float],
           claim_end_epoch: Optional[float]) -> bool:
    """Whether a run that shared its job owns a row stamped *wall*: from the start of its
    window up to the start of the run after it. Unlike :func:`in_window`, unknown is not its
    own: a row with no stamp, or any row of a run with no readable window (one the job's
    process never reached), cannot be told from a neighbour's, so the run claims none."""
    if wall is None or start_epoch is None:
        return False
    return wall >= start_epoch and (claim_end_epoch is None or wall < claim_end_epoch)


def read_window(run_dir) -> Tuple[Optional[float], Optional[float]]:
    """The run's trial window from its ``test.xml``, or ``(None, None)``.

    A run killed mid-flight never wrote ``test.xml``. That is not an error here: it has no
    window, and the consumer decides what to do with a run whose extent is unknown.
    """
    try:
        result = read_test_result(run_dir)
    except (FileNotFoundError, ValueError, OSError):
        return None, None
    return trial_window(result)


def run_slice(job_dir: str, config_name: str, run_dir, clock: clock_map.ClockMap,
              stats: SliceStats, shared: bool = False,
              claim_end_epoch: Optional[float] = None) -> RunSlice:
    """One run of a job, with its clock and its trial window.

    *clock* is the run's map, from its ``clock_map`` rows: the job's wall-time recording's
    ``/clock``, or its simulator's own recording's where that is what gave them. *shared*
    says the job held other runs as well, and *claim_end_epoch* where the next of them
    started.
    """
    run_dir = Path(run_dir)
    start_epoch, end_epoch = read_window(run_dir)
    if not clock:
        stats.without_clock.append(f"{config_name}/{run_dir.name}")
        clock = clock_map.NO_CLOCK_MAP
    return RunSlice(config_name=config_name, run_dir=run_dir, job_dir=job_dir,
                    clock=clock, start_epoch=start_epoch, end_epoch=end_epoch, shared=shared,
                    claim_end_epoch=claim_end_epoch)
