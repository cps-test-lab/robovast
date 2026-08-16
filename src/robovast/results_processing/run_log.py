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

"""Everything a run said, merged into one time-ordered record.

A run's output arrives twice and in two shapes, and neither alone is the log:

* ``logs/system.log`` and ``logs/system_<container>.log`` — what each container printed.
  This is the only source for a non-ROS run, for anything a node wrote to stdout without
  going through ROS logging (a Python traceback, a gz warning), and for the stack coming
  up before any scenario started. Lines here carry their own ``[LEVEL] [epoch] [node]``
  stamp: rclpy writes it, and so do the entrypoints and scenario-execution's logger, which
  is what makes a non-ROS run placeable in time at all. Third-party output (a gz warning,
  a vanilla sidecar's) carries nothing and is bracketed by its neighbours instead — never
  dropped, whatever it looks like.
* ``logs/rosout.csv`` — ``/rosout``, structured: level, node, and the source location.

**They overlap almost completely.** Measured on a three-container campaign, 473 of 521
rosout rows are the same event as a line in a ``system*.log``, because a launch container
forwards its nodes' output to stdout as well. Concatenating the two sources would therefore report most
of the run twice. So the merge is a *join*, and the join is what makes it a log rather
than two piles of lines.

The join also supplies something neither source has on its own: ``/rosout`` carries the
node name but not the container the node ran in, and the container is what a reader filters
by. It comes from the file the stdout twin was found in — and, for a row whose twin the exact
join could not find, from the same node's other lines
(:func:`attribute_containers_by_node`).

Timestamps are wall throughout — sim time is added by the caller from a
:class:`~robovast.results_processing.clock_map.ClockMap`, and stays ``None`` where the map
cannot answer (see that module for why nothing is extrapolated).
"""

from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass, field, fields
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from robovast.common import log_summary
# Re-exported: ``container_of`` returns this, so a caller comparing against it reads one
# name from one module. It is unused *in* this file, which is why removing it looked safe
# -- and broke a test that imports it from here.
from robovast.common.log_tail import MAIN_CONTAINER  # noqa: F401  # pylint: disable=unused-import

from . import run_slices

#: Written into each run directory, one row per log event.
FILENAME = "run_log.csv"

#: The table's columns, in reading order: when, where from, how bad, what.
FIELDNAMES = [
    "sim_time", "wall_ts", "time_source", "in_window",
    "container", "node", "source", "level", "severity",
    "message", "file", "function", "line",
]

#: ``time_source`` values — how the row's ``wall_ts`` was arrived at, which is not the same
#: question as whether it could be converted to sim time.
TIME_EXACT = "stamp"        #: the producer's own stamp, nanosecond precision
TIME_INHERITED = "inherited"  #: no stamp of its own; bracketed by a neighbouring stamp
TIME_NONE = "none"          #: no stamp anywhere in the log to bracket it with

#: ``source`` values.
SRC_ROSOUT = "rosout"
SRC_STDOUT = "stdout"

#: The main container's log; a sidecar's is ``system_<container>.log``.
_MAIN_LOG = "system.log"
_SIDECAR_RE = re.compile(r"^system_(?P<container>.+)\.log$")


@dataclass
class LogRecord:
    """One log event, before sim time is attached."""
    wall_ts: Optional[float]
    time_source: str
    container: str
    node: str
    source: str
    level: str
    message: str
    file: str = ""
    function: str = ""
    line: str = ""

    @property
    def severity(self) -> str:
        """Reuses the one severity definition, applied to the reconstructed line.

        Passing the level marker back through :func:`log_summary.severity_of` rather than
        mapping it here is deliberate: a stdout line with no level of its own then gets the
        published keyword classification, and a level that *is* present still wins — one
        answer to "is this bad?", shared with the status and the MCP log tools.
        """
        if self.level:
            return log_summary.severity_of(f"[{self.level}] [0.0] [{self.node or 'x'}]: "
                                           f"{self.message}")
        return log_summary.severity_of(self.message)

    @property
    def join_key(self) -> Optional[Tuple[str, int, str]]:
        """``(node, wall_ns, first line)`` — the same event seen through either source.

        ``None`` when the record has no stamp of its own: a line whose time was inherited
        cannot be matched, and guessing would merge two different lines that happened to
        sit behind the same stamp.

        Two decisions worth stating, because getting either wrong silently doubles the
        table:

        * The timestamp axis must be the **producer's** stamp on both sides. Keying rosout
          on the bag's *receive* time instead matched **0 of 521** rows on the campaign this
          was measured against, because the transport delay (~0.1 ms) puts every pair on
          different nanoseconds. With the producer's stamp, 473 match exactly — and a
          proximity window on top of that adds nothing, so there is none.
        * Only the message's **first** line, because the two sources disagree about line
          structure: a multi-line message is one rosout row and N stdout lines.
        """
        if self.wall_ts is None or self.time_source != TIME_EXACT:
            return None
        return (self.node, int(round(self.wall_ts * 1_000_000_000)),
                self.message.split("\n", 1)[0].strip())


