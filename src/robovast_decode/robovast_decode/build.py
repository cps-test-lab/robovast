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

"""Build a campaign's tables from its recordings, for the runs and tables asked for.

A table is built for a run the first time something names it, and kept: the manifest records
which runs each table is built for, from which source bytes and by which decoder, so a later
request builds only what is missing -- a run that has grown since, a table not yet asked for,
or anything a different decoder version wrote.

Where recordings live, relative to the campaign directory:

* a run's scenario recording: ``<config>/<run>/rosbag2/`` (the last attempt, when a recorder
  that restarted left several ``rosbag2*`` directories);
* a job's wall-time infrastructure recording: ``_jobs/.../job-N/logs/rosout_bag/``, the job
  found through ``_transient/job_links.yaml``, which is written before a job starts (the
  ``job`` symlink beside a run appears only once it ends). A job that ran one run gives its
  rows to that run; a job
  that ran several leaves ``config_name`` and ``run_id`` empty, and its file is named after the
  job, because which of its runs a row belongs to is a question of time windows.

A run's own ``*.csv`` and ``*.jsonl`` files are tables too, named after the file
(:mod:`robovast_decode.authored`).

Every table carries ``campaign_id``, ``config_name`` and ``run_id`` in its own rows, so a set of
parquet files is a table without anything else to join it to.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

import yaml

from . import __version__, clock_map, run_slices
from .authored import RaggedFile, read_rows, run_files, to_arrow, with_yaw
from .decode import decode_bag, segments
from .derived import DERIVED, INPUTS, JobRun, derive_job
from .framing import Channel, McapTail
from .handlers import Videos
from .layout import job_links, run_dirs
from .registry import INFRA_BAG, SCENARIO_BAG, narrow, plan_for
from .tables import (TableBuffer, fixed, manifest_lock, read_manifest, record_run_absent,
                     record_run_table, run_table_path, write_manifest, write_table)

#: The report of what a recording holds, as a table of its own.
RECORDING_TABLE = "_recording"

#: Tables written for the whole campaign by its campaign-end pass rather than built per run
#: here: the health checks' verdicts and the record of how each table was made.
CAMPAIGN_TABLES = frozenset({"run_health", "postprocessing_steps"})

#: Tables built from a campaign's records rather than read from a file of that name: a run's
#: data file claiming one is refused, since its rows and the built ones would be one table.
DERIVED_TABLES = frozenset({RECORDING_TABLE, "runs", *CAMPAIGN_TABLES, *DERIVED})
RECORDING_FIELDS = ["recording", "topic", "type", "messages", "bytes", "table", "reason"]

_ATTEMPT = re.compile(r"^rosbag2(?:_\d{4}_\d{2}_\d{2}-\d{2}_\d{2}_\d{2})?$")


@dataclass
class Run:
    config_name: str
    run_id: int
    path: str
    job_dir: Optional[str]

    @property
    def key(self) -> str:
        return f"{self.config_name}/{self.run_id}"


@dataclass
class BuildReport:
    built: Dict[str, List[str]] = field(default_factory=dict)      # table -> run keys
    skipped: Dict[str, List[str]] = field(default_factory=dict)    # table -> run keys current
    failed: Dict[str, Dict[str, str]] = field(default_factory=dict)  # table -> run -> reason
    unknown: List[str] = field(default_factory=list)
    #: What a derivation could not do without failing: a container that recorded nothing, a
    #: run with no clock map. Findings about the records, reported with the build.
    notes: List[str] = field(default_factory=list)


def find_runs(campaign_dir: str) -> List[Run]:
    """Every run directory of *campaign_dir*: ``<config>/<numeric run>``, with its job."""
    links = job_links(campaign_dir)
    runs = []
    for config, run_id in run_dirs(campaign_dir):
        path = os.path.join(campaign_dir, config, str(run_id))
        target = links.get(f"{config}/{run_id}/job")
        job = os.path.normpath(os.path.join(path, target)) if target else None
        runs.append(Run(config, run_id, path, job if job and os.path.isdir(job) else None))
    return runs


def scenario_recording(run: Run) -> Optional[str]:
    """The run's scenario recording: its last attempt, by start time, then by name."""
    attempts = [os.path.join(run.path, d) for d in os.listdir(run.path)
                if _ATTEMPT.match(d) and os.path.isdir(os.path.join(run.path, d))]
    if not attempts:
        return None

    def start(path):
        meta = os.path.join(path, "metadata.yaml")
        if os.path.isfile(meta):
            try:
                with open(meta, encoding="utf-8") as fh:
                    info = yaml.safe_load(fh)["rosbag2_bagfile_information"]
                return (0, info["starting_time"]["nanoseconds_since_epoch"], path)
            except (OSError, KeyError, TypeError, yaml.YAMLError):
                pass
        return (1, 0, path)
    return sorted(attempts, key=start)[-1]


