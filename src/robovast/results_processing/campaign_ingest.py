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

"""A campaign's results directory, read into the central index.

This is what replaced the per-campaign ``data.db`` writer: the same walk, the same
one-file-one-table rule, the same type inference -- and then ``COPY`` into Postgres rather than a 1.1 GB
SQLite file written, uploaded, and downloaded again on the first cold query.

**The CSV boundary stays, and that is deliberate.** An earlier plan had the rosbag handlers
write rows straight to the index. They cannot: ``rosbags_process.py`` is copied as a loose
file into ``_transient/`` and mounted at ``/config`` (``common/execution.py``), so it runs
as a standalone script with only what the ROS image provides and can import nothing from
this package. Reaching the sink from there would mean installing the postprocessing stack
into the campaign's execution image -- which is the same thing this design refuses for
in-job conversion, because a plugin's dependency conflict would then become a campaign-run
failure rather than a postprocessing one.

So the split is the one the cluster already has: stage 1 decodes bags to CSV in the
execution image, stage 2 -- here, where robovast is installed -- reads those files and
loads them. What is saved is the whole ``data.db`` materialisation, which was the expensive
half anyway: measured on a real campaign, ``COPY`` was 0.35 s of a 4.6 s ingest, and the
read dominated.

**One file, one table, still with nothing registered.** Drop a ``*.csv`` or ``*.jsonl`` in a
run directory and it becomes a table named after its stem. That is how a scenario, a
simulator, or a third-party plugin contributes data, and it is the documented
middleware-neutral seam; nothing about it changes here.
"""

import csv
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from robovast.common import campaign_data, execution, scenario_markers
from robovast.common.campaign_data import list_config_dirs, list_run_dirs
from robovast.common.quantity import to_bytes
from robovast.results_processing import (clock_map, dimension_ingest, index_schema,
                                         resource_usage)
from robovast.results_processing.csv_types import INTEGER, REAL, TEXT, UNKNOWN, widen
# Reused rather than reimplemented: these decide what a data file *is* -- the JSONL format
# registry, the yaw derivation, the table-name rule -- and a second copy of any of them
# would be a second answer to "what does this file contain?". Now that the ``data.db``
# writer is gone this is their only caller; they still live in ``postprocessing_plugins``
# beside the plugins that produce the files they read.
from robovast.results_processing.postprocessing_plugins import (_as_float, _csv_to_table_name,
                                                                _read_table_rows)
from robovast.results_processing.row_sink import PostgresRowSink

logger = logging.getLogger(__name__)

#: Derived here rather than by a plugin, and read by every surface that has to know when a
#: trial ended -- the playback clock, the log views, ``search_run_logs``. One answer, rather
#: than a text match repeated per reader; the recognition itself lives in
#: :mod:`robovast.common.scenario_markers`.
SCENARIO_TIMESTAMPS_TABLE = "scenario_timestamps"

#: Records display name -> SQL table name, so a reader can find the table a file became
#: after the name was sanitised.
TABLE_NAME_MAP = "_table_name_map"


def _data_files(run_dir: Path) -> list:
    """The data files in *run_dir*, sorted, CSV and JSONL alike.

    JSONL sits alongside CSV because a run's behaviour-tree log is written directly by
    scenario_execution -- no rosbag involved, so it exists for a non-ROS run too.
    """
    return sorted(list(run_dir.rglob("*.csv")) + list(run_dir.rglob("*.jsonl")))


def _scenario_verdict(rows) -> dict:
    """The run's terminal verdict from its ``run_log`` rows, or ``{}``.

    ``timestamp`` is *sim* time, because that is what the column means to every reader: the
    run view unions this table into the playback range, so a wall-epoch value here stretches
    the timeline to 1.8e9 seconds and the progress bar becomes unusable. ``wall_ts`` is the
    same event on the other clock and is not redundant -- the clock map does not
    extrapolate, so a run whose ``/clock`` stopped at shutdown has a NULL ``sim_time`` on
    every line after it, sometimes including the verdict itself.
    """
    for row in rows:
        message = str(row.get("message", ""))
        status = scenario_markers.verdict_of(message, str(row.get("node", "")))
        if not status:
            continue
        return {"timestamp": _as_float(row.get("sim_time")),
                "wall_ts": _as_float(row.get("wall_ts")),
                "status": status, "message": message}
    return {}