@dataclass
class MergeStats:
    """What the merge did, so a parser regression shows up as a number that moved.

    ``matched`` is the count of rosout rows that found their stdout twin. It is reported
    rather than assumed because the join is a heuristic over two representations: if a
    future relay rewrites messages, this ratio drops and the table quietly gains duplicate
    rows. A number in the provenance is what makes that visible.
    """
    stdout_lines: int = 0
    stdout_records: int = 0
    rosout_records: int = 0
    matched: int = 0
    node_attributed: int = 0
    rows: int = 0
    containers: List[str] = field(default_factory=list)

    #: Counted per RUN by the caller, because a packed job's records are sliced into several
    #: runs — so it is the one field :meth:`add_job` must not take from a job's stats.
    _PER_RUN_FIELDS = ("rows",)

    def add_job(self, other: "MergeStats") -> None:
        """Fold one job's stats into these campaign totals.

        Driven by the dataclass fields rather than a written-out list of additions, because
        such a list drops a counter added to the class later without saying so — and the
        summary then reports zero however much the merge did, which is the exact opposite of
        what these counters exist for.
        """
        for spec in fields(self):
            if spec.name in self._PER_RUN_FIELDS:
                continue
            if spec.name == "containers":
                for name in other.containers:
                    if name not in self.containers:
                        self.containers.append(name)
            else:
                setattr(self, spec.name, getattr(self, spec.name) + getattr(other, spec.name))

    def summary(self) -> str:
        return (f"{self.rows} rows "
                f"({self.rosout_records} rosout, {self.stdout_records} stdout from "
                f"{self.stdout_lines} lines, {self.matched} matched, "
                f"{self.node_attributed} by node) "
                f"across {len(self.containers)} container(s)")


def container_of(filename: str) -> Optional[str]:
    """The container a ``system*.log`` belongs to, or ``None`` if it is not one.

    Delegated to :func:`run_slices.container_of`, which maps every per-container job
    artifact. One mapping, because ``run_log.container`` and the other derived tables' are
    joined on: if two producers spelled the main container differently the join would return
    nothing rather than fail.
    """
    return run_slices.container_of(filename)


def parse_container_log(lines: Iterable[str], container: str) -> List[LogRecord]:
    """Pass 1: one record per log *event* in one container's stdout.

    Events, not lines. A line that carries no marker of its own is folded into the
    preceding record as a continuation, which is what keeps a forty-line traceback one
    entry instead of forty, and what makes a stdout event the same shape as its rosout
    twin so the two can be joined at all.

    A line's time comes from the line itself. Every producer in these containers stamps its
    own output — ROS nodes through rclpy, the entrypoints through their shared ``log``
    helper, scenario-execution through its logger — so a stamp is the normal case, and what
    is left unstamped is third-party output (a gz warning, a vanilla sidecar's).

    Such a line inherits the preceding record's time as a *continuation* of it
    (:data:`TIME_INHERITED`), and reports :data:`TIME_NONE` where there is nothing before it.
    It is deliberately **not** backfilled from the next stamp: an untimed row is honest about
    being untimed, while a borrowed later time renders exactly like a real one and would
    claim the container booted at whatever second the first node came up.

    Unstamped output is never dropped, whatever it is — a producer that stamps nothing gets a
    row per line and stays readable and searchable. That is the contract a future tool relies
    on: it may print however it likes and still be read.
    """
    records: List[LogRecord] = []
    for raw in lines:
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        parsed = log_summary.peel_prefixes(line)
        if parsed.wall_ts is not None:
            records.append(LogRecord(
                wall_ts=parsed.wall_ts, time_source=TIME_EXACT, container=container,
                node=parsed.node, source=SRC_STDOUT, level=parsed.level,
                message=parsed.message))
            continue
        # No stamp of its own. It is a *continuation* only of an event that had one — a
        # traceback under its ERROR line, a gz warning's second line. Folding it into
        # another unstamped line instead would swallow a whole unstamped log into one row:
        # the entrypoint's 46 bash lines are 46 things that happened, not one.
        if records and records[-1].time_source == TIME_EXACT:
            records[-1].message += "\n" + line
            continue
        records.append(LogRecord(
            wall_ts=None, time_source=TIME_NONE, container=container,
            node=parsed.node, source=SRC_STDOUT, level=parsed.level,
            message=parsed.message))
    return records


