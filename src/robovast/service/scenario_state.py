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

"""Where a run's scenario has got to, folded from its ``behaviors`` and ``behaviors_meta`` tables.

The reply is the shape scenario-execution's own ``tree_state`` reader gives for the run's
``behaviors.jsonl`` -- ``found``, ``running``, ``counts``, ``tree``, ``last_change``, ``now``,
``scenario``, ``clock``, ``started_at``, ``log`` -- built from the rows the campaign's data
engine reads out of that same file (:func:`~robovast.results_processing.data_query.open_data_db`).
That is what lets a running run be read without entering it: the file grows in the campaign
directory as the pod's file agent delivers each complete line, and the engine rebuilds the run's table whenever the
file has grown, so a fold here is over everything written so far.

**Why a fold, and why the whole log.** The log is a metadata record, then a snapshot of every
node at timestamp 0, then one record per behaviour whose status changed. The current tree is
the snapshot plus every later record folded over it, in log order (``seq``); the newest record
alone says only what changed last. ``last_change`` is the newest stamp, which is when
something last changed and not "now": the log cannot know the current time. For a
``monotonic`` log ``now`` is derived from ``started_at``, since its stamps count from the
run's start; for a log stamped on a simulator's clock only a caller reading that clock can
supply *now*, and without it no duration is reported rather than a wrong one.

**It reports, it does not judge.** An action running for minutes may be exactly right --
scenarios wait for topics, durations, arrivals -- and nothing here calls that wrong.

A run whose log is there but carries no metadata record is refused, naming the file: the
record is what says which scenario ran and which clock every stamp is in, and a tree read
without it would be a tree of an unknown run.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from robovast_decode.authored import JSONL_READERS
from robovast_decode.tables import read_manifest

#: What scenario-execution's ``--bt-log`` writes into the run directory.
BEHAVIOUR_LOG = "behaviors.jsonl"

#: The run's tables the fold reads, as the file's name gives them.
BEHAVIOURS_TABLE = "behaviors"
META_TABLE = "behaviors_meta"

#: The columns every run table carries beside the file's own, and the order column the reader
#: adds; none of them is part of a record.
_CONTEXT_COLUMNS = frozenset({"campaign_id", "config_name", "run_id"})
_ORDER_COLUMN = "seq"

_RUNNING = "RUNNING"
_INVALID = "INVALID"


class NoMetadataRecord(ValueError):
    """A behaviour log whose first line is not the metadata record ``--bt-log`` writes."""


def scenario_state(campaign_dir: str, campaign_id: str, run_key: str, *,
                   include_tree: bool = True, now: Optional[float] = None) -> dict:
    """Where the scenario of run *run_key* (``<config>/<run>``) of the campaign has got to.

    *include_tree* ``False`` leaves out ``tree``, most of the reply, for a caller that only
    wants the running action and the counts. *now* is the caller's current time in the log's
    clock, which every running node's ``for_s`` is measured against; ignored for a
    ``monotonic`` log, where it is derived from ``started_at`` instead.

    Returns ``{"found": False, "error": ...}`` for a run with no log, or one whose log holds
    no behaviour record yet -- an error rather than an empty tree, because "the scenario has
    no nodes" and "nothing could be read" must not render alike. Otherwise ``{found, log,
    scenario, started_at, clock, last_change, now, running, counts, tree?}``, where
    ``running`` is the executing action or ``None`` for a scenario finished or not begun.

    Raises :class:`NoMetadataRecord` for a log without its metadata record, and
    ``RuntimeError`` for a log the engine could not turn into rows, with the engine's reason.
    """
    from robovast.results_processing.data_query import open_data_db  # pylint: disable=import-outside-toplevel

    config_name, run_id = _split_run_key(run_key)
    run_dir = os.path.join(campaign_dir, config_name, str(run_id))
    path = os.path.join(run_dir, BEHAVIOUR_LOG)
    if not os.path.isfile(path):
        return {"found": False,
                "error": f"no {BEHAVIOUR_LOG} in {run_dir!r}. A scenario writes one only when "
                         f"run with --bt-log, so this run may have opted out."}
    not_ticked = {"found": False,
                  "error": f"{path} holds no behaviour records yet: the scenario has been "
                           f"launched but has not ticked."}
    if not _declares_its_format(path):
        # The writer's first act is the metadata record; a file with no complete line yet is
        # one being opened. A complete first line that is not that record is refused below.
        return not_ticked
    con = open_data_db(campaign_dir, campaign_id)
    try:
        meta_rows = _run_rows(con, META_TABLE, config_name, run_id)
        rows = _run_rows(con, BEHAVIOURS_TABLE, config_name, run_id)
    finally:
        con.close()
    if not meta_rows:
        reason = _build_reason(campaign_dir, META_TABLE, run_key)
        if reason:
            raise RuntimeError(f"the {META_TABLE} table of run {run_key} could not be built: "
                               f"{reason}")
        raise NoMetadataRecord(
            f"{path} has no metadata record: its first line must be the one scenario-execution's "
            f"--bt-log writes (format 'behavior_tree_log'), naming the scenario and the clock "
            f"its timestamps are in. Without it the log is a tree of an unknown run.")
    if len(meta_rows) > 1:
        raise RuntimeError(f"{META_TABLE} holds {len(meta_rows)} rows for run {run_key}; the "
                           f"log has exactly one metadata record")
    meta = {k: v for k, v in meta_rows[0].items() if k not in _CONTEXT_COLUMNS}
    nodes = _fold(_record(row) for row in _in_log_order(rows, run_key))
    if not nodes:
        reason = _build_reason(campaign_dir, BEHAVIOURS_TABLE, run_key)
        if reason:
            raise RuntimeError(f"the {BEHAVIOURS_TABLE} table of run {run_key} could not be "
                               f"built: {reason}")
        return not_ticked
    stamps = [n["timestamp"] for n in nodes.values() if n.get("timestamp") is not None]
    if meta.get("clock") in (None, "monotonic"):
        now = _elapsed_since(meta.get("started_at"))
    running = _running_leaf(nodes)
    out = {
        "found": True,
        "log": path,
        "scenario": meta.get("scenario"),
        "started_at": meta.get("started_at"),
        "clock": meta.get("clock"),
        "last_change": max(stamps) if stamps else None,
        "now": now,
        "running": None,
        "counts": _counts(nodes),
    }
    if running is not None:
        out["running"] = {**_node_view(running, now), "path": _path_to(nodes, running)}
    if include_tree:
        out["tree"] = _build_tree(nodes, now)
    return out


def _declares_its_format(path: str) -> bool:
    """Whether the log's first line is complete and a metadata record of a known format.

    ``False`` for a file with no terminated, non-blank line yet. Raises
    :class:`NoMetadataRecord` for a complete first line that is not the record ``--bt-log``
    writes -- checked on the file rather than through the engine, since a file of no known
    format gives no table at all, and "no such table" would be the engine's answer for a log
    that is there.
    """
    line = ""
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                break
    if not line.strip() or not line.endswith("\n"):
        return False
    try:
        first = json.loads(line)
    except ValueError:
        first = None
    if not isinstance(first, dict) or first.get("format") not in JSONL_READERS:
        raise NoMetadataRecord(
            f"{path} has no metadata record: its first line must be the one scenario-execution's "
            f"--bt-log writes (format 'behavior_tree_log'), naming the scenario and the clock "
            f"its timestamps are in. Without it the log is a tree of an unknown run.")
    return True


def _split_run_key(run_key: str) -> Tuple[str, int]:
    config_name, sep, run_id = (run_key or "").partition("/")
    if not sep or not config_name or not run_id.isdigit():
        raise ValueError(f"a run is named <config>/<run>, not {run_key!r}")
    return config_name, int(run_id)


def _run_rows(con, table: str, config_name: str, run_id: int) -> List[dict]:
    """*table*'s rows for one run, as dicts; ``[]`` when the run has none."""
    # Literal, not a placeholder: the engine narrows what it builds to the run a statement's
    # WHERE names, and it reads that from the SQL text.
    sql = (f'SELECT * FROM "{table}" WHERE config_name = {_quote(config_name)} '
           f'AND run_id = {int(run_id)}')
    cursor = con.execute(sql)
    return [{k: row[k] for k in row.keys()} for row in cursor.fetchall()]


