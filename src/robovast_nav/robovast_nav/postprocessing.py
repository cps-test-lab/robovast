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

"""``nav2_bt_tree`` postprocessing plugin: turn nav2's behavior-tree log into a tree.

nav2's ``/behavior_tree_log`` (captured to the ``nav2_behavior_tree`` table by the core
``rosbags_nav2bt_to_csv`` handler) is a flat stream of status transitions keyed by
``node_name`` -- it carries no parent/child topology. This plugin reconstructs the
structure from the BT **XML** nav2 loaded (``bt_xml``), joins it with the transition log,
and emits ``nav2_behaviors.csv`` in the **same schema as scenario_execution's**
``behaviors`` table (``timestamp, behavior_name, behavior_id, parent_id, status,
status_name, class_name``).

Emitting that shared schema is deliberate, and is why this package ships **no** tree panel:
the built-in ``scenario_tree`` panel renders any ``behaviors``-shaped table via
``source: { table: nav2_behaviors }``, so there is no bespoke UI data path here and no second
copy of the renderer to keep in step. Any future tree source targeting this schema gets the
same treatment. The columns ``--bt-log`` adds (``child_index``, ``tip_id``,
``feedback_message``, ``osc_line``, …) are absent from this table; the panel probes for them
and simply renders less.

Config (``results_processing.postprocessing``)::

    - rosbags_nav2bt_to_csv                       # core: writes nav2_behavior_tree.csv
    - nav2_bt_tree: { bt_xml: files/nav2_bt.xml } # this plugin: writes nav2_behaviors.csv

List it AFTER ``rosbags_nav2bt_to_csv`` -- postprocessing commands run in order and this
step reads that handler's per-run CSV output. ``bt_xml`` must be the same file nav2 runs
(``bt_navigator``'s ``default_nav_to_pose_bt_xml``), or the tree won't match the log.
"""

import csv
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Optional, Tuple

from robovast.results_processing.postprocessing_plugins import \
    BasePostprocessingPlugin

# BT.CPP status string -> (py_trees-style numeric code, panel status_name). The panel only
# colors RUNNING/SUCCESS/FAILURE; IDLE and SKIPPED map to INVALID (grey = "not ticked").
_STATUS_MAP = {
    "RUNNING": (2, "RUNNING"),
    "SUCCESS": (3, "SUCCESS"),
    "FAILURE": (4, "FAILURE"),
    "IDLE": (1, "INVALID"),
    "SKIPPED": (1, "INVALID"),
    "INVALID": (1, "INVALID"),
}
_BEHAVIORS_FIELDS = ["timestamp", "behavior_name", "behavior_id", "parent_id",
                     "status", "status_name", "class_name"]


class _Node:
    __slots__ = ("id", "name", "class_name", "parent_id")

    def __init__(self, node_id: int, name: str, class_name: str, parent_id: str) -> None:
        self.id = node_id
        self.name = name
        self.class_name = class_name
        self.parent_id = parent_id


def _parse_bt_xml(xml_path: Path) -> "list[_Node]":
    """Flatten a BehaviorTree.CPP XML into ordered nodes with stable integer ids.

    Each XML element under a ``<BehaviorTree>`` is a node; its child elements are its
    children (BT.CPP expresses ports as attributes, not child elements). A node's name is
    its ``name`` attribute if present, else its tag (== the log's ``node_name``);
    ``class_name`` is the tag. ``<SubTree>`` references are expanded inline (best-effort,
    cycle-guarded).
    """
    root = ET.parse(xml_path).getroot()
    trees = {bt.get("ID"): bt for bt in root.iter("BehaviorTree")}
    main_id = root.get("main_tree_to_execute")
    main = trees.get(main_id) if main_id else None
    if main is None:
        main = next(iter(trees.values()), None)
    if main is None:
        return []

    nodes: List[_Node] = []
    counter = [0]

    def bt_name(el: ET.Element) -> str:
        return el.get("name") or el.tag

    def walk(el: ET.Element, parent_id: str, active_trees: frozenset) -> None:
        node_id = counter[0]
        counter[0] += 1
        nodes.append(_Node(node_id, bt_name(el), el.tag, parent_id))
        me = str(node_id)
        for child in list(el):
            if child.tag == "SubTree":
                ref = child.get("ID")
                sub = trees.get(ref)
                if sub is not None and ref not in active_trees:
                    for sub_child in list(sub):
                        walk(sub_child, me, active_trees | {ref})
                continue
            walk(child, me, active_trees)

    # The children of <BehaviorTree> are the actual tree nodes (the element itself is a
    # structural container, not reported on /behavior_tree_log).
    for child in list(main):
        walk(child, "", frozenset({main_id} if main_id else set()))
    return nodes