def _first_float(*values) -> Optional[float]:
    """The first value that parses as a non-zero float, else ``None``.

    Zero is rejected along with unparsable: a ``/rosout`` message published before its
    node's clock was ready carries ``stamp == 0``, which is not an instant in 1970 but an
    absence, and treating it as one would sort the whole run behind it.
    """
    for value in values:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if parsed:
            return parsed
    return None


def read_rosout(path: str) -> List[LogRecord]:
    """Pass 2: ``rosout.csv`` in the same record shape.

    The container is left empty on purpose: ``/rosout`` says which *node* logged, never
    which container it ran in. :func:`merge_records` fills it in from the stdout twin,
    which is the only place that fact exists.
    """
    records: List[LogRecord] = []
    if not path or not os.path.isfile(path):
        return records
    with open(path, newline="", encoding="utf-8", errors="replace") as handle:
        for row in csv.DictReader(handle):
            # The producer's own stamp, not the bag's receive time: it is the instant the
            # event happened, and it is the same number rcutils printed into stdout, which
            # is what lets the two sources be matched at all. Receive time is a property of
            # the recorder, and differs by the transport delay (~0.1 ms here).
            wall = _first_float(row.get("stamp"), row.get("timestamp"))
            records.append(LogRecord(
                wall_ts=wall,
                time_source=TIME_EXACT if wall is not None else TIME_NONE,
                container="", node=row.get("name") or "", source=SRC_ROSOUT,
                level=row.get("level_name") or "", message=row.get("msg") or "",
                file=row.get("file") or "", function=row.get("function") or "",
                line=row.get("line") or ""))
    return records


def attribute_containers_by_node(records: Sequence[LogRecord],
                                 stats: Optional[MergeStats] = None) -> None:
    """Fill a blank container from the same node's other lines, in place.

    A rosout row learns its container from its stdout twin, but the join is exact, and a line
    that arrived mangled has no twin and so no container. The common case is two writes landing
    on one line because the first carried no newline, which leaves the second's stamp
    unpeelable: ``[INFO] [t] [scenario_execution_ros]: stdin is not a terminal device.[INFO]
    [t] [rosbag2_recorder]: Press SPACE for pausing/resuming``.

    The node is still named, though, and the *same node's* other lines were placed. A node runs
    in one container, so that is where this line ran too. This is why one line of a node can
    lack a container while the next one has it — ``rosbag2_recorder`` was attributed 22 times
    and blank 11 times in a single measured run. Across 21 campaigns the pass resolves 534 of
    539 blanks.

    Evidence rather than a guess, and the guard is what keeps it so: a node seen in two
    containers is left blank. ``entrypoint`` really does run in every one of them, and a
    filterable wrong container is worse than an honest blank — see
    :func:`collect_job_records`.
    """
    seen: Dict[str, set] = {}
    for rec in records:
        if rec.node and rec.container:
            seen.setdefault(rec.node, set()).add(rec.container)
    for rec in records:
        if rec.container or not rec.node:
            continue
        candidates = seen.get(rec.node)
        if candidates and len(candidates) == 1:
            rec.container = next(iter(candidates))
            if stats is not None:
                stats.node_attributed += 1


def merge_records(stdout_records: Sequence[LogRecord],
                  rosout_records: Sequence[LogRecord],
                  stats: Optional[MergeStats] = None) -> List[LogRecord]:
    """Pass 3: join the two sources into one row per event, then order by wall time.

    A rosout row that has a stdout twin keeps rosout's structured fields (level, and the
    source location) and takes the twin's container. A row with no twin is emitted as it
    is, saying which source it came from — stdout-only is the traceback and the gz warning,
    rosout-only is a node whose container's stdout was not captured. Such a row can still
    learn its container from the same node's other lines, which is the last step here
    (:func:`attribute_containers_by_node`).

    The match is exact on :attr:`LogRecord.join_key`; a tolerance was tried and matched no
    additional row, so there is none to reason about. Because the key carries the
    nanosecond stamp, repeats of one message are distinct keys and pair with themselves.

    Ordering is by wall time, with unstamped records held next to the record they followed:
    they have no time of their own, so sorting them by their inherited value keeps them
    where they were written instead of scattering them among other containers' lines.
    """
    stats = stats or MergeStats()
    unclaimed: Dict[Tuple[str, int, str], LogRecord] = {}
    for rec in stdout_records:
        key = rec.join_key
        if key is not None:
            unclaimed.setdefault(key, rec)

    merged: List[LogRecord] = []
    claimed: set = set()
    for rec in rosout_records:
        key = rec.join_key
        twin = unclaimed.pop(key, None) if key is not None else None
        if twin is not None:
            claimed.add(id(twin))
            stats.matched += 1
            rec.container = twin.container
            # The stdout twin may carry continuation lines the single rosout row does not
            # (rosout truncates at the newline in some producers); keep the longer text.
            if len(twin.message) > len(rec.message):
                rec.message = twin.message
        merged.append(rec)
    merged.extend(rec for rec in stdout_records if id(rec) not in claimed)

    # Stable sort on the timestamp alone: equal stamps (a burst inside one tick) keep the
    # order they were read in, which is the order they were written.
    merged.sort(key=lambda r: (r.wall_ts if r.wall_ts is not None else float("-inf")))
    attribute_containers_by_node(merged, stats)
    stats.rosout_records = len(rosout_records)
    stats.stdout_records = len(stdout_records)
    stats.rows = len(merged)
    return merged