def ingest_run(sink, run_dir: Path, config_name: str, run_id, *,
               name_map: dict = None) -> dict:
    """Load one run directory's data files; return rows written per table.

    A stem appearing twice in one run is a hard error, as it was for ``data.db``: two files
    claiming one table means one silently wins, and which one depends on directory order.
    """
    written = {}
    seen = {}
    verdict = {}
    for path in _data_files(run_dir):
        stem = path.stem
        table = _csv_to_table_name(path.name)
        if stem in seen:
            raise ValueError(
                f"Duplicate table name '{stem}' in run {run_id} of config "
                f"'{config_name}': '{path.relative_to(run_dir)}' conflicts with "
                f"'{seen[stem].relative_to(run_dir)}'")
        seen[stem] = path
        if name_map is not None:
            name_map[stem] = table

        rows = _read_table_rows(path)
        if not rows:
            continue
        if stem == "run_log" and not verdict:
            verdict = _scenario_verdict(rows)

        context = {"config_name": config_name, "run_id": run_id}
        written[table] = sink.write(table, rows, context=context,
                                    source=f"{config_name}/{run_id}/{path.name}")

    if verdict:
        written[SCENARIO_TIMESTAMPS_TABLE] = sink.write(
            SCENARIO_TIMESTAMPS_TABLE, [verdict],
            context={"config_name": config_name, "run_id": run_id},
            types={"timestamp": REAL, "wall_ts": REAL, "status": TEXT, "message": TEXT},
            source=f"{config_name}/{run_id}")
    return written


#: The dimension table an analysis joins its metrics against on ``(config_name, run_id)``.
#: One row per run directory, plus one run-less row per composition-failed unit.
RUNS_TABLE = "runs"

#: The fixed columns of :data:`RUNS_TABLE`, in order, with their verdicts. ``config_name``
#: and ``run_id`` are absent because they are :data:`index_schema.CONTEXT_COLUMNS` -- the
#: sink writes them for every table, and declaring them twice would let this list disagree
#: with the schema about the join key's type.
#:
#: ``available_cpus`` is REAL and not INTEGER on purpose: ``execution.resources.cpu`` accepts
#: fractional cores, and an INTEGER column truncates a 0.5-core reservation to 0, which then
#: reads as "this run had no CPU" in every query joining to it.
_RUNS_COLUMNS = (
    ("status", TEXT), ("passed", INTEGER), ("duration_s", REAL), ("errors", INTEGER),
    ("failures", INTEGER), ("objective", REAL),
    ("start_time", TEXT), ("end_time", TEXT),
    # ``node_label`` is the machine, ``instance_type`` its kind. Both, because neither
    # answers the other: on a cloud cluster the kind is what runs compare across, and on
    # bare metal it is the same string for every node, so only the machine separates a slow
    # one from a fast one. It is a *hash* of the node's name (see ``collect_sysinfo``), and
    # deliberately not called ``node_name`` -- that already means a behaviour-tree node in
    # ``nav2_behavior_tree``.
    ("instance_type", TEXT), ("node_label", TEXT), ("cpu_name", TEXT),
    ("available_cpus", REAL), ("available_mem_bytes", INTEGER),
    # The run's shared-memory pool: what it peaked at, and the limit in force. Beside
    # ``available_mem_bytes`` because it is the same kind of fact and the wrong one to read
    # alone -- that column is process memory, while the pool is a tmpfs charged to the pod on
    # top of it. NULL means unmeasured, which is not "used none of it".
    ("shm_peak_bytes", INTEGER), ("shm_limit_bytes", INTEGER),
    # What this run's log timestamps are worth. The two spans are the same window on each
    # clock, so their ratio is the simulated seconds bought per wall second.
    ("clock_map_source", TEXT), ("clock_map_samples", INTEGER),
    ("clock_map_wall_span_s", REAL), ("clock_map_sim_span_s", REAL),
    # A SEPARATE column, never folded into ``status``: see the note attached below.
    ("probed", INTEGER),
)

