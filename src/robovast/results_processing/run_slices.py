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

"""Turning a JOB's artifacts into per-RUN tables: the traversal every such step shares.

Some of what a run produced is not written by the run. Container logs, ``/rosout``, the
clock map and the resource monitor's samples are written per **job**, under
``_jobs/[<batch>/]job-N/`` — one job can serve several runs. Meanwhile
``generate_data_db`` only globs run directories, so the way such an artifact becomes a
table is always the same: read it once per job, cut it to each run, and write the result
into the run directory where the glob already looks.

This module is that shared middle. It answers, for every run of a campaign: which job
holds its artifacts, what clock maps its wall stamps to sim time, and **which stretch of
the job's timeline is its own**.

Two different questions about "its own", and the difference matters:

* ``start_epoch``/``end_epoch`` — the run's TRIAL window, from its ``test.xml``. Inside it
  the run was executing its scenario.
* ``claim_start``/``claim_end`` — the run's share of the job's whole timeline, a partition:
  consecutive, non-overlapping, together covering everything from before the first run to
  after the last. Bring-up, the reset between two runs, and teardown all fall to exactly
  one run.

A log wants the first: a line printed while another run was executing is still evidence
about this one, so ``run_log`` gives every run all of the job's lines and flags them. A
*measurement* wants the second: another run's CPU sample is not this run's, and copying it
into all N runs of a packed job makes every aggregate report N times the truth — plausibly,
and without an error anywhere. A consumer picks the one its data deserves.
"""

from __future__ import annotations

import csv
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

from robovast.common.log_tail import MAIN_CONTAINER

from . import clock_map

#: ``system.log`` / ``resource_usage_main.csv`` — the main container's artifacts are named
#: for their role, not for the container, and the two producers disagree about the word
#: (``main`` vs nothing). Both resolve to :data:`MAIN_CONTAINER` so that every derived table
#: names the container the same way and can be joined on it.
_MAIN_ARTIFACTS = ("system.log", "resource_usage_main.csv")

#: ``system_<container>.log`` and ``resource_usage_<container>.csv``.
_SIDECAR_ARTIFACT_RE = re.compile(
    r"^(?:system_(?P<log>.+)\.log|resource_usage_(?P<csv>.+)\.csv)$")


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
    return m.group("log") or m.group("csv") or None


def write_csv(path: str, fieldnames: Sequence[str], rows: Sequence[dict]) -> None:
    """Write a derived per-run table.

    The header is written even for zero rows, so a run that produced nothing still has a
    file: the reading surfaces can then say "no data" instead of "no table", which are very
    different answers to "what did this run do".
    """
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


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
    #: No ``job_links`` entry — the run's job artifacts are unlocatable.
    without_job: List[str] = field(default_factory=list)
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
    from robovast.common.campaign_data import \
        read_test_result  # pylint: disable=import-outside-toplevel
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


def iter_run_slices(campaign_path: Path, stats: SliceStats) -> Iterator[RunSlice]:
    """Every run of a campaign, with its job, its clock and its share of the timeline.

    Job resolution goes through ``_transient/job_links.yaml`` and never through the ``job``
    symlink: the symlink is only created once a job has finished, and cannot exist in an
    object store at all — which is precisely the cluster case this has to work in.

    The clock map is loaded once per job and reused, with a per-RUN fallback for a non-ROS
    run that recorded its own map beside its output.
    """
    from robovast.common.campaign_data import (  # pylint: disable=import-outside-toplevel
        list_config_dirs, list_run_dirs)
    from robovast.common.execution import \
        job_artifact_dir  # pylint: disable=import-outside-toplevel

    # Two passes: the claim intervals are a property of a job's whole run set, so every run
    # has to be placed before any of them can be yielded.
    located: List[Tuple[str, Path, str, Optional[float], Optional[float]]] = []
    by_job: Dict[str, List[Tuple[str, Optional[float]]]] = {}
    for config_dir in list_config_dirs(campaign_path):
        for run_dir in list_run_dirs(config_dir):
            job_name = f"{config_dir.name}/{run_dir.name}"
            try:
                job_dir = job_artifact_dir(str(campaign_path), job_name)
            except FileNotFoundError:
                # Unlocatable artifacts, reported as such rather than silently as a run
                # that produced nothing -- they are not the same finding.
                stats.without_job.append(f"{job_name} (no job_links entry)")
                continue
            start_epoch, end_epoch = _read_window(run_dir)
            located.append((config_dir.name, run_dir, job_dir, start_epoch, end_epoch))
            by_job.setdefault(job_dir, []).append((job_name, end_epoch))

    claims: Dict[str, Tuple[float, float]] = {}
    for windows in by_job.values():
        claims.update(_claims_for_job(windows))

    clocks: Dict[str, clock_map.ClockMap] = {}
    for config_name, run_dir, job_dir, start_epoch, end_epoch in located:
        job_name = f"{config_name}/{run_dir.name}"
        if job_dir not in clocks:
            clocks[job_dir] = clock_map.load_clock_map(
                os.path.join(job_dir, "logs", clock_map.FILENAME))
        clock = clocks[job_dir]
        if not clock:
            # rst writes its map beside the RUN's own recording, and in a packed job each
            # run has one of its own -- so this is looked up per run, not per job.
            clock = clock_map.find_run_clock_map(str(run_dir))
        if not clock:
            stats.without_clock.append(job_name)
            clock = clock_map.NO_CLOCK_MAP

        claim_start, claim_end = claims[job_name]
        if math.isnan(claim_start):
            stats.unplaceable.append(job_name)

        yield RunSlice(config_name=config_name, run_dir=run_dir, job_dir=job_dir,
                       clock=clock, start_epoch=start_epoch, end_epoch=end_epoch,
                       claim_start=claim_start, claim_end=claim_end)
