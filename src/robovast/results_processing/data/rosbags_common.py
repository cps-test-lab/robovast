#!/usr/bin/env python3
# Copyright (C) 2025 Frederik Pasch
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

import csv
import json
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

# -- how much CPU this process may actually use -------------------------------


def available_cpus() -> int:
    """Cores this process may use, from its cgroup rather than from the machine.

    ``os.cpu_count()`` reports the cores the kernel can see, which in a container is every
    core of the node and has nothing to do with what the container may have. Sizing a worker
    pool from it puts one worker per core of the machine inside an allocation of a few cores:
    the workers do not run faster for being many, they spend the conversion competing for a
    quota they collectively exceeded, and each one holds a bag's worth of memory while it
    waits. On a large node that is dozens of deserializers inside a limit sized for a handful.

    Lives in this module because it is the only one the conversion can reach: the container
    gets this directory mounted and nothing else of ``robovast`` (see ``docker_exec.sh``), and
    ``rosbags_process.py`` runs there as a standalone script.

    Read in order of how specifically each source describes *this* process:

    1. the cgroup v2 CPU quota -- what the kernel will actually enforce;
    2. the cgroup v1 quota, for an older host;
    3. the CPU affinity mask, which bounds us even with no quota set;
    4. the machine's cores, when nothing above applies -- an uncontained process.

    Rounded up, and never below 1: a fractional quota still runs, one worker at a time.
    """
    def _quota_v2() -> "Optional[float]":
        try:
            with open("/sys/fs/cgroup/cpu.max", encoding="utf-8") as handle:
                quota, period = handle.read().split()
        except (OSError, ValueError):
            return None
        # "max" is the literal the kernel writes for "no quota", not a number to parse.
        if quota == "max":
            return None
        try:
            return float(quota) / float(period)
        except (ValueError, ZeroDivisionError):
            return None

    def _quota_v1() -> "Optional[float]":
        try:
            with open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us", encoding="utf-8") as handle:
                quota = float(handle.read().strip())
            with open("/sys/fs/cgroup/cpu/cpu.cfs_period_us", encoding="utf-8") as handle:
                period = float(handle.read().strip())
        except (OSError, ValueError):
            return None
        # A negative quota is v1's spelling of "unlimited".
        if quota <= 0 or period <= 0:
            return None
        return quota / period

    cores = _quota_v2() or _quota_v1()
    if cores is None:
        try:
            cores = float(len(os.sched_getaffinity(0)))
        except (AttributeError, OSError):
            cores = float(os.cpu_count() or 1)
    return max(1, int(math.ceil(cores)))


# -- the wall<->sim clock map -------------------------------------------------
#
# Lives here, in the one module that both sides can reach: rosbags_process.py is copied
# into the container as a *standalone script* (see common/execution.py's _transient copy)
# and can import nothing from the robovast package, while the host-side reader
# (results_processing/clock_map.py) needs the identical accuracy promise. One definition,
# two importers.

#: Column names of ``clock_map.csv``.
CLOCK_MAP_FIELDNAMES = ["wall_ts", "sim_ts"]

#: Filename, written beside the bag it was extracted from.
CLOCK_MAP_FILENAME = "clock_map.csv"

#: How far the decimated map may mispredict sim time, in seconds. Every kept sample is
#: exact; this bounds the error of what was *dropped*. 5 ms is two orders of magnitude
#: below anything a log line is read at, and it turns a constant-rate run's tens of
#: thousands of ``/clock`` messages into a handful of rows.
DEFAULT_CLOCK_TOLERANCE_S = 0.005

#: How many consecutive samples may be dropped before one is kept regardless. Two reasons,
#: both practical: it bounds the per-sample work of re-checking the dropped ones (without
#: it, a perfectly straight run re-scans an ever-growing buffer and the pass becomes
#: quadratic), and it puts a floor under how much of a long segment a single corrupt sample
#: could misrepresent. The extra rows are negligible — one per 512 messages.
_MAX_DROPPED_RUN = 512


