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
* a run's simulator recording: ``<config>/<run>/roqsim_bag/``, the one mcap roqsim writes,
  beside the scenario recording or -- for a stepped run with no ROS at all -- instead of it;
* a job's wall-time infrastructure recording: ``_jobs/.../job-N/logs/rosout_bag/``, the job
  found through ``_transient/job_links.yaml``, which is written before a job starts (the
  ``job`` symlink beside a run appears only once it ends). A job runs one run and gives its
  rows to it. A campaign whose job-link manifest points several runs at one job is refused
  (:class:`SharedJobError`): its job's records cannot be divided between them.

A run's own ``*.csv`` and ``*.jsonl`` files are tables too, named after the file
(:mod:`robovast_decode.authored`).

Every table carries ``campaign_id``, ``config_name`` and ``run_id`` in its own rows, so a set of
parquet files is a table without anything else to join it to.

A table a live session (:mod:`robovast_decode.live`) is writing in parts for a run that is
still recording is left to that session while its ``live`` stamp is fresh; once the stamp is
stale the session is gone, and the table is built here whole, replacing its parts. A derived
table a watcher rebuilds whole as the run goes carries the same stamp and is left alike.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Dict, Iterable, List, Optional, Tuple

import yaml

from . import DATA_CONTRACT, __version__, run_slices
from .authored import RaggedFile, read_rows, run_files, to_arrow, with_yaw
from .decode import channel_type, decode_bag, segments
from .derived import DERIVED, INPUTS, JobRun, derive_job
from .framing import Channel, McapTail, has_footer, summary_channels
from .handlers import Videos
from .layout import YAML_LOADER, job_links, run_dirs
from .registry import INFRA_BAG, ROQSIM_BAG, SCENARIO_BAG, narrow, plan_for
from .tables import (TableBuffer, fixed, live_owned, manifest_lock, read_manifest,
                     record_run_absent, record_run_table, remove_files, run_lock,
                     run_table_path, write_manifest, write_table)

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

#: The file a closed rosbag2 recording has and an open one has not.
BAG_METADATA = "metadata.yaml"


class SharedJobError(ValueError):
    """The campaign's job-link manifest points several runs at one job.

    A job's logs, resource samples and wall-time recording are that of its one run. Where
    several runs share a job, which of them a row belongs to cannot be read off the records,
    so no table is built from them rather than every run being given all of it.
    """


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
    owner: Dict[str, str] = {}
    for config, run_id in run_dirs(campaign_dir):
        path = os.path.join(campaign_dir, config, str(run_id))
        target = links.get(f"{config}/{run_id}/job")
        job = os.path.normpath(os.path.join(path, target)) if target else None
        run = Run(config, run_id, path, job if job and os.path.isdir(job) else None)
        if run.job_dir:
            first = owner.setdefault(run.job_dir, run.key)
            if first != run.key:
                raise SharedJobError(
                    f"{os.path.basename(campaign_dir)}: runs {first} and {run.key} share the "
                    f"job {os.path.relpath(run.job_dir, campaign_dir)}, and a job's records "
                    "are read as its one run's -- its tables cannot be built")
        runs.append(run)
    return runs


def scenario_recording(run: Run) -> Optional[str]:
    """The run's scenario recording: its last attempt, by start time, then by name."""
    attempts = [os.path.join(run.path, d) for d in os.listdir(run.path)
                if _ATTEMPT.match(d) and os.path.isdir(os.path.join(run.path, d))]
    if not attempts:
        return None

    def start(path):
        try:
            return (0, bag_information(path)["starting_time"]["nanoseconds_since_epoch"], path)
        except (KeyError, TypeError):
            return (1, 0, path)
    return sorted(attempts, key=start)[-1]


def bag_information(bag_dir: str) -> Optional[dict]:
    """The ``rosbag2_bagfile_information`` of a closed recording's ``metadata.yaml``; ``None``
    while it has none or it cannot be read.

    rosbag2 writes the file once, when it closes the bag, so its size and modification time
    name its content: a listing reads each recording's once per process, however often it is
    asked, and a rewritten file is read again. Treat the answer as read-only; it is shared.
    """
    meta = os.path.join(bag_dir, BAG_METADATA)
    try:
        stat = os.stat(meta)
    except OSError:
        return None
    return _read_bag_information(meta, stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=4096)
