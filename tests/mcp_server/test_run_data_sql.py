# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Tests for the read-only SQL data-access tools.

The ``runs`` table is no longer written by hand: it is the dimension table
:func:`~robovast.results_processing.campaign_ingest.build_runs_table` derives from
``campaign.db`` plus the run directories, and it lands in the central index alongside
every other campaign's. What these tests are about is still the *reader* -- truncation,
the write refusals, the campaign scoping -- but the scoping is now the interesting half:
one table holds the corpus, so a query that forgets ``campaign_id`` answers about all of
it and looks entirely healthy doing so.

Needs Postgres: set ``ROBOVAST_TEST_PG_DSN`` or these skip.
"""

import asyncio
import json
import os
import sqlite3
from pathlib import Path

import pytest

from robovast.mcp_server.plugins import results as run_data

DSN = os.environ.get("ROBOVAST_TEST_PG_DSN")

#: Namespaced so a shared test database can hold several suites at once.
SCHEMA = "mcp_rundata_test"

_CAMPAIGN = "camp-2026-07-16-120000"

#: A second campaign with the SAME configuration names and run ids. Those are the keys
#: that collide in the shared tables, so they are the only ones a scoping bug shows up
#: on -- its parameter values differ so a leaked row is recognisable.
_OTHER = "camp-2026-07-16-130000"

#: (config_name, passes, wind, mass) per campaign.
_UNITS = {
    _CAMPAIGN: [("cfg-a", True, 2.5, 1.8), ("cfg-b", False, 4.0, 1.8)],
    _OTHER: [("cfg-a", True, 90.0, 9.9), ("cfg-b", True, 91.0, 9.9)],
}

# The two query tools are coroutines: they announce a pending object-store fetch *before*
# blocking on it, which needs an await. Driven here rather than through a pytest asyncio
# plugin, since the suite configures none.


def describe_campaign_data(*args, **kwargs):
    return asyncio.run(run_data.describe_campaign_data(*args, **kwargs))


def query_campaign_data_sql(*args, **kwargs):
    return asyncio.run(run_data.query_campaign_data_sql(*args, **kwargs))


def _schema_of(table: dict) -> str:
    """The schema a describe entry reports.

    ``schema`` is the one spelling. The index briefly emitted ``schema_`` -- the pydantic
    model's Python-side name, which exists only because the bare word collides with a
    BaseModel attribute -- and every consumer reading ``schema`` silently found nothing.
    """
    return table.get("schema", "")


def _write_campaign(root: Path, name: str) -> Path:
    """A minimal campaign: campaign.db (params/objective) + config/run dirs + test.xml."""
    cdir = root / name
    (cdir / "_execution").mkdir(parents=True)
    cdb = sqlite3.connect(cdir / "campaign.db")
    cdb.execute("CREATE TABLE unit (config_name TEXT, params_json TEXT, objective REAL)")
    cdb.executemany("INSERT INTO unit VALUES (?,?,?)", [
        (cfg, json.dumps({"wind": wind, "mass": mass}), 0.9 if passed else 0.4)
        for cfg, passed, wind, mass in _UNITS[name]])
    cdb.commit()
    cdb.close()
    # per-config run dirs with a JUnit test.xml -- the outcome the runs table gets its
    # status/passed/duration from when the store has no `run` table.
    for cfg, passed, _wind, _mass in _UNITS[name]:
        run0 = cdir / cfg / "0"
        run0.mkdir(parents=True)
        fails = 0 if passed else 1
        (run0 / "test.xml").write_text(
            f'<testsuite tests="1" failures="{fails}" errors="0" time="12.5">'
            f'<testcase time="12.5"/></testsuite>')
    return cdir


def _ingest(root: Path) -> None:
    from robovast.results_processing import campaign_ingest, index_query, index_views

    with index_query.open_index(readonly=False) as conn:
        campaign_ingest.ingest_campaign(conn, str(root), root.name)
        index_views.create_views(conn)


@pytest.fixture(name="index")
def _index(monkeypatch):
    if not DSN:
        pytest.skip("ROBOVAST_TEST_PG_DSN is not set")
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


@pytest.fixture
def campaign(index, tmp_path) -> str:  # pylint: disable=unused-argument
    """The campaign under test, with a second one beside it in the same index."""
    under_test = _write_campaign(tmp_path, _CAMPAIGN)
    _ingest(under_test)
    _ingest(_write_campaign(tmp_path, _OTHER))
    return str(under_test)


def test_describe_lists_runs_and_the_campaign_record(campaign):
    """Both halves stay reachable: the derived ``runs`` table and the mirrored record.

    ``campaign.unit`` used to be an ATTACHed SQLite file; it is a Postgres schema named
    ``campaign`` now, deliberately keeping that spelling so every query ever written
    against it still resolves.
    """
    d = describe_campaign_data(campaign)
    by_name = {t["table"]: t for t in d["tables"]}
    assert "runs" in by_name
    assert "unit" in by_name
    assert _schema_of(by_name["unit"]) == "campaign"


def test_sql_query_and_param_join(campaign):
    """Scalar scenario params are typed ``param_*`` columns, joinable with the outcome."""
    r = query_campaign_data_sql(
        campaign,
        f"SELECT param_wind, status FROM runs WHERE campaign_id = '{_CAMPAIGN}' "
        "ORDER BY param_wind")
    assert r["columns"] == ["param_wind", "status"]
    assert [row["param_wind"] for row in r["rows"]] == [2.5, 4.0]
    # The mirrored record is queryable under the same schema name it always had.
    r2 = query_campaign_data_sql(
        campaign, f"SELECT COUNT(*) n FROM campaign.unit "
                  f"WHERE campaign_id = '{_CAMPAIGN}'")
    assert r2["rows"][0]["n"] == 2


@pytest.mark.parametrize("bad", [
    "DELETE FROM runs", "UPDATE runs SET run_id=9", "CREATE TABLE x(a INT)",
    "DROP TABLE runs", "INSERT INTO runs (run_id) VALUES (9)",
    "GRANT ALL ON runs TO PUBLIC", "SET default_transaction_read_only = off",
])
def test_sql_rejects_writes(campaign, bad):
    """A read session, not a promise. ``ATTACH``/``PRAGMA`` are gone with SQLite; what
    replaces them is the same class of statement -- anything that is not a read."""
    assert "error" in query_campaign_data_sql(campaign, bad)


def test_sql_truncates_at_limit(campaign):
    r = query_campaign_data_sql(
        campaign, f"SELECT * FROM runs WHERE campaign_id = '{_CAMPAIGN}'", limit=1)
    assert r["row_count"] == 1 and r["truncated"] is True


def test_an_empty_result_says_whether_the_campaign_is_in_the_index(campaign, tmp_path):
    """"No rows" has two meanings and they need different actions.

    The ``data.db`` reader listed the non-empty base tables so a broken filter was
    distinguishable from an empty dataset. The index answers the question that has taken
    its place, because a campaign is a predicate rather than a file: a filter that matched
    nothing in an ingested campaign is reported plainly, while a campaign that was never
    ingested is *named as such* -- otherwise a query that can never return anything reads
    as a clean negative result.
    """
    matched_nothing = query_campaign_data_sql(
        campaign, f"SELECT * FROM runs WHERE campaign_id = '{_CAMPAIGN}' "
                  "AND config_name='nope'")
    assert matched_nothing["row_count"] == 0
    assert "not in the index" not in matched_nothing.get("note", "")

    never_ingested = tmp_path / "camp-2026-07-16-140000"
    (never_ingested / "_execution").mkdir(parents=True)
    r = query_campaign_data_sql(
        str(never_ingested),
        "SELECT * FROM runs WHERE campaign_id = 'camp-2026-07-16-140000'")
    assert r["row_count"] == 0
    assert "camp-2026-07-16-140000" in r["note"] and "not in the index" in r["note"]


def test_query_scopes_to_the_campaign_it_was_asked_about(campaign):
    """The rows of every campaign live in one index; spanning them is a ``campaign_id``
    predicate a caller writes, not a second database to attach.

    Both campaigns hold ``cfg-a``/0 and ``cfg-b``/0, so the unscoped count is exactly
    double -- a plausible-looking number that is the wrong campaign's.
    """
    r = query_campaign_data_sql(
        campaign, f"SELECT COUNT(*) n FROM runs WHERE campaign_id = '{_CAMPAIGN}'")
    assert r["rows"][0]["n"] == 2

    leaked = query_campaign_data_sql(
        campaign, f"SELECT param_wind FROM runs WHERE campaign_id = '{_CAMPAIGN}'")
    assert {row["param_wind"] for row in leaked["rows"]} == {2.5, 4.0}, \
        "no row of the other campaign may reach a query scoped to this one"


def test_list_campaign_plots(campaign):
    # Author-declared plots live in the snapshot .vast under
    # visualization.results.data_browser.plots.
    config_dir = Path(campaign) / "_config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "demo.vast").write_text(
        "visualization:\n"
        "  results:\n"
        "    data_browser:\n"
        "      plots:\n"
        "        - title: Wind vs objective\n"
        "          query: SELECT param_wind, objective FROM runs\n"
        "          vega_lite: {mark: point}\n",
        encoding="utf-8")
    r = run_data.list_campaign_plots(campaign)
    assert r["plots"][0]["title"] == "Wind vs objective"
    assert "SELECT" in r["plots"][0]["query"]
    assert r["plots"][0]["vega_lite"] == {"mark": "point"}