class ClockDecimator:
    """Streaming line simplification for ``(wall, sim)`` clock samples.

    ``/clock`` arrives at 100-1000 Hz, and most of what it says is "still the same rate".
    Keeping every message would make the map larger than the log it exists to place. So a
    sample is kept only when dropping it would mispredict sim time by more than
    *tolerance* under the linear interpolation the reader performs — a promise about
    accuracy, where a fixed-Hz thinning would only be a promise about size.

    Rate-independent by construction: a steady stretch costs two samples however long it
    is, while a pause or a change of real-time factor keeps the samples that describe it.

    Every dropped sample is re-checked against the candidate chord, not just the most
    recent one. Checking only the newest lets error accumulate across a curve — a smoothly
    changing real-time factor then drifts past the tolerance one imperceptible step at a
    time, which is exactly the case a caller trusting the stated bound would not think to
    look for.

    Usage: :meth:`offer` each sample in wall order, then :meth:`close` — which emits the
    final sample, because the map's right edge is where the run stopped and the reader
    refuses to extrapolate past it.
    """

    def __init__(self, tolerance_s: float = DEFAULT_CLOCK_TOLERANCE_S,
                 max_dropped_run: int = _MAX_DROPPED_RUN) -> None:
        self._tolerance = tolerance_s
        self._max_dropped_run = max(1, max_dropped_run)
        self._last_kept: Optional[Tuple[float, float]] = None
        #: Samples after :attr:`_last_kept`, provisionally dropped, oldest first.
        self._buffer: List[Tuple[float, float]] = []
        self.seen: int = 0

    def _chord_fits(self, target: Tuple[float, float]) -> bool:
        """Would the chord from the last kept sample to *target* reproduce the buffer?"""
        w0, s0 = self._last_kept
        w1, s1 = target
        span = w1 - w0
        if span <= 0:
            return False
        rate = (s1 - s0) / span
        return all(abs(sp - (s0 + rate * (wp - w0))) <= self._tolerance
                   for wp, sp in self._buffer[:-1])

    def offer(self, wall: float, sim: float) -> Optional[Tuple[float, float]]:
        """Take one sample; return a sample to write, if this one decided its fate."""
        self.seen += 1
        sample = (wall, sim)
        if self._last_kept is None:
            self._last_kept = sample
            return sample
        self._buffer.append(sample)
        if len(self._buffer) < 2:
            return None
        if self._chord_fits(sample) and len(self._buffer) <= self._max_dropped_run:
            return None
        # This sample cannot represent everything since the last kept one, so the sample
        # before it becomes the segment's end — the newest point that still could.
        keep = self._buffer[-2]
        self._last_kept = keep
        self._buffer = [sample]
        return keep

    def close(self) -> Optional[Tuple[float, float]]:
        """The final sample, or ``None`` when it was already written."""
        if not self._buffer:
            return None
        final = self._buffer[-1]
        self._buffer = []
        if final == self._last_kept:
            return None
        self._last_kept = final
        return final

    def run(self, samples: Sequence[Tuple[float, float]]) -> List[Tuple[float, float]]:
        """Decimate a whole sequence — the non-streaming convenience form."""
        kept = [s for s in (self.offer(w, t) for w, t in samples) if s is not None]
        final = self.close()
        if final is not None:
            kept.append(final)
        return kept


