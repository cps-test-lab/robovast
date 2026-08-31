# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Mirroring a campaign's own record into the index.

Against a real Postgres, and against a ``campaign.db`` built here rather than a fixture
file: the point under test is that the mirror follows whatever schema the file declares,
so a test carrying its own frozen copy of that schema would assert the opposite.

Set ``ROBOVAST_TEST_PG_DSN`` to run them; without it they skip.
"""

import os
import sqlite3

import pytest

from robovast.results_processing import dimension_ingest, index_schema

DSN = os.environ.get("ROBOVAST_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="ROBOVAST_TEST_PG_DSN is not set")


@pytest.fixture(name="conn")
def _conn():
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS dim_test CASCADE")
        conn.execute("CREATE SCHEMA dim_test")
        conn.execute("SET search_path TO dim_test")
        yield conn
        conn.execute("DROP SCHEMA IF EXISTS dim_test CASCADE")


def _store(tmp_path, *, with_node=True, extra_campaign_column=None):
    """A minimal campaign.db shaped like the real one."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "campaign.db"
    db = sqlite3.connect(path)
    extra = f", {extra_campaign_column} TEXT" if extra_campaign_column else ""
    db.executescript(f"""
        CREATE TABLE campaign (id INTEGER PRIMARY KEY, name TEXT, mode TEXT,
                               created_at REAL, strategy_state BLOB{extra});
        CREATE TABLE batch (id INTEGER PRIMARY KEY, campaign_id INTEGER, idx INTEGER,
                            dir TEXT, created_at REAL);
        CREATE TABLE unit (id INTEGER PRIMARY KEY, batch_id INTEGER, config_name TEXT,
                           objective REAL, status TEXT);
        CREATE TABLE job (id INTEGER PRIMARY KEY, campaign_id INTEGER, job_dir TEXT,
                          node_label TEXT);
        CREATE TABLE run (id INTEGER PRIMARY KEY, unit_id INTEGER, run_id INTEGER,
                          status TEXT, passed INTEGER, duration_s REAL, job_id INTEGER);
        CREATE TABLE container_failure (id INTEGER PRIMARY KEY, campaign_id INTEGER,
                                        container TEXT, exit_code INTEGER);
    """)
    if with_node:
        db.execute("CREATE TABLE node (id INTEGER PRIMARY KEY, campaign_id INTEGER, "
                   "node_label TEXT, cpu_name TEXT)")
    db.execute("INSERT INTO campaign (id, name, mode, created_at, strategy_state) "
               "VALUES (1, 'funnel', 'batch', 1.0, X'DEADBEEF')")
    db.execute("INSERT INTO batch VALUES (1, 1, 0, 'batch-0', 2.0)")
    db.executemany("INSERT INTO unit VALUES (?, 1, ?, ?, 'evaluated')",
                   [(1, "goal-1", 0.5), (2, "goal-2", 0.7)])
    db.executemany("INSERT INTO job VALUES (?, 1, ?, ?)",
                   [(1, "_jobs/job-0", "abc"), (2, "_jobs/job-1", "abc")])
    db.executemany("INSERT INTO run VALUES (?, ?, ?, ?, ?, ?, ?)",
                   [(1, 1, 0, "passed", 1, 12.5, 1), (2, 2, 0, "failed", 0, 9.0, 2)])
    if with_node:
        db.execute("INSERT INTO node VALUES (1, 1, 'abc', 'a cpu')")
    db.commit()
    db.close()
    return str(path)


def test_the_record_is_mirrored_scoped_by_the_campaigns_string_id(conn, tmp_path):
    """A campaign becomes findable in the index, with its own ids intact."""
    written = dimension_ingest.mirror_campaign_record(conn, _store(tmp_path), "camp-a")

    assert written["campaign"] == 1
    assert written["unit"] == 2
    assert written["run"] == 2
    got = conn.execute(
        "SELECT campaign_id, name, mode FROM campaign").fetchall()
    assert got == [("camp-a", "funnel", "batch")]


def test_source_integer_ids_are_kept_so_they_still_match_the_file(conn, tmp_path):
    """Not remapped to globals: the same id must answer a support question."""
    dimension_ingest.mirror_campaign_record(conn, _store(tmp_path), "camp-a")

    got = conn.execute("SELECT id, unit_id, run_id, job_id FROM run ORDER BY id").fetchall()
    assert got == [(1, 1, 0, 1), (2, 2, 0, 2)]


def test_two_campaigns_coexist_with_colliding_integer_ids(conn, tmp_path):
    """Every per-campaign file numbers from 1; the string id is what separates them."""
    dimension_ingest.mirror_campaign_record(conn, _store(tmp_path / "a"), "camp-a")
    dimension_ingest.mirror_campaign_record(conn, _store(tmp_path / "b"), "camp-b")

    got = conn.execute(
        "SELECT campaign_id, id FROM unit ORDER BY campaign_id, id").fetchall()
    assert got == [("camp-a", 1), ("camp-a", 2), ("camp-b", 1), ("camp-b", 2)]