def _read_bag_information(meta: str, mtime_ns: int, size: int) -> Optional[dict]:
    del mtime_ns, size                    # the cache key: a changed file is a new entry
    try:
        with open(meta, encoding="utf-8") as fh:
            info = yaml.load(fh, Loader=YAML_LOADER)["rosbag2_bagfile_information"]
    except (OSError, KeyError, TypeError, yaml.YAMLError):
        return None
    return info if isinstance(info, dict) else None


def roqsim_recording(run: Run) -> Optional[str]:
    """The run's simulator recording, ``<run>/roqsim_bag/``, when it has one."""
    path = os.path.join(run.path, ROQSIM_BAG)
    return path if os.path.isdir(path) else None


def recording_closed(role: str, bag_dir: str) -> bool:
    """Whether the recorder closed the recording and it will not grow.

    rosbag2 writes ``metadata.yaml`` when it closes a bag; roqsim's writer ends its one mcap
    with the footer and the closing magic, so the file's tail says whether it finished.
    """
    if role == ROQSIM_BAG:
        files = segments(bag_dir)
        return bool(files) and all(has_footer(p) for p in files)
    return os.path.isfile(os.path.join(bag_dir, BAG_METADATA))


def recorded_topics(bag_dir: str) -> Dict[str, str]:
    """``{topic: type}`` of a recording, from its ``metadata.yaml`` or its channel records:
    a finished file's summary, else every record of it."""
    info = bag_information(bag_dir)
    if info is not None:
        try:
            return {t["topic_metadata"]["name"]: t["topic_metadata"]["type"]
                    for t in info.get("topics_with_message_count", [])}
        except (KeyError, TypeError):
            pass
    topics: Dict[str, str] = {}
    for path in segments(bag_dir):
        summary = summary_channels(path)
        if summary is not None:
            schemas, channels = summary
            for _, channel in sorted(channels.items()):
                topics.setdefault(channel.topic, channel_type(channel, schemas))
            continue
        tail = McapTail(path)
        for record in tail.read():
            if isinstance(record, Channel):
                topics.setdefault(record.topic, channel_type(record, tail.schemas))
    return topics


def _source_size(bag_dir: str) -> int:
    return sum(os.path.getsize(p) for p in segments(bag_dir))


def _complete(run: Run, role: str, bag_dir: str) -> bool:
    """A run's table is final once the run wrote its verdict and the recorder closed the bag."""
    return (os.path.isfile(os.path.join(run.path, "test.xml"))
            and recording_closed(role, bag_dir))


def plugin_groups(config: Optional[dict]) -> Dict[str, list]:
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
    groups = plugin_groups(config)
    report = BuildReport()
    all_runs = find_runs(campaign_dir)
    selected = [r for r in all_runs if wanted_runs is None or r.key in wanted_runs]
    if wanted_runs is not None:
        missing = wanted_runs - {r.key for r in selected}
        if missing:
            raise KeyError(f"no such run in {campaign_id}: {', '.join(sorted(missing))}")

    known_tables: set = set()
    for run in selected:
        with run_lock(campaign_dir, run.key):
            _build_run(campaign_dir, campaign_id, run, groups, wanted_tables, force, report,
                       known_tables)
    if derived_wanted:
        _build_derived(campaign_dir, campaign_id, selected, derived_wanted,
                       (config or {}).get("containers"), force, report)
    if wanted_tables is not None:
        report.unknown = [t for t in wanted_tables if t not in known_tables]
    return report