def write_provenance_entry(
    provenance_file_path: Optional[str],
    output_rel: str,
    sources_rel: List[str],
    plugin_name: str,
    params: Optional[dict] = None,
) -> None:
    """Append one provenance entry to a JSON file.

    Used by container scripts to record which output was produced from which
    sources. Paths should be relative to the results root (input dir).
    If provenance_file_path is None or empty, does nothing.

    Args:
        provenance_file_path: Path to the provenance JSON file (or None to skip).
        output_rel: Output path relative to results root.
        sources_rel: List of source paths relative to results root.
        plugin_name: Name of the plugin that produced the output.
        params: Optional dict of plugin parameters.
    """
    if not provenance_file_path:
        return
    entry = {
        "output": output_rel,
        "sources": list(sources_rel),
        "plugin": plugin_name,
        "params": params if params is not None else {},
    }
    parent = os.path.dirname(provenance_file_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    existing: List[dict] = []
    if os.path.exists(provenance_file_path):
        try:
            with open(provenance_file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                existing = data.get("entries", [])
        except (json.JSONDecodeError, OSError):
            existing = []
    existing.append(entry)
    with open(provenance_file_path, "w", encoding="utf-8") as f:
        json.dump({"entries": existing}, f, indent=2)


def gen_msg_values(msg, prefix=""):
    if isinstance(msg, list):
        for i, val in enumerate(msg):
            yield from gen_msg_values(val, f"{prefix}[{i}]")
    elif hasattr(msg, "get_fields_and_field_types"):
        for field, type_ in msg.get_fields_and_field_types().items():
            val = getattr(msg, field)
            full_field_name = prefix + "." + field if prefix else field
            if type_.startswith("sequence<"):
                for i, aval in enumerate(val):
                    yield from gen_msg_values(aval, f"{full_field_name}[{i}]")
            else:
                yield from gen_msg_values(val, full_field_name)
    else:
        yield prefix, msg


def find_rosbags(directory, bag_dir_name="rosbag2", skip_names=()):
    """Find all rosbag directories using parallel directory scanning (IO-bound).

    Uses a BFS with a ThreadPoolExecutor so that large result trees (e.g. 50k
    run directories on a network filesystem) are scanned concurrently rather
    than sequentially.

    A directory matches when its name is *bag_dir_name*'s first segment exactly, or when it
    is that name plus ``ros2 bag record``'s default timestamp suffix (``rosbag2_2026_07_15-
    10_30_00``) -- without the suffix rule a bag recorded with no explicit ``-o`` is
    invisible here, and the CLI reports "0 rosbags found" as a success.

    Args:
        directory: Root directory to search under.
        bag_dir_name: Subdirectory name to look for (default: "rosbag2").
                      May contain a path separator, e.g. "logs/rosout_bag".
        skip_names: Directory names never to descend into. The campaign's reserved
                    directories are passed here, because they hold no runs -- and one of
                    them, ``_calibration``, holds bags belonging to work that is not a run
                    at all. Converting those wastes a bag's work per node and, when a probe
                    was interrupted, fails the whole postprocessing step on a bag nothing
                    was ever going to read. Passed in rather than hardcoded: this module is
                    copied into the container standalone and can import no definition of
                    what "reserved" means.

    Returns:
        Sorted list of found rosbag directory paths.

    Raises:
        ValueError: if one directory holds more than one matching bag. Handlers derive
            their output path from the bag's parent, so two bags there would write the
            same CSV -- and since bags are processed by a worker pool, which one survived
            would be nondeterministic. Ambiguity is the user's to resolve.
    """
    parts = bag_dir_name.split("/")
    prune_top = parts[0]
    prune_rest = "/".join(parts[1:])
    # ros2 bag record's default output name: <prefix>_%Y_%m_%d-%H_%M_%S. Matching the shape
    # rather than any "<prefix>_*" keeps a rosbag2_backup or results_old out of the results.
    timestamped = re.compile(re.escape(prune_top) + r"_\d{4}_\d{2}_\d{2}-\d{2}_\d{2}_\d{2}$")
    found: List[str] = []
    skip = set(skip_names or ())

    def _scan(path: str):
        """Return (bag_paths, subdirs_to_recurse) for one directory."""
        bags: List[str] = []
        subdirs: List[str] = []
        try:
            with os.scandir(path) as it:
                for entry in it:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    if entry.name in skip:
                        continue
                    if entry.name == prune_top or timestamped.fullmatch(entry.name):
                        if not prune_rest:
                            bags.append(entry.path)
                            continue  # do not recurse into bag dir
                        candidate = os.path.join(entry.path, prune_rest)
                        if os.path.isdir(candidate):
                            bags.append(candidate)
                        else:
                            # Name matched but the bag isn't below it: an ordinary directory
                            # that happens to share the prefix. Recursing is what the caller
                            # wants -- not recursing hid every bag under it.
                            subdirs.append(entry.path)
                    else:
                        subdirs.append(entry.path)
        except OSError:
            pass
        # Two bags sharing a parent directory would share an output CSV (see Raises).
        by_parent: Dict[str, List[str]] = {}
        for bag in bags:
            by_parent.setdefault(os.path.dirname(bag), []).append(bag)
        for parent, siblings in by_parent.items():
            if len(siblings) > 1:
                raise ValueError(
                    f"Ambiguous rosbag layout: {parent} holds {len(siblings)} "
                    f"'{bag_dir_name}' bags "
                    f"({', '.join(sorted(os.path.basename(b) for b in siblings))}). "
                    f"Postprocessing writes one CSV per bag parent, so these would "
                    f"overwrite each other. Keep one bag per run directory, or point "
                    f"--bag-dir at the one you want."
                )
        return bags, subdirs

    # Threads, not processes, and the work is a directory walk -- so this is deliberately a
    # multiple of the CPU budget rather than equal to it: a scan blocks on the store far more
    # than it computes. The cap is what keeps a large allocation from opening more concurrent
    # reads than a store answers well.
    n_workers = min(32, available_cpus() * 4)
    pending = [directory]
    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        while pending:
            futures = {executor.submit(_scan, p): p for p in pending}
            pending = []
            for fut in as_completed(futures):
                bags, subdirs = fut.result()
                found.extend(bags)
                pending.extend(subdirs)

    return sorted(found)
#: The manifest every video producer writes beside its file, one row per video. Read by the
#: web run-view's ``camera`` panel and by the ``get_camera_frame`` MCP tool, which is why
#: ``t_start`` is here at all: the encode below re-times the frames onto a constant rate and
#: drops the bag stamps, so the file alone cannot be placed on the run's timeline.
#:
#: A CONTRACT, not this handler's private file. Anything that puts a video in a run directory
#: may write the same row -- another postprocessing step, a simulator that renders its own, a
#: user's script -- and the panel then works for it with no change. Tying the panel to "a
#: CompressedImage topic recorded through a rosbag" would bake a ROS-shaped assumption into a
#: feature that has no reason to carry one.
VIDEOS_CSV = "videos.csv"
VIDEO_FIELDNAMES = ["topic", "file", "t_start", "t_end", "fps", "frames"]


def register_video(out_dir: str, row: dict) -> str:
    """Record one video in *out_dir*'s :data:`VIDEOS_CSV`, replacing any row for the same file.

    Rewrite rather than append: ``on_end`` runs once per bag and postprocessing may be re-run
    over a directory that already has results, so appending would duplicate rows -- and two
    bags in one run have to share this file, because the database builder refuses two CSVs
    that map to the same table name.
    """
    path = os.path.join(out_dir, VIDEOS_CSV)
    rows = []
    if os.path.isfile(path):
        with open(path, "r", newline="", encoding="utf-8") as fh:
            rows = [r for r in csv.DictReader(fh) if r.get("file") != row["file"]]
    rows.append({k: row.get(k, "") for k in VIDEO_FIELDNAMES})
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=VIDEO_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    return path




# -- bags whose failure to open is expected -----------------------------------
#
# Lives here for the same reason as the clock map above: the predicate is used by the
# standalone container script (rosbags_process.py, which can import nothing from the
# robovast package) and has to be testable on the host, where the script itself cannot
# even be imported — it pulls in rosbag2_py at module level.


def resolve_tolerated_roots(input_root: str, relative_dirs: Sequence[str]) -> List[str]:
    """Absolute forms of *relative_dirs*, resolved once against *input_root*.

    Resolved up front so the per-bag test is a prefix compare rather than a path join
    per bag — a campaign can carry thousands.
    """
    return [os.path.normpath(os.path.join(input_root, rel)) for rel in relative_dirs or ()]


def is_under_tolerated_root(bag_path: str, tolerated_roots: Sequence[str]) -> bool:
    """Is *bag_path* inside one of *tolerated_roots*?

    True for a bag belonging to a job an operator stopped by hand: killing the pod
    mid-write leaves the bag unfinalized, so it cannot be opened and never will be.
    Such an error is expected and must not fail the postprocessing step — see
    ``rosbags_process.py --tolerate-under``.

    The separator is part of the test (``root + os.sep``) so ``_jobs/job-2`` does not
    swallow ``_jobs/job-20``, which is exactly the sibling a packed campaign has.
    """
    path = os.path.abspath(bag_path)
    return any(path == root or path.startswith(root + os.sep) for root in tolerated_roots)


def failing_bag_output(results: Sequence[Tuple[str, str]], input_root: str,
                       tolerated_roots: Sequence[str]) -> List[Tuple[str, str]]:
    """``(bag path relative to input_root, its captured output)`` per bag worth reporting.

    Workers run under ``redirect_stdout`` so 32 of them cannot shred the progress bar, and
    what they printed comes home with their result instead of dying in the pool. This picks
    the part a reader needs: bags that actually said something, minus the tolerated ones.

    Tolerated bags are excluded because they are *expected* — a job stopped by hand leaves
    an unfinalized bag, every one of them prints the same "failed to open", and a campaign
    with twenty such jobs would bury the real errors under twenty copies of a non-problem
    the summary's NOTE already explains. They are counted apart for the same reason.

    Bags that could not be opened at all never reach here: they are listed by
    :func:`unreadable_bag_note` under their own heading, because a block titled "bags that
    reported errors" is the wrong place for the one outcome that is not an error.
    """
    out = []
    for bag_path, text in results:
        if not text.strip() or is_under_tolerated_root(bag_path, tolerated_roots):
            continue
        out.append((os.path.relpath(bag_path, input_root), text.rstrip()))
    return out


def unreadable_bag_note(bag_paths: Sequence[str], input_root: str,
                        limit: int = 10) -> List[str]:
    """The lines describing bags that could not be opened — named, and capped.

    Named because "5 bags were skipped" is not actionable and "these five were skipped" is:
    the reader can go and see whether the runs behind them mattered. Capped because a
    campaign that lost a whole batch would otherwise print hundreds of paths and push the
    summary off the top of the log, which is the same burying problem
    :func:`failing_bag_output` avoids for real errors.
    """
    if not bag_paths:
        return []
    rels = sorted(os.path.relpath(p, input_root) for p in bag_paths)
    lines = [f"  {rel}" for rel in rels[:limit]]
    if len(rels) > limit:
        lines.append(f"  ... and {len(rels) - limit} more")
    return lines


def handler_error_pointer(has_output: bool) -> str:
    """Where the error summary sends a reader — and never at evidence that is not there.

    "We looked and the workers said nothing" and "we never looked" are different facts, and
    collapsing them is how this line spent its life pointing at output that had already been
    discarded: the count travelled home in a sentinel while the message that explained it
    was thrown away with the worker's buffer.
    """
    return ("see the error output above" if has_output
            else "the workers printed nothing about them")


#: ``BagResult.total`` sentinels. Named because they are tested far from where they are
#: produced, and ``-1``/``-2`` at a branch says nothing about which one means what.
#: They stay ints rather than an enum: the value crosses a ``multiprocessing`` boundary and
#: shares a slot with a record count, and the handler protocol below produces ``-2`` of its
#: own -- splitting status from count properly means changing nine ``on_end`` signatures in
#: a module no test can import (``rosbag2_py`` at module level), which is a wide mechanical
#: change with no way to verify it.
#:
#: ``UNREADABLE`` is separate from ``FAILED`` because the two demand opposite verdicts. A
#: bag that cannot be opened is *missing input*: the recorder never wrote the sidecar that
#: names its storage plugin, so there is nothing to convert and no amount of retrying will
#: make one. A bag that opened and whose handlers then threw is *broken conversion*: the
#: data was there and the step did not produce it. Only the second may fail the step.
CACHED = -1
FAILED = -2
UNREADABLE = -3


class BagResult(NamedTuple):
    """One bag's outcome, as it travels back through the Pool.

    Named rather than a bare tuple because the worker returns from five places and the
    parent reads it in four, with nothing tying the two ends together: widening a
    positional tuple means editing all nine and getting a ``ValueError`` at unpack time
    for the one that was missed. That error would surface only inside the ROS execution
    container -- past the ``rosbag2_py`` import, where nothing in the test suite reaches
    -- so the failure mode is "works everywhere it can be tested". With defaults, a
    return site that does not mention a field gets the field's neutral value instead.

    A ``NamedTuple`` specifically: this crosses a ``multiprocessing`` boundary, so it has
    to pickle, and it must cost nothing beyond the standard library (this script runs with
    stdlib + ROS libs and one sibling import, nothing else).
    """

    #: The bag this describes.
    bag_path: str
    #: Records written, or a sentinel: ``-1`` cache hit, ``-2`` the handlers failed,
    #: ``-3`` the bag could not be opened, ``0`` opened but produced nothing.
    total: int
    #: ``(record_count, output_files)`` per handler.
    handler_results: Sequence[Tuple[int, List[str]]] = ()
    #: What the worker printed, kept only for a bag that failed — see the capture below.
    #: Defaults empty: a result that says nothing about failing did not fail.
    output: str = ""

    @property
    def cached(self) -> bool:
        """Already converted; its outputs are on disk and nothing was re-read."""
        return self.total == CACHED

    @property
    def failed(self) -> bool:
        """The bag was readable but its conversion did not happen — no handler would start."""
        return self.total == FAILED

    @property
    def unreadable(self) -> bool:
        """The bag could not be opened, so there was no input to convert.

        Distinct from :attr:`failed`: this is a gap in what the campaign recorded, not a
        defect in the conversion, and the step describes it and carries on.
        """
        return self.total == UNREADABLE
