"""``GET /campaigns/{id}/search/history``: the route behind the campaign card's live chart.

The point of the route (as opposed to a SQL query, or a field on ``Status``) is that it answers
from ``campaign.db`` through the record directory — so it works on a campaign that is still
running, and it is not paid for by every card on the page.
"""
import pytest
from fastapi.testclient import TestClient

from robovast.common.store import STORE_FILENAME, CampaignStore
from robovast.service.app import build_app
from robovast.service.client import LocalTransport
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore


@pytest.fixture(name="harness")
def _harness(tmp_path):
    """A real service over a results root the test owns, plus its HTTP client."""
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    transport = LocalTransport(store=store)
    root = tmp_path / "results"
    transport._campaigns_root = lambda: root
    root.mkdir(parents=True, exist_ok=True)
    with TestClient(build_app(transport)) as client:
        yield client, root


def _search_campaign(root, direction="maximize", batches=((0.2, 0.6), (0.5, 0.9))):
    root.mkdir(parents=True, exist_ok=True)
    store = CampaignStore(root / STORE_FILENAME)
    cid = store.create_campaign(
        name="s", mode="search", config_dir=".",
        config={"search": {"objectives": [{"name": "failure_rate", "direction": direction}]}})
    for idx, values in enumerate(batches):
        bid = store.open_batch(cid, idx, ".")
        for n, v in enumerate(values):
            store.record_unit(batch_id=bid, paramset_id=f"p{idx}{n}", config_name=f"c{idx}{n}",
                              params={}, objectives={"failure_rate": v}, measures={},
                              status="evaluated", result_dir=f"c{idx}{n}", n_samples=1)
    store.close()


def test_serves_the_trajectory_of_a_search(harness):
    client, results_root = harness
    _search_campaign(results_root / "s-20260823-000000")
    body = client.get("/campaigns/s-20260823-000000/search/history").json()
    assert body["objective_name"] == "failure_rate"
    assert body["direction"] == "maximize"
    assert body["unavailable"] is None
    assert [b["idx"] for b in body["batches"]] == [0, 1]
    assert [b["best_so_far"] for b in body["batches"]] == [0.6, 0.9]


def test_minimizing_search_reports_the_minimum_as_best(harness):
    client, results_root = harness
    _search_campaign(results_root / "m-20260823-000000", direction="minimize")
    body = client.get("/campaigns/m-20260823-000000/search/history").json()
    assert [b["best_so_far"] for b in body["batches"]] == [0.2, 0.2]


def test_a_campaign_with_no_store_says_so_rather_than_looking_empty(harness):
    client, results_root = harness
    (results_root / "nothing-20260823-000000").mkdir(parents=True, exist_ok=True)
    body = client.get("/campaigns/nothing-20260823-000000/search/history").json()
    assert body["unavailable"] == "no_store"
    assert body["batches"] == []
