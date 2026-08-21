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

"""What a run cost: the resource monitor's samples, per container, on the run's clock.

Every container runs ``monitor_resources.py``, which writes one row per process per second
to ``_jobs/[<batch>/]job-N/resource_usage_<container>.csv`` — a JOB artifact, so it spans
bring-up, every run the job served, and teardown. This module cuts it to a run.

Why this is worth a table rather than a file: a campaign's lane gives a job a fixed number
of cores, and a simulator that starves the stack changes what the stack does. That is a
competing explanation for any behavioral difference, and it can only be ruled in or out in
the same query as the behaviour — joined to ``runs`` for ``available_cpus``, to ``poses``
for what the robot did at the time.

Two decisions here that a reader will otherwise want to "fix" back:

**Rows are grouped by process NAME, not by pid.** Pids churn: a node restarted by a
respawn is a new pid and the same program. Nothing joins to a pid and no pid is comparable
across runs, so the name is the key and ``num_pids`` records how many shared it.

**Each tick belongs to exactly one run** (see :mod:`run_slices` for the partition). Unlike
``run_log``, which hands every run of a packed job all of the job's lines, a sample outside
this run's claim is another run's CPU. Copying it would make ``SUM`` over a job's runs
report several times what the job consumed, with nothing anywhere reporting an error.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field, fields
from typing import Dict, List, Optional, Sequence, Tuple

from . import run_slices

#: Written into each run directory, one row per container per process name per tick.
FILENAME = "resource_usage.csv"

#: The table's columns, in reading order: when, where from, what.
FIELDNAMES = [
    "timestamp", "wall_ts", "in_window",
    "container", "name",
    "cpu_percent", "memory_rss_bytes", "num_pids",
]

#: What ``monitor_resources.py`` writes. Checked against the file's actual header, because
#: a reader that assumes column order turns a changed writer into wrong numbers instead of
#: an error.
RAW_FIELDNAMES = ("timestamp", "pid", "name", "cpu_percent", "memory_rss_bytes")

_CSV_PREFIX = "resource_usage_"


@dataclass
class ScanStats:
    """What the scan read and what was wrong with it.

    The damage counters are not diagnostics for us; they are the reason a reader can trust
    the numbers. A silently short file and a healthy one produce the same shaped table.
    """
    files: int = 0
    samples: int = 0
    ticks: int = 0
    rows: int = 0
    short_rows: int = 0
    bad_rows: int = 0
    dropped_tail_ticks: int = 0
    containers: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    empty: List[str] = field(default_factory=list)
    truncated: List[str] = field(default_factory=list)
    unexpected: List[str] = field(default_factory=list)
    unreadable: List[str] = field(default_factory=list)

    #: Counted per RUN by the caller: one job's ticks are split between the runs it served,
    #: so the campaign total is a sum over runs and must not also take the job's.
    _PER_RUN_FIELDS = ("rows",)

    def add_job(self, other: "ScanStats") -> None:
        """Fold one job's stats into these campaign totals.

        Driven by the dataclass fields rather than a written-out list, so a counter added to
        the class later cannot be silently dropped from the summary -- the same guard, and
        for the same reason, as ``run_log.MergeStats.add_job``.
        """
        for spec in fields(self):
            if spec.name in self._PER_RUN_FIELDS:
                continue
            mine, theirs = getattr(self, spec.name), getattr(other, spec.name)
            if isinstance(mine, list):
                for item in theirs:
                    if item not in mine:
                        mine.append(item)
            else:
                setattr(self, spec.name, mine + theirs)

    def summary(self) -> str:
        return (f"{self.rows} rows across {len(self.containers)} container(s) "
                f"({self.samples} samples in {self.ticks} tick(s) from {self.files} file(s))")


@dataclass(frozen=True)
class Tick:
    """One container's processes at one sampling instant, grouped by process name."""
    wall_ts: float
    container: str
    #: ``{process name: (cpu_percent, memory_rss_bytes, num_pids)}``
    processes: Dict[str, Tuple[float, int, int]]


def _as_float(value: Optional[str]) -> Optional[float]:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def read_container_csv(path: str, container: str, stats: ScanStats,
                       label: str = "") -> List[Tuple[float, str, float, int]]:
    """One container's samples as ``[(wall_ts, name, cpu_percent, memory_rss_bytes)]``.

    Written to survive the cluster's damage mode. Every container of a job mirrors the same
    shared ``/out``, and the main container uploads while a sidecar is still appending; if
    that sidecar never reaches its own upload, the surviving object is the main container's
    early snapshot — a valid CSV that stops mid-row.

    So the last tick of a damaged file is dropped **whole**. A tick cut after three of nine
    processes is the dangerous case: it is not a parse error, it is a row reporting a third
    of the container's CPU, indistinguishable from a real dip. A clean file's last tick is
    kept, because process count genuinely falls during teardown and a "looks short" heuristic
    would delete real shutdown data from every healthy run.
    """
    where = f"{label}:{container}" if label else container
    try:
        handle = open(path, newline="", encoding="utf-8", errors="replace")
    except OSError as e:
        stats.unreadable.append(f"{where} ({e.strerror or e})")
        return []
    with handle:
        return parse_container_rows(handle, container, stats, label)