def recorded_topics(bag_dir: str) -> Dict[str, str]:
    """``{topic: type}`` of a recording, from its ``metadata.yaml`` or its channel records."""
    meta = os.path.join(bag_dir, "metadata.yaml")
    if os.path.isfile(meta):
        try:
            with open(meta, encoding="utf-8") as fh:
                info = yaml.safe_load(fh)["rosbag2_bagfile_information"]
            return {t["topic_metadata"]["name"]: t["topic_metadata"]["type"]
                    for t in info.get("topics_with_message_count", [])}
        except (OSError, KeyError, TypeError, yaml.YAMLError):
            pass
    topics: Dict[str, str] = {}
    for path in segments(bag_dir):
        tail = McapTail(path)
        for record in tail.read():
            if isinstance(record, Channel):
                schema = tail.schemas.get(record.schema_id)
                topics.setdefault(record.topic, schema.name if schema else "")
    return topics


def _source_size(bag_dir: str) -> int:
    return sum(os.path.getsize(p) for p in segments(bag_dir))


def _complete(run: Run, bag_dir: str) -> bool:
    """A run's table is final once the run wrote its verdict and the recorder closed the bag."""
    return (os.path.isfile(os.path.join(run.path, "test.xml"))
            and os.path.isfile(os.path.join(bag_dir, "metadata.yaml")))


def _groups(config: Optional[dict]) -> Dict[str, list]:
    return {g["bag_dir"]: list(g.get("plugins") or []) for g in (config or {}).get("groups", [])}


