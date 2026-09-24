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

"""A campaign's tables at its two ends: how they are built, and what is built when it ends.

The campaign directory is the database (:mod:`robovast_data`). A table is built from the
campaign's records the first time something names it, by :mod:`robovast_decode`. This module
is what RoboVAST adds around that:

* **The decoder's configuration** -- the campaign's ``rosbags_*`` entries (frames to resolve,
  topics to tabulate, ``require``, cameras to encode) and the containers it runs -- written to
  ``_execution/tables.yaml`` when the campaign's config is frozen and again when its
  postprocessing runs, so a copy of the campaign builds the same tables anywhere
  (:data:`robovast_decode.layout.DECODER_CONFIG`).
* **The campaign-end pass**: build the tables the campaign declares -- the derived tables every
  run view reads, the tables its plots query, its videos -- so what it says it shows is ready
  when it finishes; grade it with its health checks (``run_health``); and record how each table
  was made (``postprocessing_steps``). Everything else is built when first asked for.
* **Clearing** a campaign's tables, which loses nothing but the time to build them again, and
  **replaying** them: every table the records can give, for every run, built again whole.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import pyarrow as pa
import yaml

from robovast_data import Engine, Problem, QueryError, Scope
from robovast_data.statement import parse
from robovast_decode import __version__ as DECODER_VERSION
from robovast_decode.authored import table_name
from robovast_decode.build import CAMPAIGN_TABLES, available_tables
from robovast_decode.derived import DERIVED
from robovast_decode.layout import DECODER_CONFIG, MAIN_CONTAINER, decoder_config
from robovast_decode.tables import (CACHE_DIR, TABLES_DIR, cache_root, campaign_table_path,
                                    manifest_lock, read_manifest, record_campaign_table,
                                    write_manifest, write_table)

logger = logging.getLogger(__name__)

#: The ``.vast`` postprocessing entries that configure the decoder rather than run a step:
#: ``{entry: (handler type, recording)}``. Written as they always were, and read here into
#: the decoder's configuration.
DECODER_COMMANDS: Dict[str, Tuple[str, str]] = {
    "rosbags_to_csv": ("to_csv", "rosbag2"),
    "rosbags_tf_to_csv": ("tf_to_csv", "rosbag2"),
    "rosbags_nav2bt_to_csv": ("nav2_bt_to_csv", "rosbag2"),
    "rosbags_action_to_csv": ("action_to_csv", "rosbag2"),
    "rosbags_rosout_to_csv": ("rosout_to_csv", "logs/rosout_bag"),
    "rosbags_clock_to_csv": ("clock_to_csv", "logs/rosout_bag"),
    "rosbags_costmap_to_csv": ("costmap_to_csv", "rosbag2"),
    "rosbags_to_webm": ("to_webm", "rosbag2"),
}

#: The one entry that groups handlers by recording itself: ``{groups: [{bag_dir, plugins}]}``,
#: or ``{plugins: [...], bag_dir: ...}`` for one recording.
DECODER_GROUPS_COMMAND = "rosbags_process"

#: Entries that fill the ``videos`` table, which the run view's camera panel reads.
VIDEO_PRODUCER_COMMANDS = frozenset({"rosbags_to_webm"})

#: The tables every run view and every log surface read: built for every run at campaign end.
ALWAYS_BUILT = tuple(DERIVED)

HEALTH_TABLE = "run_health"
STEPS_TABLE = "postprocessing_steps"


def _name_and_params(command) -> Tuple[str, dict]:
    if isinstance(command, str):
        return command, {}
    if not isinstance(command, dict) or len(command) != 1:
        raise ValueError(f"a postprocessing entry is a name or a one-key mapping, got "
                         f"{command!r}")
    name = next(iter(command))
    params = command[name] or {}
    if not isinstance(params, dict):
        raise ValueError(f"{name}: parameters must be a mapping, got {params!r}")
    return name, dict(params)


def is_decoder_command(command) -> bool:
    """Whether *command* configures the decoder rather than naming a step to run."""
    name, _ = _name_and_params(command)
    return name in DECODER_COMMANDS or name == DECODER_GROUPS_COMMAND


def decoder_groups(commands) -> List[dict]:
    """The decoder's ``groups`` from a postprocessing list's decoder entries, in order."""
    by_bag: Dict[str, List[dict]] = {}
    for command in commands:
        name, params = _name_and_params(command)
        if name == DECODER_GROUPS_COMMAND:
            groups = params.get("groups")
            if groups is None:
                if not params.get("plugins"):
                    raise ValueError("rosbags_process needs 'groups' or 'plugins'")
                groups = [{"bag_dir": params.get("bag_dir", "rosbag2"),
                           "plugins": params["plugins"]}]
            elif "plugins" in params or "bag_dir" in params:
                raise ValueError("rosbags_process takes either 'groups' or 'plugins' "
                                 "(with an optional 'bag_dir'), not both")
            for group in groups:
                by_bag.setdefault(group["bag_dir"], []).extend(group.get("plugins") or [])
        elif name in DECODER_COMMANDS:
            handler, default_bag = DECODER_COMMANDS[name]
            bag = params.pop("bag_dir", default_bag)
            by_bag.setdefault(bag, []).append({"type": handler, **params})
    groups = []
    for bag, plugins in by_bag.items():
        unique = []
        for plugin in plugins:
            if plugin not in unique:
                unique.append(plugin)
        groups.append({"bag_dir": bag, "plugins": unique})
    return groups


def _campaign_blocks(vast_path: str) -> Tuple[list, list, list, list]:
    """``(postprocessing, search postprocessing, health checks, plot queries)`` of a campaign.

    Read raw rather than through the validated model: a finished campaign's frozen config
    must stay readable when the model has moved on.
    """
    from robovast.common.config import visualization_block  # noqa: PLC0415

    with open(vast_path, encoding="utf-8") as fh:
        raw = next(iter(yaml.safe_load_all(fh)), None) or {}
    results = raw.get("results_processing") or {}
    search = raw.get("search") or {}
    plots = visualization_block(raw, "results", "data_browser", "plots") or []
    return (list(results.get("postprocessing") or []),
            list(search.get("postprocessing") or []),
            list(results.get("health_checks") or []),
            [p.get("query") for p in plots if isinstance(p, dict) and p.get("query")])


def _containers(campaign_dir: str) -> Optional[List[str]]:
    """The runtime names of the containers the campaign runs, the main one first."""
    from robovast.common.campaign_data import campaign_container_plan  # noqa: PLC0415

    plan = campaign_container_plan(Path(campaign_dir))
    if plan is None:
        return None
    return [MAIN_CONTAINER] + [c.name for c in plan.sidecars]


def write_decoder_config(campaign_dir: str, vast_path: str) -> dict:
    """Write the campaign's decoder configuration to ``_execution/tables.yaml``; return it."""
    postprocessing, search, _checks, _plots = _campaign_blocks(vast_path)
    config = {"groups": decoder_groups(postprocessing + search)}
    containers = _containers(campaign_dir)
    if containers is not None:
        config["containers"] = containers
    path = os.path.join(campaign_dir, DECODER_CONFIG)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".incoming"
    with open(tmp, "w", encoding="utf-8") as fh:
        yaml.safe_dump(config, fh, sort_keys=False)
    os.replace(tmp, path)
    return config


