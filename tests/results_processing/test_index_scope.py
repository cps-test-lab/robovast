# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A query scoped to a campaign sees that campaign, whatever its SQL forgot to say.

One index holds every campaign, so ``FROM run_view`` with no predicate is a query over the
corpus. That is not hypothetical: the web UI's results tree shipped exactly that and
rendered another campaign's runs inside the campaign the user had opened, with nothing
raised and nothing empty -- the only symptom was more rows that looked ordinary.

Every test here therefore writes the **unscoped** SQL on purpose. A test that spelled the
``WHERE`` clause out would pass whether or not the mechanism exists, which is the whole
problem restated.

Both campaigns share a configuration name and their run ids, because those are the keys
that collide once one table holds everything.
"""

import csv
import json
import os
import sqlite3

import pytest

DSN = os.environ.get("ROBOVAST_TEST_PG_DSN")
SCHEMA = "index_scope_test"

pg = pytest.mark.skipif(not DSN, reason="ROBOVAST_TEST_PG_DSN is not set")

CAMP_A = "camp-a-2026-08-20-00000001"
CAMP_B = "camp-b-2026-08-20-00000002"

#: Different lengths so a count alone tells the campaigns apart.
OBJECTIVES = {CAMP_A: [0.1, 0.2], CAMP_B: [0.3, 0.4, 0.5]}

#: Opposite directions, so an unscoped read of the campaign record does not merely return
#: an extra row -- it returns a *wrong answer* to "is this objective minimised?".
DIRECTION = {CAMP_A: "minimize", CAMP_B: "maximize"}


def _make_campaign(root, name):
    cdir = root / name
    (cdir / "_execution").mkdir(parents=True)
    for run_id, objective in enumerate(OBJECTIVES[name]):
        run_dir = cdir / "nominal" / str(run_id)
        run_dir.mkdir(parents=True)
        with (run_dir / "objectives.csv").open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["objective"])
            writer.writerow([objective])

    store = sqlite3.connect(cdir / "campaign.db")
    store.executescript(
        "CREATE TABLE campaign (id INTEGER PRIMARY KEY, name TEXT, config_json TEXT);"
        "CREATE TABLE unit (id INTEGER PRIMARY KEY, batch_id INTEGER, config_name TEXT,"
        "                   paramset_id TEXT, params_json TEXT, objective REAL,"
        "                   status TEXT);"
        "CREATE TABLE run (id INTEGER PRIMARY KEY, unit_id INTEGER, run_id INTEGER,"
        "                  status TEXT, passed INTEGER, duration_s REAL, errors INTEGER,"
        "                  failures INTEGER, tests INTEGER, start_time TEXT,"
        "                  failure_message TEXT, job_id INTEGER);")
    store.execute("INSERT INTO campaign VALUES (1, ?, ?)",
                  (name, json.dumps({"search": {"direction": DIRECTION[name]}})))
    store.execute("INSERT INTO unit VALUES (1, 1, 'nominal', 'ps-1', '{}', 0.5, 'evaluated')")
    for run_id in range(len(OBJECTIVES[name])):
        store.execute("INSERT INTO run VALUES (?,1,?,'passed',1,1.0,0,0,1,'t',NULL,NULL)",
                      (run_id + 1, run_id))
    store.commit()
    store.close()
    return cdir


@pytest.fixture(name="two_campaigns")
def _two_campaigns(tmp_path, monkeypatch):
    """Two ingested campaigns -- the state in which a forgotten predicate is silent."""
    if not DSN:
        pytest.skip("ROBOVAST_TEST_PG_DSN is not set")
    psycopg = pytest.importorskip("psycopg")
    from robovast.common import index_db
    from robovast.results_processing import campaign_ingest, index_query, index_views

    monkeypatch.setenv(index_db.DSN_ENV, f"{DSN} options=-csearch_path={SCHEMA}")
    with psycopg.connect(DSN, autocommit=True) as setup:
        setup.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        setup.execute("DROP SCHEMA IF EXISTS campaign CASCADE")
        setup.execute(f"CREATE SCHEMA {SCHEMA}")

    for name in (CAMP_A, CAMP_B):
        _make_campaign(tmp_path, name)
    with index_query.open_index(readonly=False) as conn:
        for name in (CAMP_A, CAMP_B):
            campaign_ingest.ingest_campaign(conn, str(tmp_path / name), name)
        index_views.create_views(conn)

    yield tmp_path

    with psycopg.connect(DSN, autocommit=True) as teardown:
        teardown.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        teardown.execute("DROP SCHEMA IF EXISTS campaign CASCADE")


@pg
def test_an_unscoped_run_view_query_sees_only_the_campaign_it_was_asked_about(two_campaigns):
    """**The regression this exists for.**

    ``SELECT ... FROM run_view`` with no predicate is what the results tree shipped, and it
    reached the browser through a *view* -- which by default runs with its owner's rights
    and so does not inherit the row-level security of the tables underneath it.
    """
    del two_campaigns
    from robovast.results_processing import index_query

    for campaign in (CAMP_A, CAMP_B):
        rows = index_query.query_index(
            "SELECT DISTINCT campaign_id FROM run_view", campaign_id=campaign)["rows"]
        assert rows == [{"campaign_id": campaign}]


@pg
def test_an_unscoped_table_query_sees_only_the_campaign_it_was_asked_about(two_campaigns):
    """The same for a metric table, which is created per data-file stem at ingest."""
    del two_campaigns
    from robovast.results_processing import index_query

    for campaign, objectives in OBJECTIVES.items():
        rows = index_query.query_index(
            "SELECT objective FROM objectives", campaign_id=campaign)["rows"]
        assert sorted(r["objective"] for r in rows) == sorted(objectives)


@pg
def test_an_unscoped_read_of_the_campaign_record_cannot_pick_another_campaigns_row(
        two_campaigns):
    """``FROM campaign.campaign LIMIT 1`` -- a mirrored dimension table, not a metric one.

    The results tree reads the objective's direction this way. Unscoped, ``LIMIT 1`` picks
    an arbitrary campaign's row, so a minimised objective is reported as maximised: a
    wrong answer of exactly the right shape.
    """
    del two_campaigns
    from robovast.results_processing import index_query

    for campaign, direction in DIRECTION.items():
        rows = index_query.query_index(
            "SELECT name, config_json FROM campaign.campaign LIMIT 1",
            campaign_id=campaign)["rows"]
        assert rows[0]["name"] == campaign
        assert json.loads(rows[0]["config_json"])["search"]["direction"] == direction


@pg
def test_spanning_campaigns_is_possible_when_it_is_asked_for(two_campaigns):
    """The headline capability of one index, kept -- but as an argument, not an omission."""
    del two_campaigns
    from robovast.results_processing import index_query

    rows = index_query.query_index(
        "SELECT campaign_id, COUNT(*) AS n FROM objectives "
        "GROUP BY campaign_id ORDER BY campaign_id",
        campaign_id=CAMP_A, campaigns=[CAMP_A, CAMP_B])["rows"]

    assert [(r["campaign_id"], r["n"]) for r in rows] == [
        (CAMP_A, len(OBJECTIVES[CAMP_A])), (CAMP_B, len(OBJECTIVES[CAMP_B]))]


@pg
def test_the_registry_of_campaigns_is_scoped_too(two_campaigns):
    """``_campaigns`` carries a campaign_id, so a scoped session sees only its own."""
    del two_campaigns
    from robovast.results_processing import index_query

    rows = index_query.query_index(
        "SELECT campaign_id FROM _campaigns", campaign_id=CAMP_B)["rows"]
    assert rows == [{"campaign_id": CAMP_B}]


@pg
def test_the_ingest_still_writes_and_clears_every_campaign(two_campaigns):
    """RLS is FORCED, which removes the owner's exemption -- including for the ingest.

    Re-ingesting one campaign must still delete and rewrite exactly its own rows and leave
    the other campaign's alone. If ``WITH CHECK`` or the empty-scope arm of the policy were
    wrong, this is where the ingest would start failing or silently writing nothing.
    """
    root = two_campaigns
    from robovast.results_processing import campaign_ingest, index_query

    with index_query.open_index(readonly=False) as conn:
        campaign_ingest.ingest_campaign(conn, str(root / CAMP_A), CAMP_A)
        counts = dict(conn.execute(
            "SELECT campaign_id, COUNT(*) FROM objectives GROUP BY campaign_id").fetchall())

    assert counts == {CAMP_A: len(OBJECTIVES[CAMP_A]), CAMP_B: len(OBJECTIVES[CAMP_B])}


@pg
def test_a_relation_the_scope_cannot_cover_makes_the_query_fail_not_widen(two_campaigns):
    """The loud failure. An unsecured relation must not be answered from the corpus.

    A view created without ``security_invoker`` -- the exact defect that leaked -- is
    indistinguishable at query time from a scoped one: it returns rows. So the scope is
    verified before the session is handed over, and naming the relation is what makes the
    repair obvious.
    """
    del two_campaigns
    from robovast.results_processing import index_query, index_scope

    with index_query.open_index(readonly=False) as conn:
        conn.execute("CREATE VIEW leaky_view AS SELECT * FROM objectives")

    with pytest.raises(index_scope.ScopeNotEnforceable) as excinfo:
        index_query.query_index("SELECT 1", campaign_id=CAMP_A)
    assert "leaky_view" in str(excinfo.value)

    # And the ingest repairs it, so the index heals rather than needing a hand.
    with index_query.open_index(readonly=False) as conn:
        index_scope.apply_to_index(conn)
    rows = index_query.query_index(
        "SELECT DISTINCT campaign_id FROM leaky_view", campaign_id=CAMP_A)["rows"]
    assert rows == [{"campaign_id": CAMP_A}]


@pg
def test_an_empty_scope_is_refused_rather_than_read_as_the_whole_corpus(two_campaigns):
    """The empty setting means "no scope" to the policy, so it must never reach it."""
    del two_campaigns
    from robovast.results_processing import index_query, index_scope

    conn = index_query.open_index(readonly=False)
    try:
        with pytest.raises(index_scope.ScopeNotEnforceable):
            index_scope.enter_scope(conn, ["", "  "])
    finally:
        conn.close()  # pylint: disable=no-member
