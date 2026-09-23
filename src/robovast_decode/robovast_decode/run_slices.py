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

"""Turning a JOB's artifacts into per-RUN tables: the partition every such table shares.

Some of what a run produced is not written by the run. Container logs, ``/rosout``, the
clock map and the resource monitor's samples are written per **job**, under
``_jobs/[<batch>/]job-N/`` -- one job can serve several runs. Each such table is therefore
built for a whole job at once and cut to its runs (:mod:`robovast_decode.derived`).

This module is that cut. It answers, for every run of a job: what clock maps its wall stamps
to sim time, and **which stretch of the job's timeline is its own**.

Two different questions about "its own", and the difference matters:

* ``start_epoch``/``end_epoch`` — the run's TRIAL window, from its ``test.xml``. Inside it
  the run was executing its scenario.
* ``claim_start``/``claim_end`` — the run's share of the job's whole timeline, a partition:
  consecutive, non-overlapping, together covering everything from before the first run to
  after the last. Bring-up, the reset between two runs, and teardown all fall to exactly
  one run.
* ``log_claim_start``/``log_claim_end`` — the same partition, cut at the other end of the gap
  between two runs. See below.

Both a log and a measurement want a partition, not the window: a packed job may run several
*different configurations*, and another configuration's trial is a different experiment. For
a measurement, copying its CPU samples into all N runs makes every aggregate report N times
the truth — plausibly, and without an error anywhere. For a log, it gave every run the FIRST
scenario's verdict, so a run whose own trial passed reported that it had failed.

**They divide the gap between two runs differently, and that is not a detail.** A run's
``test.xml`` duration closes when its scenario stops, but the run keeps *logging* after that
— its verdict line lands milliseconds late (measured: 1.1 ms), then its shutdown. So:

* a **measurement** boundary is the earlier run's ``end_epoch``: the gap is the simulator
  being reset, which is the cost of the run *starting up* (:func:`_claims_for_job`).
* a **log** boundary is the later run's ``start_epoch``: the gap is the earlier run's own
  verdict and teardown, which belong to the run that was *finishing* (:func:`_log_claims_for_job`).

Boundaries taken from ``end_epoch`` would hand a failing run's own verdict to its successor.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

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


def claims_log(wall: Optional[float], start: float, end: float) -> bool:
    """1 when *wall* falls in ``[start, end)`` — a run's share of its job's LOG timeline.

    Unlike :func:`in_window` and :meth:`RunSlice.claims`, an unstamped record is claimed
    **only** by the run whose share opens at ``-inf``, the job's first. "Unknown counts as
    inside" is safe for a measurement, whose ticks always carry a stamp; for a log it would
    copy the record into every run of the job. The first run is where the merge's ordering
    already places it (``None`` sorts to ``-inf``), and dropping it is not an option — it is a
    third party's output, and evidence about the container either way.
    """
    if wall is None:
        return start == -math.inf
    return start <= wall < end


@dataclass
class SliceStats:
    """Runs the traversal could not fully serve, for the caller's summary message.

    Every one of these is a real degradation that a reader would otherwise meet as an empty
    column with no explanation, so they are collected rather than logged and dropped.
    """
    #: No clock map — derived sim times will be empty for this run.
    without_clock: List[str] = field(default_factory=list)
    #: A run of a PACKED job with no readable ``test.xml``: it cannot be placed on the wall
    #: clock, so it claims nothing. See :func:`iter_run_slices`.
    unplaceable: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class RunSlice:
    """One run, and everything needed to cut the job's artifacts down to it."""
    config_name: str
    run_dir: Path
    job_dir: str
    clock: clock_map.ClockMap
    #: The trial window from ``test.xml``; ``None`` when it could not be read.
    start_epoch: Optional[float]
    end_epoch: Optional[float]
    #: This run's share of the job's timeline. ``claim_start`` is ``-inf`` for a job's first
    #: run and ``claim_end`` ``+inf`` for its last, so the partition covers all of time.
    claim_start: float
    claim_end: float
    #: The same partition for LOGS, cut at the later run's ``start_epoch`` instead of the
    #: earlier one's ``end_epoch`` — see the module docstring. Equal to the measurement claim
    #: for a job with one run.
    log_claim_start: float
    log_claim_end: float

    @property
    def run_id(self) -> int:
        return int(self.run_dir.name)

    @property
    def job_name(self) -> str:
        return f"{self.config_name}/{self.run_dir.name}"

    def claims(self, wall: Optional[float]) -> bool:
        """Whether *wall* falls in this run's share of the job's timeline."""
        if wall is None:
            return True
        return self.claim_start <= wall < self.claim_end

    def claims_log(self, wall: Optional[float]) -> bool:
        """Whether *wall* falls in this run's share of the job's LOG timeline."""
        return claims_log(wall, self.log_claim_start, self.log_claim_end)

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


def _claims_for_job(windows: List[Tuple[str, Optional[float]]]) -> Dict[str, Tuple[float, float]]:
    """Partition a job's timeline between the runs that share it.

    *windows* is ``[(job_name, end_epoch)]`` for the runs of ONE job. Runs are ordered by
    when they finished, and run *i* claims ``[end(i-1), end(i))`` — so the gap between two
    runs (the simulator being reset) belongs to the one that was starting up, not to the one
    that had finished, and the partition has no holes.

    A run with no window cannot be ordered. Alone in its job it claims everything, because
    its whole trace is its own and that is the run whose trace matters most. Sharing a job,
    it claims nothing: the alternative — giving it everything — is what reintroduces the
    double count this function exists to prevent, and a table saying "no data" is honest
    where one stating another run's numbers is not.
    """
    if len(windows) == 1:
        return {windows[0][0]: (-math.inf, math.inf)}

    placeable = sorted(((end, name) for name, end in windows if end is not None))
    claims: Dict[str, Tuple[float, float]] = {
        name: (math.nan, math.nan) for name, end in windows if end is None}
    previous = -math.inf
    for index, (end, name) in enumerate(placeable):
        last = index == len(placeable) - 1
        claims[name] = (previous, math.inf if last else end)
        previous = end
    return claims