#: Reattached here because the column always deserves the warning, and the writer that used
#: to carry it went with ``data.db``.
_PROBED_NOTE = (
    "1 when a human read into this run WHILE IT RAN, which is a fact about the run's "
    "provenance and not about its outcome -- a probed run can still pass, so this is a "
    "separate column and never folded into status. Exclude these rows from anything a "
    "published number rests on. Granularity follows the job: with runs_per_job > 1 the "
    "whole packed job is marked, since which of its runs was in flight cannot be "
    "recovered -- it over-excludes rather than admitting a perturbed run.")


def _end_time(start_iso, duration_sec):
    """ISO end time = start + duration, or None when either is unavailable.

    Derived rather than stored: nothing on disk records when a run stopped, and a reader
    that needs it would otherwise recompute this join per query.
    """
    if not start_iso or duration_sec is None:
        return None
    try:
        return (datetime.fromisoformat(str(start_iso))
                + timedelta(seconds=float(duration_sec))).isoformat()
    except (ValueError, TypeError):
        return None


def _sysinfo_fields(outcome: dict) -> tuple:
    """``(instance_type, node_label, cpu_name, available_cpus, available_mem_bytes)``.

    Prefers ``campaign.db``'s ``job.sysinfo_json``, which the store already resolved once at
    execution time across that file's three historical locations, so this cannot disagree
    with ``campaign.job`` the way a second independent read could. Also accepts the
    ``sysinfo`` dict :func:`read_run_outcome` attaches on the fallback path, where a store
    too old to have a ``job`` table forces a read from disk.
    """
    info = outcome.get("sysinfo")
    if info is None:
        raw = outcome.get("sysinfo_json")
        if not raw:
            return None, None, None, None, None
        try:
            info = json.loads(raw) or {}
        except (TypeError, ValueError):
            return None, None, None, None, None
    # ``available_mem`` is the key sysinfo actually writes, and its value is a
    # Kubernetes-style quantity -- a plain byte count, or a suffixed string like "16Gi" when
    # the .vast set a limit. Normalised here so the column can be compared and averaged;
    # reading it raw would make it numeric in some runs and text in others.
    # ``node_label`` is absent for a local run and for a cluster run recorded before the pod
    # hashed one; both become NULL rather than "", so "we do not know which machine" is never
    # mistaken for a machine called "".
    return (info.get("instance_type"), info.get("node_label") or None, info.get("cpu_name"),
            info.get("available_cpus"), to_bytes(info.get("available_mem")))


def _max_bytes(current, value):
    """Fold one CSV cell into a running maximum, treating empty and unparsable as absent."""
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return current
    if parsed < 0:
        return current
    return parsed if current is None else max(current, parsed)


def _shm_info(campaign_path: Path, config_name: str, run_id: int) -> tuple:
    """``(peak used, limit in force)`` of this run's ``/dev/shm``, in bytes.

    ``(None, None)`` for a run recorded before the monitor sampled the pool, and for a
    runtime with no ``/dev/shm``. Both are *unmeasured*, which is not "used none of it" and
    must not be stored as 0 -- the column exists to explain a SIGBUS (exit 135), which
    ``available_mem_bytes`` (process memory) cannot.
    """
    path = campaign_path / config_name / str(run_id) / resource_usage.FILENAME
    used = total = None
    try:
        with open(path, newline="", encoding="utf-8", errors="replace") as handle:
            for row in csv.DictReader(handle):
                used = _max_bytes(used, row.get("shm_used_bytes"))
                total = _max_bytes(total, row.get("shm_total_bytes"))
    except OSError:
        return None, None
    return used, total