def declared_tables(vast_path: str) -> List[str]:
    """The tables a campaign builds when it ends: the derived ones, its plots', its videos."""
    postprocessing, search, _checks, plots = _campaign_blocks(vast_path)
    tables = list(ALWAYS_BUILT)
    names = {_name_and_params(c)[0] for c in postprocessing + search}
    if names & VIDEO_PRODUCER_COMMANDS:
        tables.append("videos")
    engine_tables = set()
    for query in plots:
        try:
            engine_tables |= parse(query).relations
        except QueryError as exc:
            logger.warning("a declared plot's query does not parse, so it builds nothing: "
                           "%s", exc)
    for table in sorted(engine_tables):
        if table not in tables and "." not in table:
            tables.append(table)
    return tables


def build_tables(campaign_dir: str, tables: List[str], *,
                 progress: Optional[Callable[[int, int], None]] = None) -> List[Problem]:
    """Build *tables* for every run of the campaign; what could not be built, by table and run.

    The plot tables go through the engine's own reading of a query -- a view names the tables
    it reads -- so a plot over ``pose_track_view`` builds what that view needs.
    """
    engine = Engine([Scope(campaign_dir)], progress=progress)
    return engine.ensure(engine.tables_for(tables))


def replay_tables(campaign_dir: str, *,
                  progress: Optional[Callable[[int, int], None]] = None) -> List[Problem]:
    """Build every table the campaign's records can give, for every run, from the records.

    A replay yields the rows a live watcher wrote as the runs went: the same decoder over the
    same records, whole. Nothing is cleared here; the caller clears first when it means to
    build everything again.
    """
    tables = sorted(available_tables(campaign_dir, decoder_config(campaign_dir)))
    engine = Engine([Scope(campaign_dir)], progress=progress)
    return engine.ensure(tables)


