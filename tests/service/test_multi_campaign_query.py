# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``LocalTransport.query_campaign_data_sql`` spanning campaigns, through one index.

The feature is unchanged and now cheaper: an A/B question -- "how did the nine campaigns
of this search arm compare?" -- must be answerable in one query through the service
interface, not only through the direct MCP local path.

**What changed is how.** Comparing campaigns used to mean attaching one ``data.db`` per
campaign under the schema aliases ``c1``, ``c2``, ... which cost a fetch per campaign
(~10 GB to answer one question about a nine-campaign arm). Every campaign's rows now live
in one Postgres index, so spanning them is a list of ids on the call and the aliases
are gone.

The property to protect is the same one the aliases existed to provide, and it has two
directions -- only both together pin it:

* a cross-campaign query really does see both campaigns; and
* a query naming one campaign sees **only** its own rows.

The second is the dangerous half now. With one table per campaign a scoping mistake meant
a missing file; with one shared table it means a frame of the right shape, the right
columns and the wrong experiment -- nothing raised, nothing empty, and the only symptom a
number. Both campaigns here therefore use the same configuration name and the same run
ids, which are exactly the keys that collide once one table holds everything.

Which is why spanning campaigns is now **asked for** rather than assumed: the session is
confined to ``campaign_id`` by the index itself, and ``campaigns=[...]`` names the ids a
query may see (see :mod:`robovast.results_processing.index_scope`). The capability is
unchanged; what changed is that reaching it by forgetting a predicate no longer works.
"""

import csv
import os

import pytest

from robovast.service.client import LocalTransport
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore

DSN = os.environ.get("ROBOVAST_TEST_PG_DSN")
SCHEMA = "multi_campaign_query_test"

pg = pytest.mark.skipif(not DSN, reason="ROBOVAST_TEST_PG_DSN is not set")

CAMP_A = "camp-a-2026-08-20-00000001"
CAMP_B = "camp-b-2026-08-20-00000002"

#: Same config name, same run ids in both campaigns -- the collision an unscoped read hides.
OBJECTIVES_A = [0.1, 0.2]
OBJECTIVES_B = [0.3, 0.4, 0.5]


def _make_campaign(root, name, objectives):
    """A campaign directory shaped like a real one: one run dir per run, one CSV each.

    No ``_execution/data.db`` any more -- the rows go to the index, and the tree on disk is
    only what names the campaign.

    The metric file is ``objectives.csv`` and not ``runs.csv``: ``runs`` is a table the
    ingest builds itself from the campaign record, and a data file claiming it is refused
    (see :func:`test_a_data_file_may_not_claim_a_table_the_ingest_builds`). This fixture
    used the reserved name and every run was ingested twice.
    """
    cdir = root / name
    (cdir / "_execution").mkdir(parents=True)
    for run_id, objective in enumerate(objectives):
        run_dir = cdir / "nominal" / str(run_id)
        run_dir.mkdir(parents=True)
        with (run_dir / "objectives.csv").open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["objective"])
            writer.writerow([objective])
    return cdir


@pytest.fixture(name="transport")
def _transport(tmp_path):
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    return LocalTransport(store=store)


@pytest.fixture(name="campaigns")
def _campaigns(transport):
    """Two campaigns on this transport's results root, both ingested into the index."""
    psycopg = pytest.importorskip("psycopg")
    from robovast.results_processing import campaign_ingest, index_query

    os.environ["ROBOVAST_INDEX_DSN"] = f"{DSN} options=-csearch_path={SCHEMA}"
    with psycopg.connect(DSN, autocommit=True) as setup:
        setup.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        setup.execute(f"CREATE SCHEMA {SCHEMA}")

    root = transport._campaigns_root()  # pylint: disable=protected-access
    root.mkdir(parents=True, exist_ok=True)
    _make_campaign(root, CAMP_A, OBJECTIVES_A)
    _make_campaign(root, CAMP_B, OBJECTIVES_B)
    with index_query.open_index(readonly=False) as conn:
        campaign_ingest.ingest_campaign(conn, str(root / CAMP_A), CAMP_A)
        campaign_ingest.ingest_campaign(conn, str(root / CAMP_B), CAMP_B)

    yield transport

    with psycopg.connect(DSN, autocommit=True) as teardown:
        teardown.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    # The autouse environment fixture in tests/conftest.py restores ROBOVAST_INDEX_DSN.


@pg
def test_one_query_spans_several_campaigns(campaigns):
    """The A/B case, which is now a predicate rather than nine attached databases."""
    res = campaigns.query_campaign_data_sql(
        CAMP_A,
        "SELECT campaign_id, COUNT(*) AS n FROM objectives "
        "GROUP BY campaign_id ORDER BY campaign_id",
        campaigns=[CAMP_A, CAMP_B])

    assert [(r["campaign_id"], r["n"]) for r in res.rows] == [
        (CAMP_A, len(OBJECTIVES_A)), (CAMP_B, len(OBJECTIVES_B))]


@pg
def test_a_single_campaign_query_never_sees_the_other_campaigns_rows(campaigns):
    """The other direction, and the one a shared table makes easy to get wrong.

    Asserted for both campaigns, so a scope pinned to the wrong constant would still fail
    rather than pass on whichever campaign happened to be asked about first. The SQL
    deliberately carries no predicate: that it holds anyway is the point.
    """
    for campaign, objectives in ((CAMP_A, OBJECTIVES_A), (CAMP_B, OBJECTIVES_B)):
        res = campaigns.query_campaign_data_sql(
            campaign, "SELECT run_id, objective FROM objectives")
        assert len(res.rows) == len(objectives)
        assert sorted(r["objective"] for r in res.rows) == sorted(objectives)


@pg
def test_the_campaign_named_by_the_caller_is_reported_back(campaigns):
    """The result still says which campaign was asked about; only the scoping moved."""
    res = campaigns.query_campaign_data_sql(
        CAMP_A, "SELECT COUNT(*) AS n FROM objectives")
    assert res.campaign_id == CAMP_A
    assert res.rows[0]["n"] == len(OBJECTIVES_A)
