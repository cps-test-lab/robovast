# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A behaviour-tree log as scenario-execution's ``--bt-log`` writes one, for tests of its readers.

The record shapes follow ``scenario_execution.utils.bt_logger``: a metadata record first, a
snapshot of every node at timestamp 0, then one record per status change, and a three-key
record for a node pruned at runtime. ``test_scenario_state`` asserts these key sets against
the writer itself, so a fixture built here is one the writer would have written.
"""

import json
import sqlite3
from pathlib import Path

from robovast.common.store import _SCHEMA

#: The tree: ``scenario`` (a Sequence) over ``setup``, then ``drive`` (a Sequence) over
#: ``drive_to`` and ``wait_for_pick``. Ids are what py_trees would give: opaque and unique.
ROOT, SETUP, DRIVE, DRIVE_TO, WAIT = ("id-root", "id-setup", "id-drive", "id-drive-to", "id-wait")
INSERTED = "id-inserted"

_NODES = [
    # (id, parent, child_index, name, type)
    (ROOT, None, None, "scenario", "SEQUENCE"),
    (SETUP, ROOT, 0, "setup", "BEHAVIOUR"),
    (DRIVE, ROOT, 1, "drive", "SEQUENCE"),
    (DRIVE_TO, DRIVE, 0, "drive_to", "BEHAVIOUR"),
    (WAIT, DRIVE, 1, "wait_for_pick", "BEHAVIOUR"),
]


def metadata(clock="Clock", started_at="2026-01-01T00:00:00+00:00") -> dict:
    """The first record, with every key ``bt_logger.build_meta`` writes."""
    return {"format": "behavior_tree_log", "version": 1, "scenario": "demo",
            "scenario_file": "/config/scenario.osc", "scenario_sha256": "ab" * 32,
            "tick_period": 0.1, "clock": clock, "py_trees": "2.2.3", "started_at": started_at}


def record(node_id, timestamp, status, *, is_active=False, tip_id=None, feedback="",
           osc_line=None) -> dict:
    """One full behaviour record, with every key ``bt_logger._behaviour_record`` writes."""
    _id, parent, index, name, kind = next(n for n in _NODES if n[0] == node_id)
    return {"timestamp": timestamp, "behavior_id": node_id, "parent_id": parent,
            "child_index": index, "behavior_name": name,
            "class_name": f"scenario_execution.actions.{name}", "type": kind,
            "additional_detail": "", "status": status, "feedback_message": feedback,
            "is_active": is_active, "tip_id": tip_id,
            "osc_file": "/config/scenario.osc" if osc_line else None,
            "osc_line": osc_line, "osc_column": 4 if osc_line else None}


def records(clock="Clock", started_at="2026-01-01T00:00:00+00:00") -> list:
    """The whole log: setup done, ``drive_to`` running under ``drive``, a node inserted at
    runtime and pruned again, and the same node changing status twice at one stamp so a
    reader has to respect log order."""
    out = [metadata(clock, started_at)]
    out += [record(n[0], 0.0, "INVALID") for n in _NODES]
    out += [record(ROOT, 0.1, "RUNNING", is_active=True, tip_id=SETUP),
            record(SETUP, 0.1, "RUNNING", is_active=True, osc_line=3, feedback="warming up")]
    out += [record(SETUP, 2.5, "SUCCESS", is_active=True, osc_line=3),
            record(ROOT, 2.5, "RUNNING", is_active=True, tip_id=DRIVE_TO),
            record(DRIVE, 2.5, "RUNNING", is_active=True, tip_id=DRIVE_TO),
            record(DRIVE_TO, 2.5, "RUNNING", is_active=True, osc_line=7,
                   feedback="0.4 m to go")]
    # A subtree inserted at runtime is a full record on first sight, then stated gone.
    inserted = {**record(WAIT, 3.0, "RUNNING"), "behavior_id": INSERTED, "parent_id": DRIVE_TO,
                "child_index": 0, "behavior_name": "inserted", "class_name": "x.Inserted"}
    out += [inserted, {"timestamp": 3.2, "behavior_id": INSERTED, "removed": True}]
    # Two records of one node at one stamp: the later is the node's state.
    out += [record(DRIVE_TO, 4.0, "SUCCESS", is_active=True, osc_line=7),
            record(DRIVE_TO, 4.0, "RUNNING", is_active=True, osc_line=7, feedback="again")]
    return out


def write_log(run_dir, entries=None, *, terminated=True) -> Path:
    """Write *entries* (the whole log by default) as ``behaviors.jsonl`` in *run_dir*."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "behaviors.jsonl"
    entries = records() if entries is None else entries
    text = "\n".join(json.dumps(e, separators=(",", ":")) for e in entries)
    path.write_text(text + ("\n" if terminated else ""), encoding="utf-8")
    return path


def write_store(campaign_dir, configs) -> None:
    """A ``campaign.db`` naming *configs* (``{config_name: [run ids]}``): what the campaign's
    data engine opens a campaign by. A running local campaign has one from its first run."""
    campaign_dir = Path(campaign_dir)
    campaign_dir.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(campaign_dir / "campaign.db")
    db.executescript(_SCHEMA)
    db.execute("INSERT INTO campaign (id, name, config_json) VALUES (1, ?, '{}')",
               (campaign_dir.name,))
    db.execute("INSERT INTO batch (id, campaign_id, idx) VALUES (1, 1, 0)")
    row = 1
    for unit_id, (config_name, run_ids) in enumerate(configs.items(), 1):
        db.execute("INSERT INTO unit (id, batch_id, paramset_id, config_name, params_json, "
                   "status) VALUES (?, 1, ?, ?, '{}', 'ok')", (unit_id, config_name, config_name))
        for run_id in run_ids:
            db.execute("INSERT INTO run (id, unit_id, run_id, status) VALUES (?, ?, ?, "
                       "'running')", (row, unit_id, run_id))
            row += 1
    db.commit()
    db.close()