def _build_run(campaign_dir: str, campaign_id: str, run: Run, groups: Dict[str, list],
               wanted_tables: Optional[List[str]], force: bool, report: BuildReport,
               known_tables: set) -> None:
    """One run's tables from its recordings and its own files; the caller holds its lock."""
    sources = _sources(run)
    manifest = read_manifest(campaign_dir)
    sizes = {os.path.relpath(b, campaign_dir): _source_size(b) for _, b in sources}
    report_current = not force and _is_current(manifest, RECORDING_TABLE, run.key,
                                               sum(sizes.values()))
    recording_rows = TableBuffer(RECORDING_TABLE)
    run_bag_tables: set = set()
    run_key = run.key
    # {table: role} of what an earlier recording of the run already gives, so a later one
    # does not fill the same table.
    claimed: Dict[str, str] = {}
    for role, bag_dir in sources:
        recorded = recorded_topics(bag_dir)
        plan = plan_for(role, recorded, groups.get(role), taken=claimed)
        claimed.update({t: role for t in plan.tables})
        known_tables.update(plan.tables)
        run_bag_tables.update(plan.tables)
        handlers, _unknown = narrow(plan, wanted_tables)
        for handler in handlers:
            if isinstance(handler, Videos):
                handler.output_dir = run.path
                handler.bag_name = os.path.basename(bag_dir)
        size = sizes[os.path.relpath(bag_dir, campaign_dir)]
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
        complete = _complete(run, role, bag_dir)
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
                arrow = with_yaw(buf.to_arrow(handler.orders.get(table), context={
                    "campaign_id": campaign_id, "config_name": run.config_name,
                    "run_id": run.run_id}))
                rel = run_table_path(campaign_dir, table, run.config_name, run.run_id)
                write_table(campaign_dir, rel, arrow)
                written.append((table, rel, arrow))
        if decoded and not report_current:
            _recording_rows(recording_rows, role, decoded, plan)
        with manifest_lock(campaign_dir):
            fresh = read_manifest(campaign_dir)
            superseded = []
            for table, rel, arrow in written:
                superseded += record_run_table(
                    fresh, table, run_key, files=[rel], rows=arrow.num_rows,
                    schema=arrow.schema,
                    sources={os.path.relpath(bag_dir, campaign_dir): size},
                    complete=complete)
                report.built.setdefault(table, []).append(run_key)
            write_manifest(campaign_dir, fresh)
            # The parts an abandoned live session left: named by no manifest now.
            remove_files(campaign_dir, superseded)
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


def _sources(run: Run) -> List[Tuple[str, str]]:
    """``[(role, bag_dir)]`` of the recordings *run*'s tables are built from, in the order
    they are planned: the scenario recording, the infrastructure recording of the job that
    ran it, then the simulator's own recording, which fills only the tables the earlier ones
    do not."""
    sources = [(SCENARIO_BAG, scenario_recording(run))]
    if run.job_dir:
        infra = os.path.join(run.job_dir, INFRA_BAG)
        sources.append((INFRA_BAG, infra if os.path.isdir(infra) else None))
    sources.append((ROQSIM_BAG, roqsim_recording(run)))
    return [(role, bag_dir) for role, bag_dir in sources if bag_dir]


def derived_sources(campaign_dir: str, run: Run, manifest: dict) -> Dict[str, int]:
    """What a run's derived tables are built from, as ``{source: size or rows}``.

    A live derivation (:class:`~robovast_decode.live.Watcher`) records the same shape, so
    its finalised entry is what a build would have written and a later build leaves it.
    """
    sources: Dict[str, int] = {}
    paths = []
    job_dir = run.job_dir
    if job_dir and os.path.isdir(job_dir):
        paths += [os.path.join(job_dir, n) for n in os.listdir(job_dir)
                  if n.startswith(("resource_usage_", "system_usage_")) and n.endswith(".csv")]
        logs = os.path.join(job_dir, "logs")
        if os.path.isdir(logs):
            paths += [os.path.join(logs, n) for n in os.listdir(logs)
                      if run_slices.container_of(n) is not None and n.endswith(".log")]
    paths += [os.path.join(run.path, n) for n in os.listdir(run.path) if n == "test.xml"]
    for path in sorted(paths):
        sources[os.path.relpath(path, campaign_dir)] = os.path.getsize(path)
    # The run's input rows, whichever of its recordings gave them: a clock map from its
    # simulator's own recording is filed under the run's key as the job's /clock is.
    for table in INPUTS:
        entry = manifest.get("tables", {}).get(table, {}).get("runs", {}).get(run.key) or {}
        sources[f"table:{table}"] = entry.get("rows", 0)
    return sources