def collect_job_records(job_dir: str, stats: Optional[MergeStats] = None,
                        sole_container: Optional[str] = None) -> List[LogRecord]:
    """Every log event a job produced: each container's stdout, joined with ``/rosout``.

    One job, not one run: these artifacts are written per job, and a job may run several
    configurations in sequence (``runs_per_job``). Splitting them per run is
    :func:`rows_for_window`'s job.

    *sole_container* names the one container this campaign runs, when it runs exactly one. A
    rosout row learns its container from its stdout twin, so a row without one has none — and
    with a single container there is only one place it can have come from, so filling it in is
    not a guess.

    It must come from the campaign's **declared** containers, not from counting the log files
    found here. A sidecar is explicitly allowed to be a vanilla image
    (the ROS shape's "point it at any nav2 image" promise), and such an image
    never runs ``secondary_entrypoint.sh``, so it writes no ``system_<name>.log``. Inferring from
    the file count would then label that sidecar's ``/rosout`` lines with whichever container did
    write a log — a confident wrong container, which is worse than an honest blank because it is
    filterable and believable.
    """
    stats = stats or MergeStats()
    logs_dir = os.path.join(job_dir, "logs")
    stdout_records: List[LogRecord] = []

    sources: List[Tuple[str, str]] = []
    if os.path.isdir(logs_dir):
        for name in sorted(os.listdir(logs_dir)):
            container = container_of(name)
            if container is None:
                continue
            sources.append((os.path.join(logs_dir, name), container))

    for path, container in sources:
        stats.containers.append(container)
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()
        except OSError:
            continue
        stats.stdout_lines += len(lines)
        stdout_records.extend(parse_container_log(lines, container))
    rosout = read_rosout(os.path.join(logs_dir, "rosout.csv"))
    merged = merge_records(stdout_records, rosout, stats)

    # The last resort, for what neither the join nor the node evidence could place. Rows with no
    # stdout twin are common (614 of 1256 on a measured single-container campaign); most now
    # learn their container from another line of the same node, and what reaches here is a node
    # whose output was relayed to no captured stdout *at all*, so nothing places any of it.
    #
    # Filled in only when the *campaign* declares one container -- see the docstring for why the
    # number of log files is not the same question. The logs found must agree, so a campaign
    # whose sidecars did write logs is never overridden by a stale caller.
    if sole_container and len(set(stats.containers)) <= 1:
        for rec in merged:
            if not rec.container:
                rec.container = sole_container
    return merged


def rows_for_window(records: Sequence[LogRecord], clock, *,
                    start_epoch: Optional[float] = None,
                    end_epoch: Optional[float] = None) -> List[dict]:
    """Pass 4: the CSV rows for one run — sim time attached, window marked.

    *clock* is a :class:`~robovast.results_processing.clock_map.ClockMap`; where it cannot
    answer, ``sim_time`` is empty. That happens for every line logged before the simulator
    started publishing ``/clock`` (image boot, stack bring-up), which is real output with no
    sim time rather than output at sim time zero.

    ``in_window`` is 0 for a line outside this run's own wall window. In a packed job those
    lines are the simulator being reset between runs — real output that belongs to *some*
    run, so it is attributed to the nearest one rather than dropped, and flagged so a query
    can tell "during this trial" from "while getting ready for it". With no window given
    (the single-run job) everything is in-window.
    """
    rows: List[dict] = []
    for rec in records:
        wall = rec.wall_ts
        in_window = run_slices.in_window(wall, start_epoch, end_epoch)
        sim = clock.to_sim(wall) if clock else None
        rows.append({
            "sim_time": "" if sim is None else f"{sim:.6f}",
            "wall_ts": "" if wall is None else f"{wall:.9f}",
            "time_source": rec.time_source,
            "in_window": in_window,
            "container": rec.container,
            "node": rec.node,
            "source": rec.source,
            "level": rec.level,
            "severity": rec.severity,
            "message": rec.message,
            "file": rec.file,
            "function": rec.function,
            "line": rec.line,
        })
    return rows


def write_run_log(path: str, rows: Sequence[dict]) -> None:
    """Write ``run_log.csv``. A header-only file is written when a run logged nothing, so
    the table exists and the panel can say "no lines" instead of "no table"."""
    run_slices.write_csv(path, FIELDNAMES, rows)
