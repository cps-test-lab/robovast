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

"""Tables derived from a job's records and cut to its runs.

A job's container logs, its resource samples and its wall-time recording are written once
per job, and a job may serve several runs (``runs_per_job``). So these tables are built for a
whole job at once -- every run of it, because where one run's share of the job ends is a
property of all of them (:mod:`robovast_decode.run_slices`) -- and each run gets its part:

=====================  ==========================================================================
``run_log``            every container's stdout joined with ``/rosout``, one row per event, on
                       the run's clock (:mod:`robovast_decode.run_log`)
``scenario_timestamps`` the run's verdict line, on both clocks (:mod:`.scenario_markers`)
``resource_usage``     the resource monitor's samples, per container and process name
                       (:mod:`robovast_decode.resource_usage`)
``system_usage``       the container-level counters (:mod:`robovast_decode.system_usage`)
``run_clock``          what relates the run's wall stamps to sim time, and how well
=====================  ==========================================================================

They read the job's ``rosout`` and ``clock_map`` tables, which are built first. The
campaign's decoder configuration names its containers (``containers``), which is how a
container that recorded nothing is reported rather than silently absent.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import pyarrow as pa
import pyarrow.parquet as pq

from . import clock_map, resource_usage, run_log, run_slices, scenario_markers, system_usage
from .layout import MAIN_CONTAINER
from .tables import cache_root
from .types import INTEGER, REAL, TEXT, UNKNOWN, infer_column_types, stored_value

RUN_LOG = "run_log"
SCENARIO_TIMESTAMPS = "scenario_timestamps"
RESOURCE_USAGE = "resource_usage"
SYSTEM_USAGE = "system_usage"
RUN_CLOCK = "run_clock"

#: Every derived table, in the order they are built.
DERIVED = (RUN_LOG, SCENARIO_TIMESTAMPS, RESOURCE_USAGE, SYSTEM_USAGE, RUN_CLOCK)

#: The recorded tables they read, built from the job's wall-time recording before them.
INPUTS = ("rosout", "clock_map")

_RUN_LOG_TYPES = {
    "seq": pa.int64(), "sim_time": pa.float64(), "wall_ts": pa.float64(),
    "time_source": pa.string(), "in_window": pa.int64(), "container": pa.string(),
    "node": pa.string(), "source": pa.string(), "level": pa.string(),
    "severity": pa.string(), "message": pa.string(), "file": pa.string(),
    "function": pa.string(), "line": pa.string()}
_VERDICT_TYPES = {"timestamp": pa.float64(), "wall_ts": pa.float64(), "status": pa.string(),
                  "message": pa.string()}
_RESOURCE_TYPES = {
    "timestamp": pa.float64(), "wall_ts": pa.float64(), "in_window": pa.int64(),
    "container": pa.string(), "name": pa.string(), "cpu_percent": pa.float64(),
    "memory_rss_bytes": pa.int64(), "num_pids": pa.int64(), "shm_used_bytes": pa.int64(),
    "shm_total_bytes": pa.int64()}
_CLOCK_TYPES = {"clock_map_source": pa.string(), "clock_map_samples": pa.int64(),
                "clock_map_wall_span_s": pa.float64(), "clock_map_sim_span_s": pa.float64()}
_ARROW = {INTEGER: pa.int64(), REAL: pa.float64(), TEXT: pa.string(), UNKNOWN: pa.null()}

#: Column notes, for the catalog.
NOTES = {
    RUN_LOG: {
        "seq": "the merge's own order within the run: ORDER BY seq, never by storage order",
        "sim_time": ("NULL where the clock map cannot answer -- before the simulator "
                     "published /clock and after it stopped. Nothing is extrapolated."),
        "in_window": ("0 for a line outside the run's own trial window: its bring-up, "
                      "verdict and teardown, or the reset before it in a packed job"),
    },
    RUN_CLOCK: {
        "clock_map_source": ("'none' means this run's log lines have no sim time at all; "
                             "the other values name the producer of the map"),
        "clock_map_sim_span_s": ("clock_map_sim_span_s / clock_map_wall_span_s is the run's "
                                 "realtime factor"),
    },
}


@dataclass
class JobRun:
    """One run of a job, as the derivation needs it."""
    key: str
    config_name: str
    run_id: int
    path: str


@dataclass
class Derivation:
    """What a job's derivation produced: ``{table: {run key: rows}}`` and what it could not do."""
    tables: Dict[str, Dict[str, pa.Table]] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)


