# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""What a run's data files *contain*, read as the index ingest reads them.

The successor to ``test_data_db_ingest.py``. The tests about the ``data.db`` file itself
(its ``user_version`` stamp, the retype-on-widening rebuild, its ``_column_notes`` rows)
went with the writer; the typing, name-map and widening contracts they also covered are
asserted against the index in ``test_campaign_ingest.py``.

What is kept here is the part that is about the *files* rather than about any database:
the JSONL format registry and the scenario verdict. Both are pure functions, so these run
without a Postgres, unlike the ingest tests they used to sit beside.
"""

import json
from pathlib import Path

import pytest

from robovast.results_processing.campaign_ingest import _scenario_verdict
from robovast.results_processing.postprocessing_plugins import _read_table_rows


# ---------------------------------------------------------------------------
# JSONL (scenario_execution --bt-log)
# ---------------------------------------------------------------------------

def _bt_record(**overrides) -> dict:
    record = {
        "timestamp": 0.0, "behavior_id": "id-root", "parent_id": None,
        "child_index": None, "behavior_name": "root",
        "class_name": "py_trees.composites.Sequence", "type": "SEQUENCE",
        "additional_detail": "", "status": "INVALID", "feedback_message": "",
        "is_active": False, "tip_id": None,
        "osc_file": "/scenarios/demo.osc", "osc_line": 3, "osc_column": 0,
    }
    record.update(overrides)
    return record


def _write_bt_log(path: Path, records: list) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {"format": "behaviour_tree_log", "version": 3, "scenario": "demo",
            "scenario_file": "/scenarios/demo.osc", "scenario_sha256": "abc",
            "tick_period": 0.1, "clock": "SimulationClock", "py_trees": "2.4.0",
            "started_at": "2026-08-06T09:14:22Z"}
    path.write_text("\n".join(json.dumps(r) for r in [meta, *records]) + "\n",
                    encoding="utf-8")
    return path


def test_behaviour_tree_log_jsonl_becomes_behaviour_rows(tmp_path):
    """--bt-log writes JSONL directly, so non-ROS runs get a behaviors table too."""
    path = _write_bt_log(tmp_path / "behaviors.jsonl", [
        _bt_record(),
        _bt_record(behavior_id="id-leaf", parent_id="id-root", child_index=0,
                   behavior_name="drive", class_name="roqsim.actions.Drive",
                   type="BEHAVIOUR", osc_line=7, osc_column=8),
        _bt_record(timestamp=10.5, status="RUNNING", is_active=True, tip_id="id-leaf"),
        _bt_record(timestamp=9.5, behavior_id="id-leaf", parent_id="id-root",
                   child_index=0, behavior_name="drive", class_name="roqsim.actions.Drive",
                   type="BEHAVIOUR", status="SUCCESS", is_active=True, osc_line=7),
    ])
    rows = _read_table_rows(path)

    # The metadata record is not a row.
    assert len(rows) == 4
    # Numeric status codes are re-derived so behaviors and nav2_behaviors stay one schema:
    # the 1-4 codes come from py_trees_ros_interfaces, not from py_trees, whose Status
    # values are strings.
    assert sorted({(r["status"], r["status_name"]) for r in rows}) == \
        [(1, "INVALID"), (2, "RUNNING"), (3, "SUCCESS")]
    # Structure survives: parent_id resolves and child_index orders siblings.
    leaf = next(r for r in rows if r["behavior_name"] == "drive")
    assert (leaf["parent_id"], leaf["child_index"], leaf["osc_line"]) == ("id-root", 0, 7)


def test_pruned_subtree_record_does_not_lose_its_column(tmp_path):
    """A removal record carries only three keys; the column must still be there."""
    path = _write_bt_log(tmp_path / "behaviors.jsonl", [
        _bt_record(),
        {"timestamp": 2.0, "behavior_id": "id-root", "removed": True},
    ])
    rows = _read_table_rows(path)

    assert [r["behavior_id"] for r in rows if r.get("removed")] == ["id-root"]


def test_unknown_jsonl_format_yields_no_rows(tmp_path):
    """An unrecognised JSONL producer must not create a junk table or fail the ingest."""
    path = tmp_path / "mystery.jsonl"
    path.write_text(json.dumps({"format": "something-else", "a": 1}) + "\n",
                    encoding="utf-8")

    assert _read_table_rows(path) == []


# ---------------------------------------------------------------------------
# The scenario verdict
#
# Derived once at ingest and read everywhere else (the playback clock, the log views,
# `search_run_logs`), so these guard the one place the verdict is found in text.
# ---------------------------------------------------------------------------

def _log_rows(*rows) -> list:
    return [dict(zip(("sim_time", "wall_ts", "node", "message"), r)) for r in rows]


def test_the_verdict_is_recorded_on_both_clocks():
    """`wall_ts` is not redundant with `timestamp`: the log is ordered by wall, and
    every surface that stops at the end of the trial cuts on it."""
    verdict = _scenario_verdict(_log_rows(
        ("11.75", "1785092243.5", "nav2", "goal reached"),
        ("12.00", "1785092244.0", "scenario_execution_ros",
         "Scenario 'test_scenario' succeeded."),
    ))

    assert verdict["status"] == "succeeded"
    assert verdict["timestamp"] == pytest.approx(12.0)
    assert verdict["wall_ts"] == pytest.approx(1785092244.0)


def test_a_run_aborted_at_shutdown_is_still_a_verdict():
    """The matcher this replaced knew only `succeeded.` and `: execution failed.`, so a
    run that ended `Aborted` / `Setup failed` / `Run failed` recorded no end at all --
    and is exactly the run with the most shutdown noise to hide."""
    verdict = _scenario_verdict(_log_rows(
        ("12.00", "1785092244.0", "scenario_execution_ros", "test_scenario: Aborted"),
    ))

    assert verdict["status"] == "failed"


def test_a_verdict_the_clock_map_cannot_place_still_records_its_wall_time():
    """The clock map does not extrapolate, so a run whose /clock stopped first has no
    sim time for its own verdict. Recording NULL there and a real `wall_ts` is what
    keeps the log cut working on precisely that run."""
    verdict = _scenario_verdict(_log_rows(
        ("", "1785092244.0", "scenario_execution_ros",
         "Scenario 'test_scenario' succeeded."),
    ))

    assert verdict["timestamp"] is None
    assert verdict["wall_ts"] == pytest.approx(1785092244.0)
    assert verdict["status"] == "succeeded"


def test_a_run_that_logged_no_verdict_records_nothing():
    """Not an error: a run killed by its deadline never reached one. Where ``data.db``
    wrote a row of NULLs, the index writes no row at all -- and every reader already has
    to handle a run with no verdict, since a run whose log is missing had none either."""
    assert _scenario_verdict(_log_rows(
        ("11.75", "1785092243.5", "nav2", "goal reached"),
    )) == {}
