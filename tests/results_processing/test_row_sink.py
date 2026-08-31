# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Rows reaching the central index: typed, scoped, and copied rather than inserted.

Against a real Postgres, because what is under test is the ``COPY`` and the typing it
depends on -- a mocked connection would assert the SQL string and miss whether a value
arrives as a number or as its own text.

Set ``ROBOVAST_TEST_PG_DSN`` to run them; without it they skip.
"""

import os

import pytest

from robovast.results_processing.csv_types import INTEGER, REAL, TEXT
from robovast.results_processing.row_sink import PostgresRowSink

DSN = os.environ.get("ROBOVAST_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="ROBOVAST_TEST_PG_DSN is not set")


@pytest.fixture(name="conn")
def _conn():
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS sink_test CASCADE")
        conn.execute("CREATE SCHEMA sink_test")
        conn.execute("SET search_path TO sink_test")
        yield conn
        conn.execute("DROP SCHEMA IF EXISTS sink_test CASCADE")


def test_declared_types_are_written_as_numbers_not_strings(conn):
    """The bag path: the schema says what the column is, so no value is scanned.

    Asserted on the stored Python type, because storing ``0.5`` as ``'0.5'`` is the
    failure ``csv_types`` exists to prevent and it is invisible until an ``ORDER BY``.
    """
    sink = PostgresRowSink(conn, campaign_id="camp-1")
    rows = [{"timestamp": 0.5, "frame": "base_link"},
            {"timestamp": 1.5, "frame": "base_link"}]

    written = sink.write("poses", rows, context={"config_name": "goal-1", "run_id": 0},
                         types={"timestamp": REAL, "frame": TEXT})

    assert written == 2
    got = conn.execute('SELECT "timestamp", frame FROM poses ORDER BY "timestamp"').fetchall()
    assert got == [(0.5, "base_link"), (1.5, "base_link")]


def test_declared_types_never_buffer_the_rows(conn):
    """A generator must be consumed lazily -- ~4M pose rows cannot be materialised.

    A one-shot generator that has already been exhausted would fail on a second pass, so
    a sink that buffered would either break here or silently hold the whole campaign.
    """
    sink = PostgresRowSink(conn, campaign_id="camp-1")
    consumed = []

    def _rows():
        for i in range(3):
            consumed.append(i)
            yield {"value": float(i)}

    written = sink.write("metrics", _rows(),
                         context={"config_name": "goal-1", "run_id": 0},
                         types={"value": REAL})

    assert written == 3
    assert consumed == [0, 1, 2]


def test_context_is_applied_to_every_row(conn):
    """A source emits what it measured, not where it sits."""
    sink = PostgresRowSink(conn, campaign_id="camp-7")
    sink.write("metrics", [{"value": 1.0}, {"value": 2.0}],
               context={"config_name": "goal-2", "run_id": 3},
               types={"value": REAL})

    got = conn.execute("SELECT DISTINCT campaign_id, config_name, run_id FROM metrics").fetchall()
    assert got == [("camp-7", "goal-2", 3)]


def test_without_declared_types_the_values_decide(conn):
    """The CSV path: every value is a string until something reads them."""
    sink = PostgresRowSink(conn, campaign_id="camp-1")
    rows = [{"a": "1", "b": "1.5", "c": "hello"},
            {"a": "2", "b": "2.5", "c": "world"}]

    sink.write("out", rows, context={"config_name": "goal-1", "run_id": 0})

    types = dict(conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'sink_test' AND table_name = 'out'").fetchall())
    assert types["a"] == "bigint"
    assert types["b"] == "double precision"
    assert types["c"] == "text"


def test_one_stray_label_keeps_the_whole_column_text(conn):
    """``csv_types``' strict rule, reaching the index: the raw strings survive."""
    sink = PostgresRowSink(conn, campaign_id="camp-1")
    rows = [{"value": "1"}, {"value": "n/a"}, {"value": "3"}]

    sink.write("out", rows, context={"config_name": "goal-1", "run_id": 0})

    got = [r[0] for r in conn.execute("SELECT value FROM out ORDER BY value").fetchall()]
    assert sorted(got) == ["1", "3", "n/a"]


def test_a_run_id_of_zero_does_not_narrow_the_context_column(conn):
    """The context columns are schema, not evidence.

    Inferring from this batch alone would read ``run_id=0`` as an integer and
    ``config_name`` from a single value -- fine here, wrong the moment another campaign
    writes something wider. So the context types are declared once and never inferred.
    """
    sink = PostgresRowSink(conn, campaign_id="camp-1")
    sink.write("out", [{"value": "1"}], context={"config_name": "goal-1", "run_id": 0})

    types = dict(conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'sink_test' AND table_name = 'out'").fetchall())
    assert types["run_id"] == "bigint"
    assert types["campaign_id"] == "text"
    assert types["config_name"] == "text"


def test_a_container_value_is_json_encoded(conn):
    """Scenario params arrive as lists and dicts; a column holds one value."""
    sink = PostgresRowSink(conn, campaign_id="camp-1")
    sink.write("params", [{"goal": [1.0, 2.0]}],
               context={"config_name": "goal-1", "run_id": 0},
               types={"goal": TEXT})

    assert conn.execute("SELECT goal FROM params").fetchone()[0] == "[1.0, 2.0]"


def test_two_campaigns_share_one_table(conn):
    """The point of the central index: a campaign is a WHERE clause, not an ATTACH."""
    PostgresRowSink(conn, campaign_id="camp-a").write(
        "metrics", [{"value": 1.0}], context={"config_name": "g", "run_id": 0},
        types={"value": REAL})
    PostgresRowSink(conn, campaign_id="camp-b").write(
        "metrics", [{"value": 2.0}], context={"config_name": "g", "run_id": 0},
        types={"value": REAL})

    got = conn.execute(
        "SELECT campaign_id, value FROM metrics ORDER BY campaign_id").fetchall()
    assert got == [("camp-a", 1.0), ("camp-b", 2.0)]


def test_a_later_batch_widening_a_column_keeps_the_earlier_rows(conn):
    """The cross-campaign conflict, end to end through the sink."""
    PostgresRowSink(conn, campaign_id="camp-a").write(
        "metrics", [{"value": 7}], context={"config_name": "g", "run_id": 0},
        types={"value": INTEGER})
    PostgresRowSink(conn, campaign_id="camp-b").write(
        "metrics", [{"value": "n/a"}], context={"config_name": "g", "run_id": 0},
        types={"value": TEXT}, source="camp-b run 0")

    got = sorted(r[0] for r in conn.execute("SELECT value FROM metrics").fetchall())
    assert got == ["7", "n/a"]


def test_a_missing_column_in_one_row_is_null_not_a_shifted_row(conn):
    """Rows are dicts, so a source that omits a column must not misalign the copy."""
    sink = PostgresRowSink(conn, campaign_id="camp-1")
    sink.write("out", [{"a": 1.0, "b": 2.0}, {"a": 3.0}],
               context={"config_name": "g", "run_id": 0},
               types={"a": REAL, "b": REAL})

    got = conn.execute("SELECT a, b FROM out ORDER BY a").fetchall()
    assert got == [(1.0, 2.0), (3.0, None)]