def _clock_map_info(campaign_path: Path, config_name: str, run_id: int):
    """What relates this run's wall-stamped log to sim time, and how well.

    A run with no map reports :data:`clock_map.SOURCE_NONE`, which is a finding rather than
    an error: its log is wall-time only.
    """
    try:
        job_dir = execution.job_artifact_dir(str(campaign_path), f"{config_name}/{run_id}")
    except (FileNotFoundError, OSError):
        job_dir = None
    if job_dir:
        found = clock_map.load_clock_map(os.path.join(job_dir, "logs", clock_map.FILENAME))
        if found:
            return found.info
    # Non-ROS: roqsim streams its own map beside the run's recording.
    return clock_map.find_run_clock_map(
        str(campaign_path / config_name / str(run_id))).info


def _read_units(store_path: Path) -> tuple:
    """``(params_by_config, objective_by_config, composition_failed)`` from ``campaign.db``.

    ``status`` and ``paramset_id`` are absent from a store predating them; the query retries
    with the columns every version has rather than losing every unit's params.
    """
    params_by_config: dict = {}
    objective_by_config: dict = {}
    composition_failed: list = []
    store = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
    try:
        try:
            rows = store.execute(
                "SELECT config_name, params_json, objective, status, paramset_id "
                "FROM unit").fetchall()
        except sqlite3.Error:
            rows = [(cn, pj, obj, None, None) for cn, pj, obj in store.execute(
                "SELECT config_name, params_json, objective FROM unit")]
        for config_name, params_json, objective, status, paramset_id in rows:
            try:
                params = json.loads(params_json) if params_json else {}
            except (TypeError, ValueError):
                params = {}
            if not isinstance(params, dict):
                params = {}
            if status == "composition_failed":
                # No config_name and no directory on disk: ``paramset_id`` is the only
                # identity such a draw has. Same rule as ``index_views.run_view``'s
                # UNION arm, which adds these units back for the same reason.
                composition_failed.append((config_name or str(paramset_id), params))
                continue
            if not config_name:
                continue
            params_by_config[config_name] = params
            objective_by_config[config_name] = objective
    except sqlite3.Error as exc:
        # A store with no ``unit`` table at all predates the search record. The runs are
        # still worth having; the params are simply not there to have.
        logger.warning("index: %s has no readable unit table (%s); runs get no params",
                       store_path, exc)
    finally:
        store.close()
    return params_by_config, objective_by_config, composition_failed


def _read_outcomes(store_path: Path) -> dict:
    """``{config_name: {run_id: outcome}}`` from ``campaign.db``'s ``run`` table.

    Empty for a store predating that table, in which case the caller re-parses each run's
    ``test.xml`` -- the path that also supplies ``sysinfo`` on such a store.

    ``LEFT JOIN job``: the host record is per *job*, shared by the runs of a packed
    multi-config job, and absent for a store written before the job table.
    """
    outcomes: dict = {}
    store = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
    try:
        for (config_name, run_id, status, passed, errors, failures, duration, start,
             sysinfo) in store.execute(
                "SELECT u.config_name, r.run_id, r.status, r.passed, r.errors, "
                "r.failures, r.duration_s, r.start_time, j.sysinfo_json "
                "FROM run r JOIN unit u ON r.unit_id = u.id "
                "LEFT JOIN job j ON r.job_id = j.id"):
            outcomes.setdefault(config_name, {})[run_id] = {
                "status": status, "passed": passed, "errors": errors,
                "failures": failures, "duration_s": duration, "start_time": start,
                "sysinfo_json": sysinfo}
    except sqlite3.Error:
        pass  # v1 store: no ``run``/``job`` table -- the caller falls back to test.xml.
    finally:
        store.close()
    return outcomes