def _typed(rows: Sequence[dict], types: Dict[str, pa.DataType], context: dict) -> pa.Table:
    """*rows* as a table with *types*; an empty string is NULL, as it always was."""
    arrays = {key: pa.array([value] * len(rows), type=pa.int64() if key == "run_id"
                            else pa.string()) for key, value in context.items()}
    for column, kind in types.items():
        values = [row.get(column) for row in rows]
        values = [None if value == "" else value for value in values]
        arrays[column] = pa.array(values, type=kind)
    return pa.table(arrays)


def _inferred(rows: Sequence[dict], leading: Dict[str, pa.DataType], columns: Sequence[str],
              verdicts: dict, context: dict) -> pa.Table:
    """*rows* with *leading* columns typed as given and *columns* typed by *verdicts*."""
    table = _typed(rows, leading, context)
    for column in columns:
        verdict = verdicts.get(column, UNKNOWN)
        table = table.append_column(column, pa.array(
            [stored_value(row.get(column), verdict) for row in rows], type=_ARROW[verdict]))
    return table


def _job_rows(campaign_dir: str, manifest: dict, table: str, keys: Sequence[str]) -> List[dict]:
    """The rows *table* holds for any of *keys* (the job's own key, or its one run's)."""
    runs = manifest.get("tables", {}).get(table, {}).get("runs", {})
    rows: List[dict] = []
    root = cache_root(campaign_dir)
    for key in keys:
        for rel in (runs.get(key) or {}).get("files") or []:
            rows.extend(pq.read_table(os.path.join(root, rel)).to_pylist())
    return rows


def _verdict(rows: Sequence[dict]) -> Optional[dict]:
    """The run's terminal verdict from its ``run_log`` rows.

    ``timestamp`` is *sim* time, because that is what the column means to every reader: the
    run view unions this table into the playback range, so a wall-epoch value here stretches
    the timeline to 1.8e9 seconds. ``wall_ts`` is the same event on the other clock and is not
    redundant -- the clock map does not extrapolate, so a run whose ``/clock`` stopped at
    shutdown has a NULL ``sim_time`` on every line after it, sometimes the verdict itself.
    """
    for row in rows:
        message = row.get("message") or ""
        status = scenario_markers.verdict_of(message, row.get("node") or "")
        if status:
            return {"timestamp": row.get("sim_time"), "wall_ts": row.get("wall_ts"),
                    "status": status, "message": message}
    return None