def _in_log_order(rows: List[dict], run_key: str) -> List[dict]:
    """*rows* by their position in the log, which is the order a fold replays.

    Sorted here rather than in the statement: a table no run in the campaign has rows for is
    defined by its context columns alone, and naming ``seq`` there is a binder error. A row
    without ``seq`` is one the reader that adds it did not make, which is refused: storage
    order is not a stand-in for the log's.
    """
    try:
        return sorted(rows, key=lambda row: row[_ORDER_COLUMN])
    except KeyError as exc:
        raise RuntimeError(f"the {BEHAVIOURS_TABLE} rows of run {run_key} carry no "
                           f"{_ORDER_COLUMN!r}; the fold cannot replay them in log order") from exc


def _quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _build_reason(campaign_dir: str, table: str, run_key: str) -> Optional[str]:
    """Why the engine could not build *table* for the run, from the cache's own record."""
    entry = read_manifest(campaign_dir).get("tables", {}).get(table, {}).get("runs", {}).get(
        run_key) or {}
    return entry.get("reason")


def _record(row: dict) -> dict:
    """A ``behaviors`` row as the log record it was read from.

    The table types the record's fields and carries every column on every row; the record
    had only the keys its writer gave it. A ``removed`` record is three keys and nothing else,
    so its row's other columns are the table's nulls and not the node's state: giving them
    back would blank a node the writer meant to mark. A status record carries every field,
    with ``status`` as the name the writer wrote and the numeric code beside it dropped.
    """
    if row.get("removed"):
        return {"timestamp": row.get("timestamp"), "behavior_id": row.get("behavior_id"),
                "removed": True}
    record = {k: v for k, v in row.items()
              if k not in _CONTEXT_COLUMNS and k not in (_ORDER_COLUMN, "removed", "status")}
    record["status"] = record.pop("status_name", None)
    return record


