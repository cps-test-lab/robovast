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

"""The ``runs`` table: what an analysis joins its metrics against on ``(config_name, run_id)``.

One row per run directory of the campaign, plus one run-less row per unit that produced no run
(a composition that failed, a search draw that never came back), so a campaign that got half of
what it declared does not read as one that asked for less. Built from the campaign's store,
``campaign.db``, and its intervention ledger; nothing is decoded, so it costs a read of one
SQLite file whatever the campaign's size.

* The outcome (``status``, ``passed``, ``duration_s``, ...) is the store's ``run`` row, which
  the controller writes from each ``test.xml``. A run directory with no row yet is a run still
  going: its outcome columns are NULL.
* The host (``instance_type``, ``node_label``, ``cpu_name``, ``available_cpus``,
  ``available_mem_bytes``) is the run's ``job`` row. ``available_mem`` is recorded as a byte
  count or a Kubernetes quantity (``16Gi``), whichever the runtime reported, and is normalised here so
  the column is one type.
* Every varied factor is a typed ``param_<name>`` column, whichever channel it was written on:
  the scenario channel under its own key, the ``sim`` and ``sut`` channels under names built
  from the end of their destination paths (:func:`channel_column_names`). A column's type is
  inferred from its values across the campaign, so ``ORDER BY`` and ``>`` mean what they say.
* ``probed`` is 1 for a run a person read into while it ran. A separate column, never folded
  into ``status``: a probed run can still pass, and it is excluded from published numbers by
  the analysis, not by its verdict.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import pyarrow as pa

from .layout import INTERVENTIONS, STORE, job_links, run_dirs
from .quantity import to_bytes
from .types import INTEGER, REAL, TEXT, UNKNOWN, json_text, stored_value, widen

logger = logging.getLogger(__name__)

RUNS_TABLE = "runs"

#: ``unit.status`` values that mean the cell produced no run at all, each naming a different
#: coverage loss: ``composition_failed`` is a draw whose configuration could not be built,
#: ``missing`` a declared configuration that reached the results tree with no directory of its
#: own. Both are units a run join would drop, so every reader that counts cells adds them back
#: from here -- a shortfall only one reader knows about is one nobody is told about.
RUNLESS_UNIT_STATUSES = ("composition_failed", "missing")

#: The intervention kind that marks a run as probed.
KIND_PROBED = "probed"

#: The fixed columns after ``config_name`` and ``run_id``, with their types.
#: ``available_cpus`` is REAL because a reservation can be a fraction of a core.
RUNS_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("status", TEXT), ("passed", INTEGER), ("duration_s", REAL), ("errors", INTEGER),
    ("failures", INTEGER), ("objective", REAL), ("start_time", TEXT), ("end_time", TEXT),
    ("instance_type", TEXT), ("node_label", TEXT), ("cpu_name", TEXT),
    ("available_cpus", REAL), ("available_mem_bytes", INTEGER), ("probed", INTEGER),
)

#: Column notes, for the catalog.
NOTES = {
    "node_label": ("a hash of the machine's name, never the name; with instance_type it "
                   "separates a slow machine from a fast one of the same kind"),
    "probed": ("1 when a person read into this run while it ran: a fact about its "
               "provenance, not its outcome. Exclude these rows from anything a published "
               "number rests on."),
    "end_time": "start_time + duration_s; NULL when either is unknown",
}

_ARROW = {INTEGER: pa.int64(), REAL: pa.float64(), TEXT: pa.string(), UNKNOWN: pa.null()}

#: The identifier-shaped tokens of a destination path. A ``sim`` path is dotted and a ``sut``
#: one may be an XPath, so the split is on what could be part of a column name.
_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class StoreError(RuntimeError):
    """The campaign has no readable store: it is not a campaign directory."""


def _json(text, default):
    try:
        value = json.loads(text) if text else default
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def read_store(campaign_dir: str) -> dict:
    """The units and runs of ``campaign.db``: what the ``runs`` table is built from."""
    path = os.path.join(campaign_dir, STORE)
    if not os.path.isfile(path):
        raise StoreError(f"{campaign_dir} has no {STORE}: not a campaign directory")
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        units = db.execute("SELECT config_name, params_json, objective, status, paramset_id, "
                           "channels_json FROM unit").fetchall()
        runs = db.execute(
            "SELECT u.config_name, r.run_id, r.status, r.passed, r.errors, r.failures, "
            "r.duration_s, r.start_time, j.sysinfo_json FROM run r "
            "JOIN unit u ON r.unit_id = u.id LEFT JOIN job j ON r.job_id = j.id").fetchall()
    except sqlite3.Error as exc:
        raise StoreError(f"{path}: {exc}") from exc
    finally:
        db.close()
    params, channels, objective, runless = {}, {}, {}, []
    for config_name, params_json, obj, status, paramset_id, channels_json in units:
        unit_params = _json(params_json, {})
        if status in RUNLESS_UNIT_STATUSES:
            # No directory on disk, and for a search draw no config_name either: the
            # parameter-set id is then the only identity it has.
            runless.append((config_name or str(paramset_id), status, unit_params))
        elif config_name:
            params[config_name] = unit_params
            channels[config_name] = _json(channels_json, {})
            objective[config_name] = obj
    outcomes: Dict[Tuple[str, int], dict] = {}
    for config_name, run_id, status, passed, errors, failures, duration, start, sysinfo in runs:
        outcomes[(config_name, run_id)] = {
            "status": status, "passed": passed, "errors": errors, "failures": failures,
            "duration_s": duration, "start_time": start, "sysinfo": _json(sysinfo, {})}
    return {"params": params, "channels": channels, "objective": objective,
            "runless": runless, "outcomes": outcomes}


def probed_runs(campaign_dir: str) -> set:
    """``{"<config>/<run>"}`` a person read into while they ran.

    A ledger entry names its runs itself or only its job directory (its run is then found
    through the job-link manifest).
    """
    path = os.path.join(campaign_dir, INTERVENTIONS)
    if not os.path.isfile(path):
        return set()
    with open(path, encoding="utf-8") as fh:
        entries = json.load(fh)
    probes = [e for e in entries if e.get("kind") == KIND_PROBED]
    if not probes:
        return set()
    out = {run for e in probes for run in (e.get("runs") or ())}
    job_dirs = {e["job_dir"] for e in probes if e.get("job_dir")}
    if job_dirs:
        for link, target in job_links(campaign_dir).items():
            run_key = link[:-len("/job")] if link.endswith("/job") else link
            if os.path.normpath(os.path.join(run_key, target)) in job_dirs:
                out.add(run_key)
    return out


def flatten_channel(block, prefix: str = "") -> dict:
    """A nested channel block as ``{dotted destination: leaf value}``."""
    out = {}
    for key, value in (block or {}).items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            out.update(flatten_channel(value, f"{path}."))
        else:
            out[path] = value
    return out


def channel_column_names(destinations) -> dict:
    """``{(channel, destination): name}``: the shortest suffix unique in this campaign.

    ``sut.nav2.local_costmap...inflation_layer.inflation_radius`` becomes
    ``sut_inflation_radius``, and grows leftwards only as far as it must to stay unambiguous,
    so two ``friction`` keys under different components become ``sim_floor_friction`` and
    ``sim_wall_friction``. Uniqueness is decided over the whole campaign, because the table is
    one shape for every row.
    """
    tokens = {(channel, path): _TOKEN.findall(path) for channel, path in destinations}
    depth = {key: 1 for key in destinations}

    def _name(key):
        return f"{key[0]}_" + "_".join(tokens[key][-depth[key]:]) if tokens[key] else ""

    names = {key: _name(key) for key in destinations}
    for _ in range(max((len(t) for t in tokens.values()), default=0)):
        counts: dict = {}
        for name in names.values():
            counts[name] = counts.get(name, 0) + 1
        grew = False
        for key in destinations:
            if counts[names[key]] > 1 and depth[key] < len(tokens[key]):
                depth[key] += 1
                grew = True
        if not grew:
            break
        names = {key: _name(key) for key in destinations}
    return names


def channel_params(channels_by_config: dict) -> Tuple[dict, List[str]]:
    """``({config: {name: value}}, unnamed)`` for the ``sim`` and ``sut`` channels.

    *unnamed* are destinations no column name could be built for; their values stay in the
    store's ``unit.channels_json``.
    """
    destinations = sorted({(channel, dest)
                           for channels in channels_by_config.values()
                           for channel in ("sim", "sut")
                           for dest in flatten_channel(channels.get(channel) or {})})
    names = channel_column_names(destinations)
    unnamed = sorted(f"{c}:{p}" for (c, p), name in names.items() if not name)
    out = {}
    for config_name, channels in channels_by_config.items():
        out[config_name] = {
            names[(channel, path)]: value
            for channel in ("sim", "sut")
            for path, value in flatten_channel(channels.get(channel) or {}).items()
            if names[(channel, path)]}
    return out, unnamed


def _param_type(key: str, sources) -> str:
    verdict = UNKNOWN
    for params in sources:
        if key in params:
            value = params[key]
            verdict = widen(verdict, json_text(value) if isinstance(value, (list, dict))
                            else value)
    return verdict


def _end_time(start, duration) -> Optional[str]:
    if not start or duration is None:
        return None
    try:
        return (datetime.fromisoformat(str(start)) + timedelta(seconds=float(duration))
                ).isoformat()
    except (TypeError, ValueError):
        return None


def build_runs(campaign_dir: str) -> pa.Table:
    """The ``runs`` table of *campaign_dir*, with ``campaign_id`` in every row."""
    campaign_dir = os.path.abspath(campaign_dir)
    campaign_id = os.path.basename(campaign_dir)
    store = read_store(campaign_dir)
    params = store["params"]

    # A factor is a column whichever channel it was written on; a scenario parameter wins a
    # name clash, being the one an analysis already reads, and the loser is named.
    by_channel, unnamed = channel_params(store["channels"])
    if unnamed:
        logger.warning("runs: %d sim/sut destination(s) get no param_ column and are "
                       "readable only in unit.channels_json: %s", len(unnamed),
                       ", ".join(unnamed))
    clashes = sorted({name for config, values in by_channel.items() for name in values
                      if name in (params.get(config) or {})})
    if clashes:
        logger.warning("runs: %s already name a scenario parameter, so the sim/sut value "
                       "is readable only in unit.channels_json", ", ".join(clashes))
    for config, values in by_channel.items():
        params[config] = {**values, **(params.get(config) or {})}

    fixed = dict(RUNS_COLUMNS)
    reserved = {*fixed, "campaign_id", "config_name", "run_id"}
    sources = [*params.values(), *(p for _, _, p in store["runless"])]
    keys = sorted({k for p in sources for k in p if f"param_{k}" not in reserved})
    types = {**fixed, **{f"param_{k}": _param_type(k, sources) for k in keys}}

    probed = probed_runs(campaign_dir)
    walk = sorted(set(run_dirs(campaign_dir)) | set(store["outcomes"]))
    rows = []
    for config_name, run_id in walk:
        outcome = store["outcomes"].get((config_name, run_id), {})
        info = outcome.get("sysinfo") or {}
        start, duration = outcome.get("start_time"), outcome.get("duration_s")
        row = {"config_name": config_name, "run_id": run_id,
               "status": outcome.get("status"), "passed": outcome.get("passed"),
               "duration_s": duration, "errors": outcome.get("errors"),
               "failures": outcome.get("failures"),
               "objective": store["objective"].get(config_name),
               "start_time": start, "end_time": _end_time(start, duration),
               "instance_type": info.get("instance_type"),
               # Absent for a local run: NULL, so "which machine" is never a machine "".
               "node_label": info.get("node_label") or None,
               "cpu_name": info.get("cpu_name"), "available_cpus": info.get("available_cpus"),
               "available_mem_bytes": to_bytes(info.get("available_mem")),
               "probed": int(f"{config_name}/{run_id}" in probed)}
        unit = params.get(config_name) or {}
        row.update({f"param_{k}": unit.get(k) for k in keys})
        rows.append(row)
    for identity, status, unit in store["runless"]:
        row = {c: None for c in types}
        row.update({"config_name": identity, "run_id": None, "status": status, "passed": 0,
                    "probed": 0})
        row.update({f"param_{k}": unit.get(k) for k in keys})
        rows.append(row)

    arrays = {"campaign_id": pa.array([campaign_id] * len(rows), type=pa.string()),
              "config_name": pa.array([r["config_name"] for r in rows], type=pa.string()),
              "run_id": pa.array([r["run_id"] for r in rows], type=pa.int64())}
    for column, verdict in types.items():
        arrays[column] = pa.array([stored_value(r.get(column), verdict) for r in rows],
                                  type=_ARROW[verdict])
    return pa.table(arrays)


__all__ = ["KIND_PROBED", "NOTES", "RUNLESS_UNIT_STATUSES", "RUNS_COLUMNS",
           "RUNS_TABLE", "StoreError", "build_runs", "channel_column_names",
           "channel_params", "flatten_channel", "probed_runs", "read_store"]