def derive_job(campaign_dir: str, campaign_id: str, job_dir: Optional[str],
               runs: Sequence[JobRun], tables: Sequence[str], manifest: dict,
               input_keys: Sequence[str], containers: Optional[Sequence[str]]) -> Derivation:
    """Build *tables* for every run of one job.

    *input_keys* are the manifest keys the job's ``rosout`` and ``clock_map`` rows are
    recorded under; *containers* are the campaign's runtime container names, or ``None``
    when its configuration records none.
    """
    out = Derivation()
    wanted = set(tables)
    stats = run_slices.SliceStats()
    clock = clock_map.from_rows(_job_rows(campaign_dir, manifest, "clock_map", input_keys))
    slices = run_slices.job_slices(job_dir or "", [(r.config_name, r.path) for r in runs],
                                   clock, stats)
    by_key = {f"{r.config_name}/{r.run_id}": r for r in runs}
    contexts = {s.job_name: {"campaign_id": campaign_id, "config_name": s.config_name,
                             "run_id": s.run_id} for s in slices}

    if wanted & {RUN_LOG, SCENARIO_TIMESTAMPS}:
        merge = run_log.MergeStats()
        sole = (MAIN_CONTAINER if containers is not None and len(containers) == 1 else None)
        records = (run_log.collect_job_records(
            job_dir, _job_rows(campaign_dir, manifest, "rosout", input_keys), merge,
            sole_container=sole) if job_dir else [])
        markers = [r.wall_ts for r in records if r.wall_ts is not None
                   and scenario_markers.is_scenario_start(r.message)
                   and scenario_markers.is_own_logger(r.node)]
        snapped = run_slices.log_claims_from_markers(
            [(s.job_name, s.start_epoch) for s in slices], markers)
        for slice_ in slices:
            start, end = (snapped[slice_.job_name] if snapped
                          else (slice_.log_claim_start, slice_.log_claim_end))
            rows = run_log.rows_for_window(
                [r for r in records if run_slices.claims_log(r.wall_ts, start, end)],
                slice_.clock, start_epoch=slice_.start_epoch, end_epoch=slice_.end_epoch)
            context = contexts[slice_.job_name]
            if RUN_LOG in wanted:
                out.tables.setdefault(RUN_LOG, {})[slice_.job_name] = _typed(
                    rows, _RUN_LOG_TYPES, context)
            verdict = _verdict(rows)
            if SCENARIO_TIMESTAMPS in wanted and verdict:
                out.tables.setdefault(SCENARIO_TIMESTAMPS, {})[slice_.job_name] = _typed(
                    [verdict], _VERDICT_TYPES, context)

    if RESOURCE_USAGE in wanted:
        scan = resource_usage.ScanStats()
        ticks = (resource_usage.collect_job_ticks(
            job_dir, resource_usage.expected_container_files(containers), scan,
            os.path.relpath(job_dir, campaign_dir)) if job_dir else [])
        for slice_ in slices:
            out.tables.setdefault(RESOURCE_USAGE, {})[slice_.job_name] = _typed(
                resource_usage.rows_for_slice(ticks, slice_), _RESOURCE_TYPES,
                contexts[slice_.job_name])
        out.notes.extend(_scan_notes(scan))

    if SYSTEM_USAGE in wanted:
        columns, samples = system_usage.collect_job_rows(job_dir) if job_dir else ([], [])
        per_run = {s.job_name: system_usage.rows_for_slice(columns, samples, s) for s in slices}
        verdicts = infer_column_types([r for rows in per_run.values() for r in rows], columns)
        leading = {"timestamp": pa.float64(), "wall_ts": pa.float64(), "in_window": pa.int64(),
                   "container": pa.string()}
        for name, rows in per_run.items():
            out.tables.setdefault(SYSTEM_USAGE, {})[name] = _inferred(
                rows, leading, columns, verdicts, contexts[name])

    if RUN_CLOCK in wanted:
        for slice_ in slices:
            info = slice_.clock.info
            out.tables.setdefault(RUN_CLOCK, {})[slice_.job_name] = _typed(
                [{"clock_map_source": info.source, "clock_map_samples": info.samples,
                  "clock_map_wall_span_s": info.wall_span_s,
                  "clock_map_sim_span_s": info.sim_span_s}],
                _CLOCK_TYPES, contexts[slice_.job_name])

    for key in by_key:
        if job_dir is None:
            out.notes.append(f"{key}: no job-link entry, so no job artifacts -- its log and "
                             "resource tables are empty")
        if key in stats.without_clock:
            out.notes.append(f"{key}: no clock map -- its derived tables have wall time only")
        if key in stats.unplaceable:
            out.notes.append(f"{key}: no test.xml in a packed job, so it claims no samples")
    return out


def _scan_notes(scan: resource_usage.ScanStats) -> List[str]:
    notes = []
    for label, items in (("no resource CSV for", scan.missing),
                         ("empty resource CSV for", scan.empty),
                         ("truncated resource CSV for", scan.truncated),
                         ("unreadable resource CSV for", scan.unreadable),
                         ("resource CSV from an undeclared container", scan.unexpected)):
        notes.extend(f"{label} {item}" for item in items)
    return notes


__all__ = ["DERIVED", "Derivation", "INPUTS", "JobRun", "NOTES", "RESOURCE_USAGE", "RUN_CLOCK",
           "RUN_LOG", "SCENARIO_TIMESTAMPS", "SYSTEM_USAGE", "derive_job"]