def build(campaign_dir: str, tables: Optional[Iterable[str]] = None,
          runs: Optional[Iterable[str]] = None, config: Optional[dict] = None,
          force: bool = False) -> BuildReport:
    """Build *tables* (every one the recordings can give, for ``None``) for *runs*.

    *runs* are ``config/run`` keys (every run, for ``None``); *config* is the campaign's
    decoder configuration (``{"groups": [...]}``). A table already built for a run from the
    same source bytes by this decoder version is left alone unless *force*.
    """
    campaign_dir = os.path.abspath(campaign_dir)
    campaign_id = os.path.basename(campaign_dir)
    wanted_runs = set(runs) if runs is not None else None
    derived_wanted = [t for t in DERIVED if tables is None or t in tables]
    wanted_tables = None
    if tables is not None:
        # The derived tables are built after the recordings, from the job's inputs.
        wanted_tables = [t for t in tables if t not in DERIVED]
        if derived_wanted:
            wanted_tables += [t for t in INPUTS if t not in wanted_tables]
    groups = _groups(config)
    report = BuildReport()
    all_runs = find_runs(campaign_dir)
    runs_of_job: Dict[str, List[Run]] = {}
    for run in all_runs:
        if run.job_dir:
            runs_of_job.setdefault(run.job_dir, []).append(run)

    selected = [r for r in all_runs if wanted_runs is None or r.key in wanted_runs]
    if wanted_runs is not None:
        missing = wanted_runs - {r.key for r in selected}
        if missing:
            raise KeyError(f"no such run in {campaign_id}: {', '.join(sorted(missing))}")

    known_tables: set = set()
    done_jobs: set = set()
    for run in selected:
        sources = [(SCENARIO_BAG, scenario_recording(run), run)]
        if run.job_dir and run.job_dir not in done_jobs:
            done_jobs.add(run.job_dir)
            infra = os.path.join(run.job_dir, INFRA_BAG)
            sources.append((INFRA_BAG, infra if os.path.isdir(infra) else None, run))
        sources = [(role, bag_dir, owner) for role, bag_dir, owner in sources if bag_dir]
        manifest = read_manifest(campaign_dir)
        sizes = {os.path.relpath(b, campaign_dir): _source_size(b) for _, b, _ in sources}
        report_current = not force and _is_current(manifest, RECORDING_TABLE, run.key,
                                                   sum(sizes.values()))
        recording_rows = TableBuffer(RECORDING_TABLE)
        run_bag_tables: set = set()
        for role, bag_dir, owner in sources:
            recorded = recorded_topics(bag_dir)
            plan = plan_for(role, recorded, groups.get(role))
            known_tables.update(plan.tables)
            run_bag_tables.update(plan.tables)
            handlers, _unknown = narrow(plan, wanted_tables)
            for handler in handlers:
                if isinstance(handler, Videos):
                    handler.output_dir = owner.path
                    handler.bag_name = os.path.basename(bag_dir)
            size = sizes[os.path.relpath(bag_dir, campaign_dir)]
            context = _context(campaign_id, role, owner, runs_of_job)
            run_key = context["key"]
            todo = []
            for handler in handlers:
                current = [t for t in handler.tables()
                           if not force and _is_current(manifest, t, run_key, size)]
                for table in current:
                    report.skipped.setdefault(table, []).append(run_key)
                if len(current) < len(handler.tables()):
                    todo.append(handler)
            # The recording report covers every topic, so a recording whose tables are all
            # current is still framed -- cheaply, nothing is deserialized -- when it is stale.
            decoded = decode_bag(bag_dir, todo) if (todo or not report_current) else None
            complete = _complete(owner, bag_dir)
            written = []
            for handler in todo:
                name = type(handler).__name__
                if name in decoded.failed:
                    for table in handler.tables():
                        if wanted_tables is None or table in wanted_tables:
                            report.failed.setdefault(table, {})[run_key] = decoded.failed[name]
                    continue
                for table, buf in handler.buffers.items():
                    if wanted_tables is not None and table not in wanted_tables:
                        continue
                    arrow = with_yaw(buf.to_arrow(handler.orders.get(table),
                                                  context=context["columns"]))
                    rel = run_table_path(campaign_dir, table, *context["path"])
                    write_table(campaign_dir, rel, arrow)
                    written.append((table, rel, arrow))
            if decoded and not report_current:
                _recording_rows(recording_rows, role, decoded, plan)
            with manifest_lock(campaign_dir):
                fresh = read_manifest(campaign_dir)
                for table, rel, arrow in written:
                    record_run_table(fresh, table, run_key, files=[rel], rows=arrow.num_rows,
                                     schema=arrow.schema,
                                     sources={os.path.relpath(bag_dir, campaign_dir): size},
                                     complete=complete)
                    report.built.setdefault(table, []).append(run_key)
                write_manifest(campaign_dir, fresh)
        if not report_current and sources:
            _write_recording(campaign_dir, campaign_id, run, recording_rows, sizes)
            report.built.setdefault(RECORDING_TABLE, []).append(run.key)
        elif sources:
            report.skipped.setdefault(RECORDING_TABLE, []).append(run.key)
        file_tables = _build_files(campaign_dir, campaign_id, run, wanted_tables, force,
                                   report, known_tables,
                                   reserved=run_bag_tables | DERIVED_TABLES)
        if wanted_tables is not None:
            _record_absent(campaign_dir, run, wanted_tables, report, sizes,
                           known=run_bag_tables | file_tables | {RECORDING_TABLE})
    if derived_wanted:
        _build_derived(campaign_dir, campaign_id, selected, runs_of_job, derived_wanted,
                       (config or {}).get("containers"), force, report)
    if wanted_tables is not None:
        report.unknown = [t for t in wanted_tables if t not in known_tables]
    return report


