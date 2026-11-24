# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The status report of a search in stages: which stage it is in, and the current stage's trend."""

from click.testing import CliRunner

from robovast.client.campaign_report import attach_objective_history, status_to_dict
from robovast.client.status import Status
from robovast.service.interface import SearchHistory


class _Client:
    def __init__(self, history, status=None):
        self.history, self.status = history, status

    def get_search_history(self, campaign_id):
        return self.history

    def get_status(self, campaign_id):
        return self.status


def _history():
    return SearchHistory(
        stages=[
            {"idx": 0, "name": "obstacle", "over": ["form", "width"], "objective_name": "margin",
             "direction": "minimize", "outcome": "done",
             "answer": {"values": {"form": "box", "width": 3.5}, "unit_id": 4,
                        "rule": {"select": "worst"}, "objective": -1.0}},
            {"idx": 1, "name": "human", "over": ["speed"], "objective_name": "time",
             "direction": "maximize", "outcome": "running"},
            {"idx": 2, "name": "decision", "over": ["config"], "outcome": "not_run"}],
        batches=[
            {"idx": 0, "stage": "obstacle", "n_units": 2, "n_scored": 2, "min": -1.0, "max": 2.0,
             "mean": 0.5, "best_so_far": -1.0, "complete": True},
            {"idx": 1, "stage": "human", "n_units": 2, "n_scored": 2, "min": 3.0, "max": 5.0,
             "mean": 4.0, "best_so_far": 5.0, "complete": True},
            {"idx": 2, "stage": "human", "n_units": 2, "n_scored": 2, "min": 1.0, "max": 4.0,
             "mean": 2.5, "best_so_far": 5.0, "complete": True}])


def test_the_trend_is_the_current_stage_s_and_every_stage_is_listed():
    result = {}
    attach_objective_history(result, _Client(_history()), "c")
    assert (result["objective_name"], result["objective_direction"]) == ("time", "maximize")
    assert result["batches_since_improvement"] == 1         # human's best has not moved since
    assert [b["stage"] for b in result["objective_history"]] == ["obstacle", "human", "human"]
    assert result["search_stages"] == [
        {"name": "obstacle", "over": ["form", "width"], "outcome": "done", "objective": "margin",
         "answer": {"form": "box", "width": 3.5}},
        {"name": "human", "over": ["speed"], "outcome": "running", "objective": "time"},
        {"name": "decision", "over": ["config"], "outcome": "not_run"}]


def test_the_status_names_the_stage_a_search_is_in():
    st = Status(phase="running", mode="search", batches_done=3,
                search_stage={"idx": 1, "name": "human", "of": 3})
    assert status_to_dict("c", "service", st)["search_stage"] == {
        "idx": 1, "name": "human", "of": 3}


def test_vast_campaign_status_prints_the_stage(monkeypatch):
    from robovast.client import campaign_cli
    st = Status(phase="running", mode="search", search_stage={"idx": 1, "name": "human", "of": 3})
    client = _Client(_history(), st)

    class _Ctx:
        def __enter__(self):
            return client, "test"

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(campaign_cli, "service_client", _Ctx)
    out = CliRunner().invoke(campaign_cli.campaign, ["status", "c"]).output
    assert "  stage     2/3 human" in out