def _fold(records) -> Dict[str, dict]:
    """The current state of every node, in first-seen order: later records replace earlier
    ones for the same ``behavior_id``, and the order is the snapshot's, so the tree comes out
    in the shape it was declared rather than in the order things happened to change."""
    nodes: Dict[str, dict] = {}
    for record in records:
        node_id = record.get("behavior_id")
        if node_id is None:
            continue
        nodes[node_id] = {**nodes.get(node_id, {}), **record}
    return nodes


def _elapsed_since(started_at) -> Optional[float]:
    """Seconds from an ISO ``started_at`` to now, or ``None`` when it cannot be read.

    How a ``monotonic`` log gets durations: its stamps are elapsed seconds from the run's
    start, so wall time since that start is the same quantity, to the stamp's resolution.
    Assumes the run is still going, which is what this is asked about: on a log whose process
    died mid-action the running node's duration keeps growing, and one past anything
    plausible is a sign the run is gone rather than that the action is slow.
    """
    if not started_at:
        return None
    try:
        start = datetime.fromisoformat(started_at)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        return round((datetime.now(timezone.utc) - start).total_seconds(), 1)
    except (TypeError, ValueError):
        return None


def _running_leaf(nodes: Dict[str, dict]) -> Optional[dict]:
    """The deepest node currently RUNNING, or ``None``.

    Not simply "a RUNNING node": a Sequence is RUNNING for as long as any child is, so the
    naive answer is a branch when the question was what is executing. ``tip_id`` is
    py_trees' own answer and points downwards -- a composite names the deepest node being
    ticked and a leaf records none -- so it is followed from the root. Failing that (an older
    log, or a tip naming a node the log does not hold), the deepest RUNNING node is one that
    is not the parent of another; with two branches running in parallel there are several,
    and ``tree`` carries the rest.
    """
    running = [n for n in nodes.values() if n.get("status") == _RUNNING]
    if not running:
        return None
    for node in running:
        if node.get("parent_id") is None or node.get("parent_id") not in nodes:
            tip = nodes.get(node.get("tip_id"))
            if tip is not None and tip.get("status") == _RUNNING:
                return tip
            break
    parents = {n.get("parent_id") for n in running}
    deepest = [n for n in running if n.get("behavior_id") not in parents]
    return deepest[0] if deepest else running[-1]