def _derived_sources(campaign_dir: str, job_dir: Optional[str], runs: List[Run],
                     manifest: dict, input_keys: List[str]) -> Dict[str, int]:
    """What a job's derived tables are built from, as ``{source: size or rows}``."""
    sources: Dict[str, int] = {}
    paths = []
    if job_dir and os.path.isdir(job_dir):
        paths += [os.path.join(job_dir, n) for n in os.listdir(job_dir)
                  if n.startswith(("resource_usage_", "system_usage_")) and n.endswith(".csv")]
        logs = os.path.join(job_dir, "logs")
        if os.path.isdir(logs):
            paths += [os.path.join(logs, n) for n in os.listdir(logs)
                      if run_slices.container_of(n) is not None and n.endswith(".log")]
    for run in runs:
        paths += [os.path.join(run.path, n) for n in os.listdir(run.path)
                  if n == "test.xml" or n.endswith(clock_map.ROQSIM_SUFFIX)]
    for path in sorted(paths):
        sources[os.path.relpath(path, campaign_dir)] = os.path.getsize(path)
    for table in INPUTS:
        entries = manifest.get("tables", {}).get(table, {}).get("runs", {})
        sources[f"table:{table}"] = sum((entries.get(k) or {}).get("rows", 0)
                                        for k in input_keys)
    return sources


def _build_derived(campaign_dir: str, campaign_id: str, selected: List[Run],
                   runs_of_job: Dict[str, List[Run]], tables: List[str], containers,
                   force: bool, report: BuildReport) -> None:
    """The derived tables of every job a selected run belongs to, for all of its runs."""
    jobs: Dict[str, List[Run]] = {}
    for run in selected:
        if run.job_dir:
            jobs.setdefault(run.job_dir, runs_of_job[run.job_dir])
        else:
            jobs.setdefault(run.path, [run])
    for key, runs in jobs.items():
        job_dir = key if runs[0].job_dir else None
        input_keys = ([runs[0].key] if len(runs) == 1
                      else [f"_jobs/{os.path.basename(job_dir)}"])
        manifest = read_manifest(campaign_dir)
        sources = _derived_sources(campaign_dir, job_dir, runs, manifest, input_keys)
        entries = {t: manifest.get("tables", {}).get(t, {}).get("runs", {}) for t in tables}
        if not force and all((entries[t].get(r.key) or {}).get("sources") == sources
                             and entries[t][r.key].get("decoder") == __version__
                             for t in tables for r in runs):
            for table in tables:
                report.skipped.setdefault(table, []).extend(r.key for r in runs)
            continue
        derivation = derive_job(campaign_dir, campaign_id, job_dir,
                                [JobRun(r.key, r.config_name, r.run_id, r.path) for r in runs],
                                tables, manifest, input_keys, containers)
        report.notes.extend(derivation.notes)
        complete = all(os.path.isfile(os.path.join(r.path, "test.xml")) for r in runs)
        written = []
        for table in tables:
            for run in runs:
                rows = derivation.tables.get(table, {}).get(run.key)
                if rows is None:
                    continue
                rel = run_table_path(campaign_dir, table, run.config_name, run.run_id)
                write_table(campaign_dir, rel, rows)
                written.append((table, run.key, rel, rows))
        with manifest_lock(campaign_dir):
            fresh = read_manifest(campaign_dir)
            done = set()
            for table, run_key, rel, rows in written:
                record_run_table(fresh, table, run_key, files=[rel], rows=rows.num_rows,
                                 schema=rows.schema, sources=sources, complete=complete)
                report.built.setdefault(table, []).append(run_key)
                done.add((table, run_key))
            for table in tables:
                for run in runs:
                    if (table, run.key) not in done:
                        # A run with no verdict line has no scenario_timestamps row: a table
                        # it has, and that came out empty.
                        record_run_absent(fresh, table, run.key, sources=sources,
                                          complete=complete, known=True)
            write_manifest(campaign_dir, fresh)