def _param_types(param_keys, param_sources) -> dict:
    """``{param_<key>: verdict}``, inferred from the values across every unit.

    This is what makes ``ORDER BY param_wind_strength`` and ``WHERE param_speed > 0.5`` mean
    what they say instead of comparing strings. A list or dict is JSON-encoded first, so it
    widens to text exactly as it is stored.
    """
    types = {}
    for key in param_keys:
        verdict = UNKNOWN
        for params in param_sources:
            if key not in params:
                continue
            value = params[key]
            verdict = widen(
                verdict,
                json.dumps(value) if isinstance(value, (list, dict)) else value)
        types[f"param_{key}"] = verdict
    return types


def build_runs_table(sink, campaign_dir: str) -> int:
    """Write the ``runs`` dimension table for one campaign; return rows written.

    One row per run directory under the campaign -- **not** per row in ``campaign.db``'s
    ``run`` table -- plus one run-less row per composition-failed unit. Keyed on
    ``(config_name, run_id)``, the join key every metric table shares, so "how does <param>
    affect <metric>" is one query.

    ``campaign.db`` is the operational source of truth for status and duration, written live
    from each ``test.xml``; a store predating its ``run`` table falls back to re-parsing that
    ``test.xml``, which is also what supplies ``sysinfo`` there. Both paths must stay.
    """
    root = Path(campaign_dir)
    store_path = root / "campaign.db"
    params_by_config: dict = {}
    objective_by_config: dict = {}
    composition_failed: list = []
    outcomes: dict = {}
    if store_path.is_file():
        params_by_config, objective_by_config, composition_failed = _read_units(store_path)
        outcomes = _read_outcomes(store_path)

    fixed = dict(_RUNS_COLUMNS)
    # Every unit's keys, so a run whose params differ from its siblings still gets every
    # sibling's column, NULL where it has no value -- the table is one shape for the whole
    # campaign. A key whose ``param_`` name would collide with a fixed column is skipped.
    param_sources = [*params_by_config.values(), *(p for _, p in composition_failed)]
    param_keys = sorted({k for p in param_sources for k in p
                         if f"param_{k}" not in fixed
                         and f"param_{k}" not in dict(index_schema.CONTEXT_COLUMNS)})
    types = {**fixed, **_param_types(param_keys, param_sources)}

    # Resolved once per campaign: the ledger is absent for every campaign nobody touched,
    # and then no manifest is read at all.
    probed = campaign_data.probed_runs(root)

    rows = []
    for config_dir in campaign_data.list_config_dirs(root):
        config_name = Path(config_dir).name
        params = params_by_config.get(config_name, {})
        objective = objective_by_config.get(config_name)
        for run_dir in campaign_data.list_run_dirs(config_dir):
            run_path = Path(run_dir)
            run_id = int(run_path.name)
            outcome = (outcomes.get(config_name, {}).get(run_id)
                       or campaign_data.read_run_outcome(run_path, root))
            instance_type, node_label, cpu_name, cpus, mem = _sysinfo_fields(outcome)
            clock = _clock_map_info(root, config_name, run_id)
            shm_peak, shm_limit = _shm_info(root, config_name, run_id)
            start_time = outcome["start_time"]
            duration = outcome["duration_s"]
            row = {
                "config_name": config_name, "run_id": run_id,
                "status": outcome["status"], "passed": outcome["passed"],
                "duration_s": duration, "errors": outcome["errors"],
                "failures": outcome["failures"], "objective": objective,
                "start_time": start_time, "end_time": _end_time(start_time, duration),
                "instance_type": instance_type, "node_label": node_label,
                "cpu_name": cpu_name, "available_cpus": cpus,
                "available_mem_bytes": mem,
                "shm_peak_bytes": shm_peak, "shm_limit_bytes": shm_limit,
                "clock_map_source": clock.source, "clock_map_samples": clock.samples,
                "clock_map_wall_span_s": clock.wall_span_s,
                "clock_map_sim_span_s": clock.sim_span_s,
                "probed": 1 if f"{config_name}/{run_id}" in probed else 0,
            }
            row.update({f"param_{k}": params.get(k) for k in param_keys})
            rows.append(row)

    # The draws that never became a configuration. One row each, ``run_id`` NULL (there is
    # no run to number) and every run-derived column NULL -- the parameters are the whole
    # point: they are what the search proposed and what turned out to be unrealizable. A
    # campaign that could not build half of what it proposed must not read as one that
    # proposed less.
    for identity, params in composition_failed:
        row = {c: None for c in types}
        row.update({"config_name": identity, "run_id": None,
                    "status": "composition_failed", "passed": 0, "probed": 0})
        row.update({f"param_{k}": params.get(k) for k in param_keys})
        rows.append(row)

    written = sink.write(RUNS_TABLE, rows, context={"config_name": None, "run_id": None},
                         types=types, source=f"{root.name}/campaign.db")
    return written