def parse_container_rows(lines, container: str, stats: ScanStats,
                         label: str = "") -> List[Tuple[float, str, float, int]]:
    """:func:`read_container_csv` on any line source — a file, or a live tail.

    Split out so that reading a **running** job's CSV, which arrives over an exec rather than as
    a path, shares one definition of the column contract and of the damaged-tail rule. Two
    parsers for one writer is how a changed column becomes wrong numbers in one place and an
    error in the other.
    """
    samples: List[Tuple[float, str, float, int]] = []
    bad_here = 0
    where = f"{label}:{container}" if label else container
    reader = csv.DictReader(lines)
    if not reader.fieldnames or set(RAW_FIELDNAMES) - set(reader.fieldnames):
        # Reported, not guessed at: a writer that changed its columns must not be read
        # as if it had not.
        stats.unreadable.append(f"{where} (header: {reader.fieldnames})")
        return []
    stats.files += 1
    for row in reader:
        if row.get(None) is not None or any(row.get(k) is None for k in RAW_FIELDNAMES):
            stats.short_rows += 1
            bad_here += 1
            continue
        wall = _as_float(row["timestamp"])
        cpu = _as_float(row["cpu_percent"])
        mem = _as_float(row["memory_rss_bytes"])
        if wall is None or cpu is None or mem is None or wall <= 0:
            # A zero or negative stamp is an absence, not an instant in 1970 -- admitting
            # one puts the whole run behind it in every ORDER BY.
            stats.bad_rows += 1
            bad_here += 1
            continue
        samples.append((wall, row["name"] or "", cpu, int(mem)))

    if not samples:
        stats.empty.append(where)
        return []
    if bad_here:
        last = max(wall for wall, _, _, _ in samples)
        samples = [s for s in samples if s[0] != last]
        stats.dropped_tail_ticks += 1
        stats.truncated.append(f"{where} ({bad_here} bad row(s), last tick dropped)")
    stats.samples += len(samples)
    if container not in stats.containers:
        stats.containers.append(container)
    return samples


def collect_job_ticks(job_dir: str, expected: Optional[Dict[str, str]],
                      stats: ScanStats, label: str = "") -> List[Tick]:
    """Every container's samples in one job, rolled up per ``(container, tick)``.

    *expected* is ``{container: filename}`` from the campaign's container plan, or ``None``
    when the plan could not be read. A container the plan names but that wrote no file is a
    real finding — most often a vanilla sidecar image without ``psutil``, where
    ``monitor_resources.py`` dies before it opens the file and the entrypoint backgrounds it
    without checking. It is reported, never inferred away by taking the files that happen to
    be there.

    Window-independent by construction, so a packed job's ticks are read and rolled up once
    and then sliced per run.
    """
    label = label or os.path.basename(job_dir)
    try:
        present = {name: os.path.join(job_dir, name) for name in sorted(os.listdir(job_dir))
                   if name.startswith(_CSV_PREFIX) and name.endswith(".csv")}
    except OSError as e:
        stats.unreadable.append(f"{label} ({e.strerror or e})")
        return []

    wanted: Dict[str, str] = {}
    if expected is None:
        for name, path in present.items():
            container = run_slices.container_of(name)
            if container:
                wanted[container] = path
    else:
        for container, filename in expected.items():
            if filename in present:
                wanted[container] = present[filename]
            else:
                stats.missing.append(f"{label}:{container}")
        for name, path in present.items():
            container = run_slices.container_of(name)
            if container and container not in expected:
                stats.unexpected.append(f"{label}:{container}")
                wanted[container] = path

    grouped: Dict[Tuple[float, str], Dict[str, Tuple[float, int, int]]] = {}
    for container, path in sorted(wanted.items()):
        for wall, name, cpu, mem in read_container_csv(path, container, stats, label):
            bucket = grouped.setdefault((wall, container), {})
            cpu_sum, mem_sum, pids = bucket.get(name, (0.0, 0, 0))
            bucket[name] = (cpu_sum + cpu, mem_sum + mem, pids + 1)

    ticks = [Tick(wall_ts=wall, container=container, processes=processes)
             for (wall, container), processes in sorted(grouped.items())]
    stats.ticks += len(ticks)
    return ticks


def rows_for_slice(ticks: Sequence[Tick], slice_: run_slices.RunSlice) -> List[dict]:
    """The CSV rows for one run: the ticks it claims, on its clock.

    ``timestamp`` is empty where the clock map cannot answer — every tick before the
    simulator started publishing ``/clock`` (image boot, stack bring-up) and every one after
    it stopped. That is real measurement with no sim time, not measurement at sim time zero,
    and nothing is extrapolated.
    """
    rows: List[dict] = []
    for tick in ticks:
        if not slice_.claims(tick.wall_ts):
            continue
        sim = slice_.clock.to_sim(tick.wall_ts) if slice_.clock else None
        marker = slice_.in_window(tick.wall_ts)
        for name, (cpu, mem, pids) in sorted(tick.processes.items()):
            rows.append({
                "timestamp": "" if sim is None else f"{sim:.6f}",
                "wall_ts": f"{tick.wall_ts:.9f}",
                "in_window": marker,
                "container": tick.container,
                "name": name,
                "cpu_percent": f"{cpu:.2f}",
                "memory_rss_bytes": mem,
                "num_pids": pids,
            })
    return rows


def expected_container_files(plan) -> Optional[Dict[str, str]]:
    """``{container: filename}`` the campaign should have produced, or ``None``.

    The main container's file is named for its role (``resource_usage_main.csv``) while the
    container is called ``robovast`` everywhere a table names it, so the two are mapped here
    rather than at each reader.
    """
    if plan is None:
        return None
    from robovast.common.log_tail import MAIN_CONTAINER  # pylint: disable=import-outside-toplevel
    files = {MAIN_CONTAINER: f"{_CSV_PREFIX}main.csv"}
    for container in plan.sidecars:
        files[container.name] = f"{_CSV_PREFIX}{container.name}.csv"
    return files