def _node_view(node: dict, now: Optional[float] = None) -> dict:
    """One node as a reader wants it: name, type and status, ``since`` its last change, ``for_s``
    while it is running and *now* is known, its feedback, and where in the ``.osc`` it is.

    Not the record: ``behavior_id``/``parent_id`` exist to link records and the tree carries
    the structure instead. ``for_s`` appears only on a running node: on a finished one the same
    subtraction would be "how long since it ended", a different quantity under the same name.
    """
    status = node.get("status", _INVALID)
    since = node.get("timestamp")
    view = {"name": node.get("behavior_name"), "type": node.get("type"), "status": status}
    if since is not None:
        view["since"] = since
        if now is not None and status == _RUNNING:
            view["for_s"] = round(now - since, 1)
    if node.get("feedback_message"):
        view["feedback"] = node["feedback_message"]
    osc_file, osc_line = node.get("osc_file"), node.get("osc_line")
    if osc_file:
        view["osc"] = f"{osc_file}:{osc_line}" if osc_line else osc_file
    return view


def _build_tree(nodes: Dict[str, dict], now: Optional[float] = None):
    """The nodes as a nested tree, children in declaration order (``child_index``).

    A node whose parent the log does not hold -- a truncated log -- is attached at the top
    rather than dropped, because losing a node silently is worse than showing one whose place
    is unclear. One root is returned bare, several as a list.
    """
    children: Dict[Optional[str], List[dict]] = {}
    for node in nodes.values():
        children.setdefault(node.get("parent_id"), []).append(node)
    known = set(nodes)
    roots = [n for n in nodes.values()
             if n.get("parent_id") is None or n.get("parent_id") not in known]

    def build(node, seen):
        view = _node_view(node, now)
        node_id = node.get("behavior_id")
        kids = sorted(children.get(node_id, []), key=lambda n: n.get("child_index") or 0)
        kids = [k for k in kids if k.get("behavior_id") not in seen]
        if kids:
            view["children"] = [build(k, seen | {node_id}) for k in kids]
        return view

    built = [build(r, {r.get("behavior_id")}) for r in roots]
    return built[0] if len(built) == 1 else built


def _path_to(nodes: Dict[str, dict], node: Optional[dict]) -> str:
    """``root > sequence > drive_to`` for *node*, so its place reads without walking."""
    names: List[str] = []
    seen = set()
    while node is not None and node.get("behavior_id") not in seen:
        seen.add(node.get("behavior_id"))
        names.append(node.get("behavior_name") or "?")
        node = nodes.get(node.get("parent_id"))
    return " > ".join(reversed(names))


def _counts(nodes: Dict[str, dict]) -> Dict[str, int]:
    """How many nodes stand in each status: the one-line answer to "how far along is this"."""
    counts: Dict[str, int] = {}
    for node in nodes.values():
        status = node.get("status", _INVALID)
        counts[status] = counts.get(status, 0) + 1
    return counts


__all__ = ["BEHAVIOUR_LOG", "BEHAVIOURS_TABLE", "META_TABLE", "NoMetadataRecord",
           "scenario_state"]
