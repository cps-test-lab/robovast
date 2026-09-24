# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A run's behaviour log is two tables from one file, read as far as it has been written.

``behaviors.jsonl`` opens with a metadata record -- which scenario ran, which clock its stamps
are in, when it started -- and continues with one record per status change. The record is the
one-row table ``behaviors_meta`` beside ``behaviors``, so what the log says about itself is
read by the same path as its rows; and the log grows while a run goes, so a reader arriving
mid-write reads every complete line and holds the half-written one back.
"""

import json

import pyarrow.parquet as pq

from robovast_decode.authored import (JSONL_READERS, META_SUFFIX, read_rows, read_tables,
                                      run_files)
from robovast_decode.build import build
from robovast_decode.tables import read_manifest

META = {"format": "behavior_tree_log", "version": 1, "scenario": "demo",
        "scenario_file": "/config/scenario.osc", "scenario_sha256": None, "tick_period": 0.1,
        "clock": "monotonic", "py_trees": "2.2.3", "started_at": "2026-01-01T00:00:00+00:00"}


def _record(seq, status="RUNNING"):
    return {"timestamp": float(seq), "behavior_id": f"id-{seq}", "parent_id": None,
            "child_index": None, "behavior_name": f"node-{seq}", "class_name": "x.Y",
            "type": "BEHAVIOUR", "additional_detail": "", "status": status,
            "feedback_message": "", "is_active": True, "tip_id": None, "osc_file": None,
            "osc_line": None, "osc_column": None}


def _write(path, entries, terminated=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(e) for e in entries)
    path.write_text(text + ("\n" if terminated else ""), encoding="utf-8")
    return path


def test_the_log_is_listed_as_its_two_tables_against_one_file(tmp_path):
    run = tmp_path / "cfg" / "0"
    log = _write(run / "behaviors.jsonl", [META, _record(1)])

    found = run_files(str(run))

    assert found.tables == {"behaviors": str(log), "behaviors" + META_SUFFIX: str(log)}
    assert not found.refused


def test_a_jsonl_of_no_known_format_is_listed_as_its_own_table_only(tmp_path):
    run = tmp_path / "cfg" / "0"
    log = _write(run / "trace.jsonl", [{"format": "something_else"}, {"a": 1}])

    found = run_files(str(run))

    assert found.tables == {"trace": str(log)}
    assert read_tables(str(log)) == {}


def test_the_metadata_record_is_the_one_row_of_the_meta_table(tmp_path):
    log = _write(tmp_path / "behaviors.jsonl", [META, _record(1), _record(2, "SUCCESS")])

    tables = read_tables(str(log))

    assert set(tables) == {"behaviors", "behaviors_meta"}
    assert tables["behaviors_meta"] == [META]
    assert read_rows(str(log), "behaviors_meta") == [META]
    assert read_rows(str(log)) == tables["behaviors"]


def test_behaviour_rows_carry_their_position_in_the_log(tmp_path):
    """A fold replays the log in its own order, and two records of one node can share a stamp,
    so the row says where in the log it was; the numeric status and its name come as before."""
    log = _write(tmp_path / "behaviors.jsonl", [META, _record(1), _record(2, "SUCCESS")])

    rows = read_rows(str(log))

    assert [r["seq"] for r in rows] == [1, 2]
    assert [(r["status"], r["status_name"]) for r in rows] == [(2, "RUNNING"), (3, "SUCCESS")]
    assert "format" not in rows[0]


def test_a_half_written_last_line_is_held_back(tmp_path):
    """The writer flushes per record and a reader may arrive between the bytes of one. What is
    not yet a line is not yet a record; it is read whole once the newline lands."""
    log = _write(tmp_path / "behaviors.jsonl", [META, _record(1)])
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(_record(2))[:-4])

    assert [r["seq"] for r in read_rows(str(log))] == [1]

    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(_record(2))[-4:] + "\n")

    assert [r["seq"] for r in read_rows(str(log))] == [1, 2]


def test_a_finished_file_without_a_trailing_newline_keeps_its_last_record(tmp_path):
    log = _write(tmp_path / "behaviors.jsonl", [META, _record(1), _record(2)], terminated=False)

    assert [r["seq"] for r in read_rows(str(log))] == [1, 2]


def test_a_terminated_line_that_is_not_json_is_a_corrupt_file(tmp_path):
    log = _write(tmp_path / "behaviors.jsonl", [META, _record(1)])
    with open(log, "a", encoding="utf-8") as fh:
        fh.write("{not json\n")
        fh.write(json.dumps(_record(2)) + "\n")

    try:
        read_rows(str(log))
    except ValueError:
        pass
    else:
        raise AssertionError("a terminated line that is not JSON must raise")


def test_both_spellings_of_the_format_are_the_same_layout():
    assert JSONL_READERS["behaviour_tree_log"] is JSONL_READERS["behavior_tree_log"]
    assert JSONL_READERS["behavior_tree_log"].tables == ("", META_SUFFIX)


def test_a_build_writes_both_tables_from_the_one_file(tmp_path):
    """Through the builder: the meta table is typed from its one row, carries the run's context
    columns like any table, and both tables record the same source, so a change to the file
    rebuilds both."""
    campaign = tmp_path / "campaign-2026-01-01-000000"
    run = campaign / "cfg" / "0"
    _write(run / "behaviors.jsonl", [META, _record(1)])

    report = build(str(campaign), tables=["behaviors", "behaviors_meta"])

    assert not report.failed and not report.unknown
    assert report.built == {"behaviors": ["cfg/0"], "behaviors_meta": ["cfg/0"]}
    meta = pq.read_table(campaign / ".cache" / "tables" / "behaviors_meta" / "cfg" / "0.parquet")
    row = meta.to_pylist()[0]
    assert meta.num_rows == 1
    assert (row["campaign_id"], row["config_name"], row["run_id"]) == (
        "campaign-2026-01-01-000000", "cfg", 0)
    assert (row["scenario"], row["clock"], row["started_at"], row["tick_period"]) == (
        "demo", "monotonic", "2026-01-01T00:00:00+00:00", 0.1)
    assert row["scenario_sha256"] is None
    manifest = read_manifest(str(campaign))
    entries = manifest["tables"]
    assert (entries["behaviors"]["runs"]["cfg/0"]["sources"]
            == entries["behaviors_meta"]["runs"]["cfg/0"]["sources"]
            == {"cfg/0/behaviors.jsonl": (run / "behaviors.jsonl").stat().st_size})