def _log_claims_for_job(starts: List[Tuple[str, Optional[float]]]) -> Dict[str, Tuple[float, float]]:
    """Partition a job's LOG timeline between the runs that share it.

    *starts* is ``[(job_name, start_epoch)]`` for the runs of ONE job. Runs are ordered by
    when they STARTED, and run *i* claims ``[start(i), start(i+1))`` — so the gap between two
    runs belongs to the one that was finishing, because what fills that gap is its verdict and
    its teardown. :func:`_claims_for_job` splits the same gap the other way for measurements;
    the module docstring says why.

    A run with no window cannot be ordered, and is treated exactly as it is there: alone in
    its job it claims everything, sharing a job it claims nothing. Giving it the whole log is
    what handed it another run's verdict.
    """
    if len(starts) == 1:
        return {starts[0][0]: (-math.inf, math.inf)}

    placeable = sorted(((start, name) for name, start in starts if start is not None))
    claims: Dict[str, Tuple[float, float]] = {
        name: (math.nan, math.nan) for name, start in starts if start is None}
    for index, (start, name) in enumerate(placeable):
        first = index == 0
        last = index == len(placeable) - 1
        claims[name] = (-math.inf if first else start,
                        math.inf if last else placeable[index + 1][0])
    return claims


def log_claims_from_markers(
        starts: List[Tuple[str, Optional[float]]],
        markers: Sequence[float]) -> Optional[Dict[str, Tuple[float, float]]]:
    """Refine a job's log partition so each run owns its own scenario-start line.

    :func:`_log_claims_for_job` can only use ``test.xml``'s ``start_time``, and that is
    **tens of microseconds LATE**: scenario-execution logs ``Executing scenario '<name>'``
    and *then* records the start, measured here at 33–44 µs. A boundary on ``start_epoch``
    therefore leaves every run's own marker line just outside it, in its predecessor's share
    — so the first run of a job held two scenario-start lines and the last held none.

    A tolerance would not fix it honestly: the nearest thing on the other side is the
    previous run's ``Shutting down finished.``, only ~11 ms earlier in a 114 ms gap, and that
    margin is a property of the machine rather than of the format.

    So the boundaries are the markers. A job's runs are serial and each logs one, so marker
    *i* opens run *i*'s share; the first run also keeps everything before its own marker (the
    container's bring-up). ``None`` when that mapping cannot be trusted — a marker missing
    (rosout is only recorded once subscribed) or a run that cannot be ordered — and the
    caller then keeps the ``start_epoch`` boundaries, which are right to those microseconds.
    """
    if any(start is None for _, start in starts):
        return None
    ordered = [name for _, name in sorted((start, name) for name, start in starts)]
    if len(markers) != len(ordered):
        return None
    bounds = sorted(markers)
    return {name: (-math.inf if index == 0 else bounds[index],
                   math.inf if index == len(ordered) - 1 else bounds[index + 1])
            for index, name in enumerate(ordered)}


def job_slices(job_dir: str, runs: Sequence[Tuple[str, Path]], clock: clock_map.ClockMap,
               stats: SliceStats) -> List[RunSlice]:
    """Every run of one job, with its clock and its share of the job's timeline.

    *runs* is ``[(config_name, run_dir)]``, every run the job served: the claim intervals are
    a property of the job's whole run set, so all of them are placed before any is cut.
    *clock* is the job's map; a run whose simulator recorded its own map beside its output
    uses that instead, looked up per run because in a packed job each run has its own.
    """
    located = []
    by_end: List[Tuple[str, Optional[float]]] = []
    by_start: List[Tuple[str, Optional[float]]] = []
    for config_name, run_dir in runs:
        run_dir = Path(run_dir)
        job_name = f"{config_name}/{run_dir.name}"
        start_epoch, end_epoch = _read_window(run_dir)
        located.append((config_name, run_dir, start_epoch, end_epoch))
        by_end.append((job_name, end_epoch))
        by_start.append((job_name, start_epoch))
    claims = _claims_for_job(by_end)
    log_claims = _log_claims_for_job(by_start)

    out = []
    for config_name, run_dir, start_epoch, end_epoch in located:
        job_name = f"{config_name}/{run_dir.name}"
        run_clock = clock if clock else clock_map.find_run_clock_map(str(run_dir))
        if not run_clock:
            stats.without_clock.append(job_name)
            run_clock = clock_map.NO_CLOCK_MAP
        claim_start, claim_end = claims[job_name]
        log_claim_start, log_claim_end = log_claims[job_name]
        if math.isnan(claim_start):
            stats.unplaceable.append(job_name)
        out.append(RunSlice(config_name=config_name, run_dir=run_dir, job_dir=job_dir,
                            clock=run_clock, start_epoch=start_epoch, end_epoch=end_epoch,
                            claim_start=claim_start, claim_end=claim_end,
                            log_claim_start=log_claim_start, log_claim_end=log_claim_end))
    return out
