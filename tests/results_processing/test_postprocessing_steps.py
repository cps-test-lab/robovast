# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``postprocessing_steps`` and the curated column notes, rebuilt in the central index.

Both were written by the retired ``data.db`` writer and both are live surfaces: the table
is documented to agents by ``data_query._TABLE_DESCRIPTIONS`` and pointed at by the MCP
results prompts, and the notes are what ``describe_campaign_data`` shows beside a column --
where someone about to join on the wrong clock is looking.

Set ``ROBOVAST_TEST_PG_DSN`` to run them; without it they skip.
"""

import json
import os
import sqlite3

import pytest

from robovast.results_processing import campaign_ingest, index_query, index_schema

DSN = os.environ.get("ROBOVAST_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="ROBOVAST_TEST_PG_DSN is not set")

SCHEMA = "pp_steps_test"


@pytest.fixture(name="conn")
def _conn():
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(DSN, autocommit=True) as conn:
        for statement in (f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE",
                          "DROP SCHEMA IF EXISTS campaign CASCADE",
                          f"CREATE SCHEMA {SCHEMA}", f"SET search_path TO {SCHEMA}"):
            conn.execute(statement)
        yield conn
        conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        conn.execute("DROP SCHEMA IF EXISTS campaign CASCADE")


#: One step per kind the resolution has to distinguish: an output that became a table, one
#: whose stem is sanitised on the way (``nav-metrics.csv`` -> ``nav_metrics``), and one that
#: is not a data file at all.
ENTRIES = [
    {"plugin": "pose_extract", "output": "goal-1/0/poses.csv",
     "sources": ["rosbag2"], "params": {"topic": "/amcl_pose"}},
    {"plugin": "nav_metrics", "output": "goal-1/0/nav-metrics.csv",
     "sources": ["goal-1/0/poses.csv"], "params": {}},
    {"plugin": "plot_paths", "output": "plots/paths.png", "sources": [], "params": {}},
]


def _campaign(tmp_path, name="camp", *, entries=tuple(ENTRIES), pose_columns=True):
    """A results tree with two runs, and a postprocessing provenance record."""
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / "campaign.db")
    db.executescript("CREATE TABLE campaign (id INTEGER PRIMARY KEY, name TEXT);")
    db.execute("INSERT INTO campaign VALUES (1, 'camp')")
    db.commit()
    db.close()
    for run_id in (0, 1):
        run_dir = root / "goal-1" / str(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        if pose_columns:
            (run_dir / "poses.csv").write_text(
                "timestamp,stamp,position.x,orientation.yaw\n"
                "0.5,0.4,1.0,0.1\n1.5,1.4,2.0,0.2\n")
        else:
            # A `stamp` without a position: rosout's shape, which must NOT collect the
            # pose-contract notes.
            (run_dir / "poses.csv").write_text("timestamp,stamp\n0.5,0.4\n")
        (run_dir / "nav-metrics.csv").write_text("duration_s\n12.5\n")
        (run_dir / "resource_usage.csv").write_text(
            "wall_ts,container,cpu_percent,memory_rss_bytes\n1.0,sim,42.0,1024\n")
    if entries is not None:
        transient = root / "_transient"
        transient.mkdir(parents=True, exist_ok=True)
        (transient / "postprocessing.yaml").write_text(
            json.dumps({"entries": entries}), encoding="utf-8")
    return str(root)


def _steps(conn, campaign_id="camp-a"):
    return conn.execute(
        "SELECT step_idx, plugin, output, table_name, sources_json, params_json "
        "FROM postprocessing_steps WHERE campaign_id = %s ORDER BY step_idx",
        (campaign_id,)).fetchall()


def test_one_row_per_step_with_the_table_its_output_became(conn, tmp_path):
    """The provenance edge, joinable: which plugin produced which table, with what params."""
    totals = campaign_ingest.ingest_campaign(conn, _campaign(tmp_path), "camp-a")

    assert totals["postprocessing_steps"] == 3
    rows = _steps(conn)
    assert [(r[0], r[1], r[3]) for r in rows] == [
        (0, "pose_extract", "poses"),
        # The stem is sanitised on the way to a table name; resolving it here is the whole
        # point of doing this after the walk rather than in the caller.
        (1, "nav_metrics", "nav_metrics"),
        # Not a data file that became a table: NULL is a fact about the step.
        (2, "plot_paths", None),
    ]
    assert json.loads(rows[0][4]) == ["rosbag2"]
    assert json.loads(rows[0][5]) == {"topic": "/amcl_pose"}


def test_the_table_exists_even_when_no_postprocessing_ran(conn, tmp_path):
    """Absent and empty say different things, and only one of them is a broken index."""
    totals = campaign_ingest.ingest_campaign(
        conn, _campaign(tmp_path, entries=None), "camp-a")

    assert totals["postprocessing_steps"] == 0
    assert _steps(conn) == []


def test_a_provenance_record_that_cannot_be_read_is_fatal(conn, tmp_path):
    """It exists and is corrupt: ingesting zero steps would report metrics with no origin."""
    tree = _campaign(tmp_path)
    (tmp_path / "camp" / "_transient" / "postprocessing.yaml").write_text("a: [1,\n")

    with pytest.raises(ValueError, match="postprocessing provenance"):
        campaign_ingest.ingest_campaign(conn, tree, "camp-a")


def test_re_ingesting_reproduces_identical_rows(conn, tmp_path):
    tree = _campaign(tmp_path)
    campaign_ingest.ingest_campaign(conn, tree, "camp-a")
    before = _steps(conn)
    campaign_ingest.ingest_campaign(conn, tree, "camp-a")

    assert _steps(conn) == before


def test_another_campaigns_steps_never_appear(conn, tmp_path):
    campaign_ingest.ingest_campaign(conn, _campaign(tmp_path, "a"), "camp-a")
    campaign_ingest.ingest_campaign(
        conn, _campaign(tmp_path, "b", entries=ENTRIES[:1]), "camp-b")
    campaign_ingest.ingest_campaign(conn, _campaign(tmp_path, "a2"), "camp-a")

    assert len(_steps(conn, "camp-a")) == 3
    assert len(_steps(conn, "camp-b")) == 1


def _note(conn, table, column):
    row = conn.execute(
        f'SELECT note FROM "{index_schema.COLUMN_NOTES_TABLE}" '
        "WHERE table_name = %s AND column_name = %s AND kind = %s",
        (table, column, index_schema.NOTE_DOC)).fetchone()
    return row[0] if row else None


def test_the_pose_contract_notes_warn_about_the_two_clocks(conn, tmp_path):
    """The failure they exist to prevent: joining on `stamp` or differencing `timestamp`."""
    campaign_ingest.ingest_campaign(conn, _campaign(tmp_path), "camp-a")

    assert "ARRIVAL time" in _note(conn, "poses", "timestamp")
    assert "MEASUREMENT time" in _note(conn, "poses", "stamp")
    assert "planar projection" in _note(conn, "poses", "orientation.yaw")


def test_a_stamp_without_a_position_is_not_a_pose_table(conn, tmp_path):
    """rosout carries a `stamp` too, and must not collect notes that talk about poses."""
    campaign_ingest.ingest_campaign(
        conn, _campaign(tmp_path, pose_columns=False), "camp-a")

    assert _note(conn, "poses", "timestamp") is None
    assert _note(conn, "poses", "stamp") is None


def test_the_resource_usage_notes_reach_the_columns_they_warn_about(conn, tmp_path):
    campaign_ingest.ingest_campaign(conn, _campaign(tmp_path), "camp-a")

    assert "one row is one PROCESS NAME" in _note(conn, "resource_usage", "cpu_percent")
    assert "summed RSS" in _note(conn, "resource_usage", "memory_rss_bytes")


def test_no_note_is_invented_for_a_table_the_campaign_does_not_have(conn, tmp_path):
    """A documented column that does not exist is worse than no documentation."""
    root = tmp_path / "bare"
    (root / "goal-1" / "0").mkdir(parents=True)
    (root / "goal-1" / "0" / "nav_metrics.csv").write_text("duration_s\n1.0\n")
    campaign_ingest.ingest_campaign(conn, str(root), "camp-bare")

    assert _note(conn, "poses", "timestamp") is None
    assert _note(conn, "resource_usage", "cpu_percent") is None


def test_the_notes_reach_describe_campaign_datas_column_notes(conn, tmp_path):
    """What makes them a contract: they are served beside the column, not just stored."""
    campaign_ingest.ingest_campaign(conn, _campaign(tmp_path), "camp-a")

    notes = index_query.column_notes(conn)
    assert "ARRIVAL time" in notes["poses"]["timestamp"]
    assert "summed RSS" in notes["resource_usage"]["memory_rss_bytes"]


# -- the record is written after the ingest, so it cannot be the ingest's input ----

class _RecordingSink:
    """Captures what was written, and needs no database.

    Ungated deliberately: the bug below shipped because the coverage that would have
    caught it was gated on a Postgres nobody had configured.
    """

    def __init__(self):
        self.writes = []

    def write(self, table, rows, context=None, types=None, source=""):  # noqa: D102
        rows = list(rows)
        self.writes.append((table, rows))
        return len(rows)

    def rows_for(self, table):
        return [r for name, rows in self.writes if name == table for r in rows]


def test_the_steps_are_recorded_on_a_campaigns_first_postprocessing(tmp_path):
    """Entries passed in must not depend on the record file, which does not exist yet.

    The ordering that makes this necessary: the provenance record is written LAST, after
    the ingest, so that its presence means postprocessing finished. Read from that file
    during the ingest, this table came out EMPTY on every campaign's first postprocessing
    -- reporting "these metrics have no recorded derivation", which is a wrong answer
    rather than an error, and one that looked correct on any re-run because it was then
    reading the *previous* run's record.
    """
    entries = [
        {"plugin": "rosbags_tf_to_csv", "output": "poses.csv",
         "sources": ["rosbag2"], "params": {"frames": "all"}},
        {"plugin": "rosbags_to_webm", "output": "camera.webm", "sources": ["rosbag2"]},
    ]
    sink = _RecordingSink()

    written = campaign_ingest.build_postprocessing_steps_table(
        sink, str(tmp_path), {"poses": "poses"}, entries=entries)

    assert not (tmp_path / "_transient" / "postprocessing.yaml").exists(), (
        "the premise: there is no record on disk during a first postprocessing")
    assert written == 2
    rows = {r["plugin"]: r for r in sink.rows_for(campaign_ingest.POSTPROCESSING_STEPS_TABLE)}
    assert rows["rosbags_tf_to_csv"]["table_name"] == "poses"
    assert rows["rosbags_to_webm"]["table_name"] is None, (
        "an output that never became a table is a fact about the step, not a failure")


def test_the_record_is_still_read_when_no_entries_are_passed(tmp_path):
    """Re-ingest and import hold no entries, and must still recover the provenance."""
    import yaml  # pylint: disable=import-outside-toplevel

    record = tmp_path / "_transient" / "postprocessing.yaml"
    record.parent.mkdir(parents=True)
    record.write_text(
        yaml.safe_dump({"entries": [{"plugin": "p", "output": "poses.csv"}]}),
        encoding="utf-8")
    sink = _RecordingSink()

    written = campaign_ingest.build_postprocessing_steps_table(
        sink, str(tmp_path), {"poses": "poses"})

    assert written == 1
