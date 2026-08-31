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

This is what replaces ``generate_data_db``: the same walk, the same one-file-one-table
rule, the same type inference -- and then ``COPY`` into Postgres rather than a 1.1 GB
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

import logging
from pathlib import Path

from robovast.common import scenario_markers
from robovast.common.campaign_data import list_config_dirs, list_run_dirs
from robovast.results_processing import dimension_ingest, index_schema
from robovast.results_processing.csv_types import REAL, TEXT
# Reused rather than reimplemented: these decide what a data file *is* -- the JSONL format
# registry, the yaw derivation, the table-name rule -- and a second copy of any of them
# would be a second answer to "what does this file contain?". They move out of
# ``postprocessing_plugins`` when ``generate_data_db`` is retired; until then this is one
# definition with two callers, not a fork.
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