def _build_files(campaign_dir: str, campaign_id: str, run: Run, wanted_tables, force: bool,
                 report: BuildReport, known_tables: set, reserved) -> set:
    """The run's own ``*.csv``/``*.jsonl`` files as tables (:mod:`robovast_decode.authored`);
    the tables its files are, built or refused."""
    files = run_files(run.path, reserved=reserved)
    for table, reason in files.refused.items():
        known_tables.add(table)
        if wanted_tables is None or table in wanted_tables:
            report.failed.setdefault(table, {})[run.key] = reason
    manifest = read_manifest(campaign_dir)
    columns = {"campaign_id": campaign_id, "config_name": run.config_name,
               "run_id": run.run_id}
    complete = os.path.isfile(os.path.join(run.path, "test.xml"))
    written = []
    for table, path in files.tables.items():
        known_tables.add(table)
        if wanted_tables is not None and table not in wanted_tables:
            continue
        size = os.path.getsize(path)
        if not force and _is_current(manifest, table, run.key, size):
            report.skipped.setdefault(table, []).append(run.key)
            continue
        try:
            rows = read_rows(path)
        except (RaggedFile, OSError, ValueError) as exc:
            report.failed.setdefault(table, {})[run.key] = (
                f"{os.path.relpath(path, run.path)}: {exc}")
            continue
        if not rows:
            continue
        arrow = to_arrow(rows, context=columns)
        rel = run_table_path(campaign_dir, table, run.config_name, run.run_id)
        write_table(campaign_dir, rel, arrow)
        written.append((table, rel, arrow, os.path.relpath(path, campaign_dir), size))
    if written:
        with manifest_lock(campaign_dir):
            fresh = read_manifest(campaign_dir)
            for table, rel, arrow, source, size in written:
                record_run_table(fresh, table, run.key, files=[rel], rows=arrow.num_rows,
                                 schema=arrow.schema, sources={source: size},
                                 complete=complete)
                report.built.setdefault(table, []).append(run.key)
            write_manifest(campaign_dir, fresh)
    return set(files.tables) | set(files.refused)


def _record_absent(campaign_dir: str, run: Run, wanted_tables, report: BuildReport,
                   sizes: dict, known: set) -> None:
    """Enter the asked-for tables *run* has no rows for, and why where a build failed.

    *known* are the tables the run's records can give at all: a table outside it is one this
    run never had, which is a different answer from one it had and came out empty.
    """
    have = {t for t, keys in report.built.items() if run.key in keys}
    have |= {t for t, keys in report.skipped.items() if run.key in keys}
    missing = [t for t in wanted_tables if t not in have]
    if not missing:
        return
    complete = os.path.isfile(os.path.join(run.path, "test.xml"))
    with manifest_lock(campaign_dir):
        manifest = read_manifest(campaign_dir)
        for table in missing:
            reason = report.failed.get(table, {}).get(run.key)
            record_run_absent(manifest, table, run.key, sources=sizes, complete=complete,
                              reason=reason, known=table in known)
        write_manifest(campaign_dir, manifest)


def _context(campaign_id: str, role: str, owner: Run, runs_of_job: Dict[str, List[Run]]) -> dict:
    """The context columns and file location of one recording's rows."""
    if role == INFRA_BAG and owner.job_dir:
        job_runs = runs_of_job.get(owner.job_dir, [owner])
        job = os.path.basename(owner.job_dir)
        if len(job_runs) == 1:
            return {"key": owner.key, "path": (owner.config_name, owner.run_id),
                    "columns": {"campaign_id": campaign_id, "config_name": owner.config_name,
                                "run_id": owner.run_id}}
        return {"key": f"_jobs/{job}", "path": ("_jobs", job),
                "columns": {"campaign_id": campaign_id, "config_name": None, "run_id": None}}
    return {"key": owner.key, "path": (owner.config_name, owner.run_id),
            "columns": {"campaign_id": campaign_id, "config_name": owner.config_name,
                        "run_id": owner.run_id}}