def clear_tables(campaign_dir: str) -> int:
    """Remove the campaign's built tables; the bytes freed. Every one is built again on use."""
    root = cache_root(campaign_dir)
    if not os.path.isdir(root):
        return 0
    with manifest_lock(campaign_dir):
        tables = os.path.join(root, TABLES_DIR)
        freed = sum(os.path.getsize(os.path.join(d, f))
                    for d, _dirs, files in os.walk(tables) for f in files)
        shutil.rmtree(tables, ignore_errors=True)
        write_manifest(campaign_dir, {"version": read_manifest(campaign_dir)["version"],
                                      "tables": {}})
    return freed


def table_cache_bytes(campaign_dir: str) -> int:
    """How many bytes the campaign's built tables take."""
    tables = os.path.join(campaign_dir, CACHE_DIR, TABLES_DIR)
    return sum(os.path.getsize(os.path.join(d, f))
               for d, _dirs, files in os.walk(tables) for f in files)


def _write_campaign_table(campaign_dir: str, table: str, rows: pa.Table, sources: dict) -> None:
    rel = campaign_table_path(table)
    write_table(campaign_dir, rel, rows)
    with manifest_lock(campaign_dir):
        manifest = read_manifest(campaign_dir)
        record_campaign_table(manifest, table, files=[rel], rows=rows.num_rows,
                              schema=rows.schema, sources=sources)
        write_manifest(campaign_dir, manifest)


def write_run_health(campaign_dir: str, vast_path: str) -> int:
    """Run the campaign's health checks and write ``run_health``; the rows written.

    The table is written even when no check has anything to say: an empty table says the
    checks ran, an absent one that the campaign was never graded.
    """
    from robovast.results_processing import run_health  # noqa: PLC0415
    from robovast.results_processing.data_query import open_data_db  # noqa: PLC0415

    _pp, _search, declared, _plots = _campaign_blocks(vast_path)
    checks = run_health.load_health_checks(declared, config_dir=os.path.dirname(vast_path))
    campaign_id = os.path.basename(os.path.normpath(campaign_dir))
    rows = run_health.run_checks(open_data_db(campaign_dir, campaign_id), campaign_id, checks)
    table = run_health.to_table(rows, campaign_id)
    _write_campaign_table(campaign_dir, HEALTH_TABLE, table,
                          {"checks": len(checks)})
    return table.num_rows


def write_postprocessing_steps(campaign_dir: str, entries: List[dict]) -> int:
    """Write ``postprocessing_steps``: one row per step's output, one per decoded table."""
    campaign_id = os.path.basename(os.path.normpath(campaign_dir))
    rows = []
    for index, entry in enumerate(entries):
        output = entry.get("output") or None
        name = os.path.basename(output) if output else ""
        rows.append({"step_idx": index, "plugin": entry.get("plugin") or "", "output": output,
                     "table_name": (table_name(name)
                                    if name.lower().endswith((".csv", ".jsonl")) else None),
                     "sources_json": json.dumps(entry.get("sources") or []),
                     "params_json": json.dumps(entry.get("params") or {}, sort_keys=True)})
    manifest = read_manifest(campaign_dir)
    for table in sorted(manifest.get("tables", {})):
        if table in CAMPAIGN_TABLES:
            continue
        rows.append({"step_idx": len(rows), "plugin": "robovast-decode", "output": None,
                     "table_name": table, "sources_json": "[]",
                     "params_json": json.dumps({"decoder": DECODER_VERSION})})
    columns = ("step_idx", "plugin", "output", "table_name", "sources_json", "params_json")
    table = pa.table({
        "campaign_id": pa.array([campaign_id] * len(rows), type=pa.string()),
        "config_name": pa.array([None] * len(rows), type=pa.string()),
        "run_id": pa.array([None] * len(rows), type=pa.int64()),
        **{c: pa.array([r[c] for r in rows],
                       type=pa.int64() if c == "step_idx" else pa.string()) for c in columns}})
    _write_campaign_table(campaign_dir, STEPS_TABLE, table, {"entries": len(entries)})
    return table.num_rows


__all__ = ["ALWAYS_BUILT", "DECODER_COMMANDS", "DECODER_GROUPS_COMMAND",
           "VIDEO_PRODUCER_COMMANDS", "build_tables", "clear_tables", "declared_tables",
           "decoder_groups", "is_decoder_command", "replay_tables", "table_cache_bytes",
           "write_decoder_config", "write_postprocessing_steps", "write_run_health"]