def _behaviors_rows(nodes: "list[_Node]", raw_csv: Path) -> "Optional[list[dict]]":
    """Join the XML topology with the transition log into behaviors-schema rows."""
    transitions: List[dict] = []
    min_ts: Optional[float] = None
    with open(raw_csv, newline="") as f:
        for row in csv.DictReader(f):
            try:
                ts = float(row["timestamp"])
            except (KeyError, ValueError):
                continue
            transitions.append({"t": ts, "node_name": row.get("node_name", ""),
                                 "status": (row.get("current_status") or "").upper()})
            min_ts = ts if min_ts is None else min(min_ts, ts)

    by_name = {n.name: n for n in nodes}
    base_ts = min_ts if min_ts is not None else 0.0
    rows: List[dict] = []

    # Baseline row per node so nodes that never transition still render (as INVALID/grey).
    for n in nodes:
        rows.append({"timestamp": base_ts, "behavior_name": n.name, "behavior_id": n.id,
                     "parent_id": n.parent_id, "status": 1, "status_name": "INVALID",
                     "class_name": n.class_name})
    # One row per transition, attached to its XML node by name.
    for tr in transitions:
        node = by_name.get(tr["node_name"])
        if node is None:
            continue  # log/XML name drift -- reported by the caller's count
        code, name = _STATUS_MAP.get(tr["status"], (1, "INVALID"))
        rows.append({"timestamp": tr["t"], "behavior_name": node.name,
                     "behavior_id": node.id, "parent_id": node.parent_id,
                     "status": code, "status_name": name, "class_name": node.class_name})
    return rows


class Nav2BtTree(BasePostprocessingPlugin):
    """Reconstruct nav2's behavior tree from its BT XML + ``/behavior_tree_log``.

    Reads each run's ``nav2_behavior_tree.csv`` (raw transitions) and the ``bt_xml`` tree
    definition, writes ``nav2_behaviors.csv`` in the shared ``behaviors`` schema.
    """

    def __call__(self, results_dir: str, config_dir: str,
                 bt_xml: Optional[str] = None, file: str = "nav2_behaviors.csv",
                 raw: str = "nav2_behavior_tree.csv", force: bool = False,
                 **kwargs) -> Tuple[bool, str]:
        if not bt_xml:
            return False, "nav2_bt_tree requires a 'bt_xml' parameter (path to the BT XML)"
        xml_path = Path(config_dir) / bt_xml
        if not xml_path.exists():
            return False, f"nav2_bt_tree: bt_xml not found: {xml_path}"

        try:
            nodes = _parse_bt_xml(xml_path)
        except ET.ParseError as e:
            return False, f"nav2_bt_tree: failed to parse {xml_path}: {e}"
        if not nodes:
            return False, f"nav2_bt_tree: no BehaviorTree nodes in {xml_path}"
        node_names = {n.name for n in nodes}

        written = skipped = drift = 0
        for raw_csv in sorted(Path(results_dir).rglob(raw)):
            out = raw_csv.parent / file
            if not force and out.exists() and out.stat().st_mtime >= raw_csv.stat().st_mtime:
                skipped += 1
                continue
            rows = _behaviors_rows(nodes, raw_csv)
            if rows is None:
                continue
            # Count transitions whose node_name isn't in the XML (log/XML drift).
            with open(raw_csv, newline="") as f:
                drift += sum(1 for r in csv.DictReader(f)
                             if r.get("node_name") and r["node_name"] not in node_names)
            with open(out, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=_BEHAVIORS_FIELDS)
                w.writeheader()
                w.writerows(rows)
            written += 1

        suffix = f" ({skipped} up-to-date)" if skipped else ""
        if drift:
            suffix += f"; {drift} log rows had no matching XML node (name drift)"
        return True, f"Nav2BtTree wrote {file} for {written} run(s){suffix}"

    def get_files_to_copy(self, config_dir: str, params: dict) -> List[str]:
        """Stage the BT XML into ``_config/`` so it's available at execution time."""
        bt_xml = params.get("bt_xml")
        if bt_xml and (Path(config_dir) / bt_xml).exists():
            return [bt_xml]
        return []