def _is_current(manifest: dict, table: str, run_key: str, size: int) -> bool:
    entry = manifest.get("tables", {}).get(table, {}).get("runs", {}).get(run_key)
    if not entry or entry.get("decoder") != __version__:
        return False
    return sum(entry.get("sources", {}).values()) == size


def _recording_rows(buf: TableBuffer, role: str, decoded, plan) -> None:
    for topic, stats in sorted(decoded.topics.items()):
        table = plan.topic_table.get(topic)
        reason = stats.undecodable or plan.untabulated.get(topic)
        buf.add({"recording": role, "topic": topic, "type": stats.type,
                 "messages": stats.messages, "bytes": stats.bytes,
                 "table": None if reason else table, "reason": reason})


def _write_recording(campaign_dir: str, campaign_id: str, run: Run, buf: TableBuffer,
                     sizes: dict) -> None:
    arrow = buf.to_arrow(fixed(RECORDING_FIELDS),
                         context={"campaign_id": campaign_id, "config_name": run.config_name,
                                  "run_id": run.run_id})
    rel = run_table_path(campaign_dir, RECORDING_TABLE, run.config_name, run.run_id)
    write_table(campaign_dir, rel, arrow)
    with manifest_lock(campaign_dir):
        manifest = read_manifest(campaign_dir)
        record_run_table(manifest, RECORDING_TABLE, run.key, files=[rel], rows=arrow.num_rows,
                         schema=arrow.schema, sources=sizes, complete=True)
        write_manifest(campaign_dir, manifest)


def available_tables(campaign_dir: str, config: Optional[dict] = None,
                     runs: Optional[Iterable[str]] = None) -> Dict[str, dict]:
    """``{table: {"runs": n, "built": n, "failed": {run: reason}}}`` without building anything.

    ``runs`` counts the runs (or, for a job that ran several, the jobs) whose records can yield
    the table, ``built`` those it is built for. *runs* limits both to those ``config/run`` keys.
    """
    campaign_dir = os.path.abspath(campaign_dir)
    groups = _groups(config)
    manifest = read_manifest(campaign_dir)
    wanted = set(runs) if runs is not None else None
    all_runs = find_runs(campaign_dir)
    runs_of_job: Dict[str, List[Run]] = {}
    for run in all_runs:
        if run.job_dir:
            runs_of_job.setdefault(run.job_dir, []).append(run)
    keys: Dict[str, set] = {}
    seen_jobs = set()
    for run in all_runs:
        if wanted is not None and run.key not in wanted:
            continue
        sources = [(SCENARIO_BAG, scenario_recording(run))]
        if run.job_dir and run.job_dir not in seen_jobs:
            seen_jobs.add(run.job_dir)
            infra = os.path.join(run.job_dir, INFRA_BAG)
            if os.path.isdir(infra):
                sources.append((INFRA_BAG, infra))
        bag_tables = set()
        for role, bag_dir in sources:
            if bag_dir is None:
                continue
            key = _context("", role, run, runs_of_job)["key"]
            for table in plan_for(role, recorded_topics(bag_dir), groups.get(role)).tables:
                bag_tables.add(table)
                keys.setdefault(table, set()).add(key)
        for table in run_files(run.path, reserved=bag_tables | DERIVED_TABLES).tables:
            keys.setdefault(table, set()).add(run.key)
        for table in DERIVED:
            keys.setdefault(table, set()).add(run.key)
    out: Dict[str, dict] = {}
    for table, table_keys in keys.items():
        entries = manifest.get("tables", {}).get(table, {}).get("runs", {})
        built = [k for k in table_keys if k in entries and not entries[k].get("reason")]
        failed = {k: entries[k]["reason"] for k in table_keys
                  if k in entries and entries[k].get("reason")}
        out[table] = {"runs": len(table_keys), "built": len(built), "failed": failed}
    return out


__all__ = ["BuildReport", "CAMPAIGN_TABLES", "DERIVED_TABLES", "RECORDING_TABLE", "Run",
           "available_tables", "build", "find_runs", "recorded_topics",
           "scenario_recording"]