def ingest_campaign(conn, campaign_dir: str, campaign_id: str) -> dict:
    """Load a whole campaign -- its record and its data files -- into the index.

    Returns ``{table: rows}``. Idempotent at the campaign level: the record is rewritten
    (see :func:`~robovast.results_processing.dimension_ingest.mirror_campaign_record`) and
    metric rows for this campaign are cleared first, so re-ingesting after a re-postprocess
    lands the same rows rather than doubling them.
    """
    root = Path(campaign_dir)
    totals = {}
    name_map: dict = {}

    # Before anything is written, so a re-ingest replaces rather than doubles. Scoped to
    # this campaign: the others in the index are not this operation's business.
    cleared = index_schema.clear_campaign(conn, campaign_id)
    if cleared:
        logger.info("index: cleared %s rows for %s before re-ingest",
                    sum(cleared.values()), campaign_id)

    store = root / "campaign.db"
    if store.is_file():
        totals.update(dimension_ingest.mirror_campaign_record(conn, str(store), campaign_id))
    else:
        # Not fatal: a campaign whose record is missing still has its measurements, and
        # refusing here would make the index unable to hold exactly the campaigns that most
        # need reading -- the ones that ended badly.
        logger.warning("index: %s has no campaign.db; ingesting its data only", campaign_dir)

    sink = PostgresRowSink(conn, campaign_id=campaign_id)

    # The dimension table every analysis joins its metrics against, built before them so a
    # partial ingest still says which runs exist. ``campaign.db`` is optional here for the
    # same reason it is above: a campaign that ended badly still has runs on disk.
    totals[RUNS_TABLE] = build_runs_table(sink, str(root))
    index_schema.record_note(conn, RUNS_TABLE, "probed", _PROBED_NOTE,
                             kind=index_schema.NOTE_DOC)

    for config_dir in list_config_dirs(str(root)):
        config_name = Path(config_dir).name
        for run_dir in list_run_dirs(config_dir):
            run_path = Path(run_dir)
            written = ingest_run(sink, run_path, config_name, int(run_path.name),
                                 name_map=name_map)
            for table, count in written.items():
                totals[table] = totals.get(table, 0) + count

    if name_map:
        sink.write(TABLE_NAME_MAP,
                   [{"display_name": d, "sql_name": s} for d, s in sorted(name_map.items())],
                   context={"config_name": None, "run_id": None},
                   types={"display_name": TEXT, "sql_name": TEXT},
                   source=campaign_id)

    # Recorded even when the campaign produced no rows at all: "ingested and empty" is a
    # different answer from "never ingested", and only the registry can tell them apart.
    index_schema.record_campaign(conn, campaign_id)

    logger.info("index: ingested %s (%s)", campaign_id,
                ", ".join(f"{t}={n}" for t, n in sorted(totals.items())) or "nothing")
    return totals
