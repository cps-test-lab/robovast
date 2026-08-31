# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Reading the index: the same contract data.db offered, and the one thing it could not.

Set ``ROBOVAST_TEST_PG_DSN`` to run these; without it they skip.
"""

import os

import pytest

from robovast.results_processing import campaign_ingest, index_query

DSN = os.environ.get("ROBOVAST_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="ROBOVAST_TEST_PG_DSN is not set")

SCHEMA = "q_test"


@pytest.fixture(name="index")
def _index(monkeypatch):
    """An empty index, with the env pointing at it the way a deployment would."""
    psycopg = pytest.importorskip("psycopg")
    from robovast.common import index_db

    with psycopg.connect(DSN, autocommit=True) as setup:
        for statement in (f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE",
                          "DROP SCHEMA IF EXISTS campaign CASCADE",
                          f"CREATE SCHEMA {SCHEMA}"):
            setup.execute(statement)
    monkeypatch.setenv(index_db.DSN_ENV, f"{DSN} options=-csearch_path={SCHEMA}")
    yield
    with psycopg.connect(DSN, autocommit=True) as teardown:
        teardown.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        teardown.execute("DROP SCHEMA IF EXISTS campaign CASCADE")


def _campaign(tmp_path, name):
    root = tmp_path / name
    run = root / "goal-1" / "0"
    run.mkdir(parents=True)
    (run / "poses.csv").write_text(
        "timestamp,frame,x\n0.5,base_link,1.0\n1.5,base_link,2.0\n2.5,base_link,3.0\n")
    return str(root)


def _ingest(tmp_path, *campaign_ids):
    with index_query.open_index(readonly=False) as conn:
        for cid in campaign_ids:
            campaign_ingest.ingest_campaign(conn, _campaign(tmp_path / cid, cid), cid)


def test_a_scoped_query_returns_the_documented_shape(index, tmp_path):
    """``{columns, rows, row_count, truncated}`` -- what every caller already reads."""
    _ingest(tmp_path, "camp-a")

    result = index_query.query_index(
        "SELECT config_name, COUNT(*) AS n FROM poses WHERE campaign_id = 'camp-a' "
        "GROUP BY 1", campaign_id="camp-a")

    assert result["columns"] == ["config_name", "n"]
    assert result["rows"] == [{"config_name": "goal-1", "n": 3}]
    assert result["row_count"] == 1
    assert result["truncated"] is False


def test_one_query_spans_campaigns_without_attaching_anything(index, tmp_path):
    """The measured reason for the whole change.

    Comparing an arm used to mean attaching one data.db per campaign, each fetched into
    the pod first -- ~10 GB moved to answer one question. Here it is a GROUP BY.
    """
    _ingest(tmp_path, "camp-a", "camp-b", "camp-c")

    result = index_query.query_index(
        "SELECT campaign_id, COUNT(*) AS n FROM poses GROUP BY 1 ORDER BY 1")

    assert result["rows"] == [{"campaign_id": "camp-a", "n": 3},
                              {"campaign_id": "camp-b", "n": 3},
                              {"campaign_id": "camp-c", "n": 3}]


def test_a_write_is_refused_by_the_session_not_by_reading_the_sql(index, tmp_path):
    """A denylist of statement kinds parsed from the string is what leaks.

    The session refuses at the server regardless of how the statement is spelled.
    """
    _ingest(tmp_path, "camp-a")

    for statement in ("CREATE TABLE nope (a int)",
                      "DELETE FROM poses",
                      "UPDATE poses SET x = 0",
                      "DROP TABLE poses"):
        with pytest.raises(index_query.IndexQueryError, match="read-only"):
            index_query.query_index(statement)

    # And the data is still there.
    assert index_query.query_index(
        "SELECT COUNT(*) AS n FROM poses")["rows"] == [{"n": 3}]


def test_a_campaign_that_was_never_ingested_says_so(index, tmp_path):
    """"Not ingested" and "ingested and empty" are different answers.

    An empty result set claims the second. The corpus predating the index is not carried
    across, so this is the expected answer for an old campaign -- and saying which it is
    saves re-running a query that will never return anything.
    """
    _ingest(tmp_path, "camp-a")

    result = index_query.query_index(
        "SELECT * FROM poses WHERE campaign_id = 'never-ran'", campaign_id="never-ran")

    assert result["row_count"] == 0
    assert "not in the index" in result["note"]


def test_an_empty_result_from_a_real_campaign_is_not_mislabelled(index, tmp_path):
    """The other side of it: a campaign that IS ingested and genuinely has no match."""
    _ingest(tmp_path, "camp-a")

    result = index_query.query_index(
        "SELECT * FROM poses WHERE campaign_id = 'camp-a' AND frame = 'nonexistent'",
        campaign_id="camp-a")

    assert result["row_count"] == 0
    assert "not in the index" not in result.get("note", "")


def test_a_bad_query_is_a_query_error_not_an_unreachable_index(index, tmp_path):
    """The index answered; what it answered is the caller's to act on."""
    _ingest(tmp_path, "camp-a")

    with pytest.raises(index_query.IndexQueryError, match="SQL error"):
        index_query.query_index("SELECT no_such_column FROM poses")


def test_rows_are_capped_and_the_cap_is_reported(index, tmp_path):
    _ingest(tmp_path, "camp-a")

    result = index_query.query_index("SELECT * FROM poses", max_rows=2)

    assert result["row_count"] == 2
    assert result["truncated"] is True


def test_the_query_functions_are_available_without_the_caller_arranging_it(index, tmp_path):
    """PERCENTILE is something RoboVAST defines, not something Postgres ships."""
    _ingest(tmp_path, "camp-a")

    result = index_query.query_index(
        "SELECT PERCENTILE(x, 50) AS p50, MEDIAN(x) AS med, SQRT(4.0) AS root FROM poses")

    assert result["rows"] == [{"p50": 2.0, "med": 2.0, "root": 2.0}]


def test_internal_tables_are_not_offered_as_campaign_data(index, tmp_path):
    """Bookkeeping is queryable if you know the name; it is not an answer to
    "what is in this campaign?"."""
    _ingest(tmp_path, "camp-a")

    with index_query.open_index() as conn:
        names = {t["table"] for t in index_query.list_tables(conn, "camp-a")}

    assert "poses" in names
    assert not names & index_query.INTERNAL_TABLES


def test_table_row_counts_are_scoped_to_the_campaign_asked_about(index, tmp_path):
    """A corpus-wide count would report the index's size and read as the campaign's."""
    _ingest(tmp_path, "camp-a", "camp-b")

    with index_query.open_index() as conn:
        by_name = {t["table"]: t["rows"] for t in index_query.list_tables(conn, "camp-a")}

    assert by_name["poses"] == 3, "camp-a's rows, not both campaigns'"
