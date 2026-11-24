"""``GET /campaigns/{id}/search/history``: the route behind the campaign card's live chart.

The point of the route (as opposed to a SQL query, or a field on ``Status``) is that it answers
from ``campaign.db`` through the record directory — so it works on a campaign that is still
running, and it is not paid for by every card on the page.
"""
import pytest
from fastapi.testclient import TestClient

from robovast.common.store import STORE_FILENAME, CampaignStore
from robovast.service.app import build_app
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore
from tests.service.null_service import NullService


@pytest.fixture(name="harness")
def _harness(tmp_path):
    """A real service over a results root the test owns, plus its HTTP client."""
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    transport = NullService(store=store)
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
        store.complete_batch(bid)
    store.close()


def test_serves_the_trajectory_of_a_search(harness):
    client, results_root = harness
    _search_campaign(results_root / "s-20260823-000000")
    body = client.get("/campaigns/s-20260823-000000/search/history").json()
    [stage] = body["stages"]
    assert (stage["name"], stage["objective_name"], stage["direction"]) == (
        "search", "failure_rate", "maximize")
    assert body["unavailable"] is None
    assert [b["idx"] for b in body["batches"]] == [0, 1]
    assert [b["best_so_far"] for b in body["batches"]] == [0.6, 0.9]
    assert [b["complete"] for b in body["batches"]] == [True, True]


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


def test_a_search_in_stages_has_a_trajectory_per_stage(harness):
    """Each batch names its stage, and best_so_far starts again in each stage's direction."""
    client, results_root = harness
    root = results_root / "st-20260823-000000"
    root.mkdir(parents=True)
    store = CampaignStore(root / STORE_FILENAME)
    cid = store.create_campaign(name="st", mode="search", config_dir=".", config={"search": {
        "objectives": [{"name": "margin", "direction": "minimize"}],
        "stages": [{"name": "a", "over": ["x"]},
                   {"name": "b", "over": ["y"],
                    "objectives": [{"name": "time", "direction": "maximize"}]},
                   {"name": "c", "over": ["z"]}]}})
    a, b, _c = store.declare_stages(cid, [
        {"name": "a", "over": ["x"], "objective": "margin", "direction": "minimize"},
        {"name": "b", "over": ["y"], "objective": "time", "direction": "maximize"},
        {"name": "c", "over": ["z"], "objective": "margin", "direction": "minimize"}])
    for idx, (stage, values, name) in enumerate([(a, (0.5, -1.0), "margin"),
                                                 (b, (3.0, 7.0), "time")]):
        bid = store.open_batch(cid, idx, ".", stage_id=stage)
        for n, v in enumerate(values):
            store.record_unit(batch_id=bid, paramset_id=f"p{idx}{n}", config_name=f"c{idx}{n}",
                              params={}, objectives={name: v}, measures={}, status="evaluated",
                              result_dir=f"c{idx}{n}", n_samples=1)
        store.complete_batch(bid)
    store.close_stage(a, outcome="done", stop_kind="batches", stop_reason="1 batch",
                      batches=1, evaluations=2, runs=2,
                      answer={"rule": {"select": "worst"}, "unit_id": 2, "values": {"x": 1},
                              "objective": -1.0})
    store.close_stage(b, outcome="condition", stop_kind="stage_condition",
                      stop_reason="stage 'b' has no cell", batches=1, evaluations=2, runs=2,
                      answer={"rule": {"select": "worst"}, "cells": 0, "failed": "no cell"})
    store.close()

    body = client.get("/campaigns/st-20260823-000000/search/history").json()
    assert [(s["name"], s["objective_name"], s["outcome"]) for s in body["stages"]] == [
        ("a", "margin", "done"), ("b", "time", "condition"), ("c", "margin", "not_run")]
    assert body["stages"][0]["answer"]["values"] == {"x": 1}
    assert [(b["stage"], b["best_so_far"]) for b in body["batches"]] == [("a", -1.0), ("b", 7.0)]