def _derive_run(campaign_dir: str, campaign_id: str, run: Run, tables: List[str],
                containers, force: bool, report: BuildReport) -> None:
    """One run's derived tables, from its job's records; the caller holds its lock."""
    manifest = read_manifest(campaign_dir)
    sources = derived_sources(campaign_dir, run, manifest)
    entries = {t: manifest.get("tables", {}).get(t, {}).get("runs", {}) for t in tables}
    # Current: the same sources by this decoder, or a live derivation's whose stamp is
    # fresh (a watcher rebuilds it whole as the run goes and finalises it); one whose
    # stamp went stale is the abandoned watcher's, whatever it was built from.
    if not force and all(_entry_current(entries[t].get(run.key), sources) for t in tables):
        for table in tables:
            report.skipped.setdefault(table, []).append(run.key)
        return
    derivation = derive_job(campaign_dir, campaign_id, run.job_dir,
                            JobRun(run.key, run.config_name, run.run_id, run.path),
                            tables, manifest, containers)
    report.notes.extend(derivation.notes)
    complete = os.path.isfile(os.path.join(run.path, "test.xml"))
    written = []
    for table in tables:
        rows = derivation.tables.get(table)
        if rows is None:
            continue
        rel = run_table_path(campaign_dir, table, run.config_name, run.run_id)
        write_table(campaign_dir, rel, rows)
        written.append((table, rel, rows))
    with manifest_lock(campaign_dir):
        fresh = read_manifest(campaign_dir)
        done = set()
        for table, rel, rows in written:
            record_run_table(fresh, table, run.key, files=[rel], rows=rows.num_rows,
                             schema=rows.schema, sources=sources, complete=complete)
            report.built.setdefault(table, []).append(run.key)
            done.add(table)
        for table in tables:
            if table not in done:
                # A run with no verdict line has no scenario_timestamps row: a table
                # it has, and that came out empty.
                record_run_absent(fresh, table, run.key, sources=sources,
                                  complete=complete, known=True)
        write_manifest(campaign_dir, fresh)


def _build_derived(campaign_dir: str, campaign_id: str, selected: List[Run],
                   tables: List[str], containers, force: bool, report: BuildReport) -> None:
    """The derived tables of every selected run, from its job's records."""
    for run in selected:
        with run_lock(campaign_dir, run.key):
            _derive_run(campaign_dir, campaign_id, run, tables, containers, force, report)


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
            rows = read_rows(path, table)
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


def _written_here(entry: Optional[dict]) -> bool:
    """Whether *entry* was written by this decoder under the contract it follows now.

    Two versions of the decoder can carry one package version -- every build of a branch
    does -- so the contract number is checked beside it: a table laid out under an older
    contract is not the table a reader was promised, whatever version wrote it.
    """
    return bool(entry) and (entry.get("decoder") == __version__
                            and entry.get("contract") == DATA_CONTRACT)


def _is_current(manifest: dict, table: str, run_key: str, size: int) -> bool:
    """Whether the entry needs no build: same bytes by this decoder under this contract, or
    a live session's."""
    entry = manifest.get("tables", {}).get(table, {}).get("runs", {}).get(run_key)
    if not _written_here(entry):
        return False
    if entry.get("live") is not None:
        return live_owned(entry)
    return sum(entry.get("sources", {}).values()) == size


def _entry_current(entry: Optional[dict], sources: dict) -> bool:
    """Whether a derived entry needs no build: the same *sources* by this decoder under this
    contract, or a watcher's whose stamp is fresh."""
    if not _written_here(entry):
        return False
    if entry.get("live") is not None:
        return live_owned(entry)
    return entry.get("sources") == sources


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

    ``runs`` counts the runs whose records can yield the table, ``built`` those it is built
    for. *runs* limits both to those ``config/run`` keys.
    """
    campaign_dir = os.path.abspath(campaign_dir)
    groups = plugin_groups(config)
    manifest = read_manifest(campaign_dir)
    wanted = set(runs) if runs is not None else None
    all_runs = find_runs(campaign_dir)
    keys: Dict[str, set] = {}
    for run in all_runs:
        if wanted is not None and run.key not in wanted:
            continue
        bag_tables = set()
        for role, bag_dir in _sources(run):
            for table in plan_for(role, recorded_topics(bag_dir), groups.get(role)).tables:
                bag_tables.add(table)
                keys.setdefault(table, set()).add(run.key)
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


__all__ = ["BAG_METADATA", "BuildReport", "CAMPAIGN_TABLES", "DERIVED_TABLES", "RECORDING_TABLE",
           "Run", "SharedJobError", "available_tables", "bag_information", "build",
           "derived_sources", "find_runs", "recorded_topics", "recording_closed",
           "roqsim_recording", "scenario_recording"]