def test_re_mirroring_replaces_rather_than_duplicates(conn, tmp_path):
    """Re-ingest is the reproducibility invariant, so it must be idempotent."""
    store = _store(tmp_path)
    first = dimension_ingest.mirror_campaign_record(conn, store, "camp-a")
    second = dimension_ingest.mirror_campaign_record(conn, store, "camp-a")

    assert first == second
    assert conn.execute("SELECT COUNT(*) FROM run").fetchone()[0] == 2


def test_re_mirroring_one_campaign_leaves_the_others_alone(conn, tmp_path):
    """The delete is scoped; a re-postprocess must not empty the index."""
    dimension_ingest.mirror_campaign_record(conn, _store(tmp_path / "a"), "camp-a")
    dimension_ingest.mirror_campaign_record(conn, _store(tmp_path / "b"), "camp-b")
    dimension_ingest.mirror_campaign_record(conn, _store(tmp_path / "a2"), "camp-a")

    counts = dict(conn.execute(
        "SELECT campaign_id, COUNT(*) FROM run GROUP BY 1 ORDER BY 1").fetchall())
    assert counts == {"camp-a": 2, "camp-b": 2}


def test_the_redundant_integer_campaign_fk_is_dropped(conn, tmp_path):
    """It is always 1 in a per-campaign file, so it carries nothing."""
    dimension_ingest.mirror_campaign_record(conn, _store(tmp_path), "camp-a")

    columns = {r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'dim_test' AND table_name = 'batch'").fetchall()}
    assert "campaign_id" in columns
    assert columns == {"campaign_id", "id", "idx", "dir", "created_at"}


def test_the_opaque_strategy_blob_is_not_mirrored(conn, tmp_path):
    """Nothing can query it and no results surface may show it."""
    dimension_ingest.mirror_campaign_record(conn, _store(tmp_path), "camp-a")

    columns = {r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'dim_test' AND table_name = 'campaign'").fetchall()}
    assert "strategy_state" not in columns


def test_a_column_added_upstream_arrives_without_a_code_change(conn, tmp_path):
    """The mirror is schema-driven because store.py's ladder moves.

    A transcribed column list would silently drop whatever it had not heard about.
    """
    store = _store(tmp_path, extra_campaign_column="brand_new_column")
    dimension_ingest.mirror_campaign_record(conn, store, "camp-a")

    columns = {r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'dim_test' AND table_name = 'campaign'").fetchall()}
    assert "brand_new_column" in columns


def test_a_missing_table_is_skipped_not_fatal(conn, tmp_path):
    """An old campaign predating a table is still worth listing."""
    written = dimension_ingest.mirror_campaign_record(
        conn, _store(tmp_path, with_node=False), "camp-a")

    assert "node" not in written
    assert written["campaign"] == 1


def test_dimension_tables_get_no_config_or_run_columns(conn, tmp_path):
    """A batch belongs to no configuration and no run.

    Prepending the metric context would add columns that are NULL forever and read as
    data that failed to arrive.
    """
    dimension_ingest.mirror_campaign_record(conn, _store(tmp_path), "camp-a")

    columns = {r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'dim_test' AND table_name = 'node'").fetchall()}
    assert "config_name" not in columns
    assert columns == {"campaign_id", "id", "node_label", "cpu_name"}


def test_the_context_index_covers_the_campaign(conn, tmp_path):
    """Every dimension read is scoped to one campaign."""
    dimension_ingest.mirror_campaign_record(conn, _store(tmp_path), "camp-a")

    indexes = [r[0] for r in conn.execute(
        "SELECT indexdef FROM pg_indexes "
        "WHERE schemaname = 'dim_test' AND tablename = 'run'").fetchall()]
    assert any("campaign_id" in d for d in indexes)
    assert not any("config_name" in d for d in indexes)


def test_metric_and_dimension_tables_share_the_campaign_scope(conn, tmp_path):
    """The join that makes the mirror worth doing: a run's record beside its metrics."""
    from robovast.results_processing.csv_types import REAL
    from robovast.results_processing.row_sink import PostgresRowSink

    dimension_ingest.mirror_campaign_record(conn, _store(tmp_path), "camp-a")
    PostgresRowSink(conn, campaign_id="camp-a").write(
        "poses", [{"x": 1.0}, {"x": 2.0}],
        context={"config_name": "goal-1", "run_id": 0}, types={"x": REAL})

    got = conn.execute(
        "SELECT u.config_name, r.status, COUNT(p.x) "
        "FROM run r "
        "JOIN unit u ON u.campaign_id = r.campaign_id AND u.id = r.unit_id "
        "JOIN poses p ON p.campaign_id = r.campaign_id "
        "            AND p.config_name = u.config_name AND p.run_id = r.run_id "
        "GROUP BY 1, 2").fetchall()
    assert got == [("goal-1", "passed", 2)]


def test_ensure_metadata_tables_is_not_confused_by_the_campaign_table(conn, tmp_path):
    """``campaign`` is a mirrored table; ``_column_types`` is bookkeeping."""
    dimension_ingest.mirror_campaign_record(conn, _store(tmp_path), "camp-a")

    verdicts = index_schema.read_verdicts(conn, "campaign")
    assert verdicts["campaign_id"] == "TEXT"
    assert verdicts["created_at"] == "REAL"
