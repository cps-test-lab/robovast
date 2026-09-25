# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``NullService.query_campaign_data_sql`` spanning campaigns.

An A/B question -- "how did the nine campaigns of this search arm compare?" -- must be
answerable in one query through the service interface, not only through the direct MCP
local path. Spanning is a list of ids on the call; each campaign's tables are read from its
own directory, and every row carries its ``campaign_id``.

The property to protect has two directions -- only both together pin it:

* a cross-campaign query really does see both campaigns; and
* a query naming one campaign sees **only** its own rows.

The second is the dangerous half: a scoping mistake returns a frame of the right shape, the
right columns and the wrong experiment -- nothing raised, nothing empty, and the only
symptom a number. Both campaigns here therefore use the same configuration name and the
same run ids, which are exactly the keys that collide in a query spanning both.
"""

import csv

import pytest

from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore
from tests.robovast_data.conftest import write_store
from tests.service.null_service import NullService

CAMP_A = "camp-a-2026-08-20-00000001"
CAMP_B = "camp-b-2026-08-20-00000002"

#: Same config name, same run ids in both campaigns -- the collision an unscoped read hides.
OBJECTIVES_A = [0.1, 0.2]
OBJECTIVES_B = [0.3, 0.4, 0.5]


def _make_campaign(root, name, objectives):
    """A campaign directory shaped like a real one: its record, one run dir per run, one
    metric CSV each.

    The metric file is ``objectives.csv`` and not ``runs.csv``: ``runs`` is a table built
    from the campaign record, and a data file may not claim it.
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
    write_store(cdir, {"nominal": {"runs": {i: "passed" for i in range(len(objectives))}}})
    return cdir


@pytest.fixture(name="transport")
def _transport(tmp_path):
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    return NullService(store=store)


@pytest.fixture(name="campaigns")
def _campaigns(transport):
    """Two campaigns on this transport's results root."""
    root = transport._campaigns_root()  # pylint: disable=protected-access
    root.mkdir(parents=True, exist_ok=True)
    _make_campaign(root, CAMP_A, OBJECTIVES_A)
    _make_campaign(root, CAMP_B, OBJECTIVES_B)
    return transport


def test_one_query_spans_several_campaigns(campaigns):
    """The A/B case: one query, the campaigns named on the call."""
    res = campaigns.query_campaign_data_sql(
        CAMP_A,
        "SELECT campaign_id, COUNT(*) AS n FROM objectives "
        "GROUP BY campaign_id ORDER BY campaign_id",
        campaigns=[CAMP_A, CAMP_B])

    assert [(r["campaign_id"], r["n"]) for r in res.rows] == [
        (CAMP_A, len(OBJECTIVES_A)), (CAMP_B, len(OBJECTIVES_B))]


def test_a_single_campaign_query_never_sees_the_other_campaigns_rows(campaigns):
    """The other direction.

    Asserted for both campaigns, so a scope pinned to the wrong constant would still fail
    rather than pass on whichever campaign happened to be asked about first. The SQL
    deliberately carries no predicate: that it holds anyway is the point.
    """
    for campaign, objectives in ((CAMP_A, OBJECTIVES_A), (CAMP_B, OBJECTIVES_B)):
        res = campaigns.query_campaign_data_sql(
            campaign, "SELECT run_id, objective FROM objectives")
        assert len(res.rows) == len(objectives)
        assert sorted(r["objective"] for r in res.rows) == sorted(objectives)


def test_the_campaign_named_by_the_caller_is_reported_back(campaigns):
    """The result says which campaign was asked about."""
    res = campaigns.query_campaign_data_sql(
        CAMP_A, "SELECT COUNT(*) AS n FROM objectives")
    assert res.campaign_id == CAMP_A
    assert res.rows[0]["n"] == len(OBJECTIVES_A)
