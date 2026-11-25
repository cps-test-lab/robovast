# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""A search that ended before its last stage is shown as such everywhere: which stage, which
rule, and that the stages after it did not run -- never as a campaign that ran every stage."""

from click.testing import CliRunner

from robovast.client.campaign_report import status_to_dict, summary_to_dict
from robovast.common.store import read_stage_end
from robovast.execution.control_server import ControllerState
from robovast.execution.status_recovery import reconstruct_status_from_disk
from robovast.service.interface import CampaignSummary

from .test_stages_loop import FOUR, LandscapeEvaluator, _controller, _search


def _ended_on_region(tmp_path, state=None):
    stages = FOUR[:1] + [dict(FOUR[1], region={"band": 0.001, "min_cells": 50})] + FOUR[2:]
    controller, store, _, _ = _controller(tmp_path, _search(stages), state=state)
    controller.run()
    store.close()
    return tmp_path / "camp"


def test_the_record_says_where_the_line_ended(tmp_path):
    end = read_stage_end(_ended_on_region(tmp_path))
    assert {k: end[k] for k in ("stage", "idx", "of", "rule", "not_run")} == {
        "stage": "human", "idx": 1, "of": 4, "rule": "region",
        "not_run": ["hardest", "decision"]}
    assert "fewer than the 50 its region needs" in end["reason"]


def test_a_campaign_that_ran_every_stage_has_no_end_to_report(tmp_path):
    controller, store, _, _ = _controller(tmp_path, _search(FOUR))
    controller.run()
    store.close()
    assert read_stage_end(tmp_path / "camp") is None


def test_the_status_read_from_disk_names_the_stage_its_rule_and_the_stages_not_run(tmp_path):
    status = reconstruct_status_from_disk(_ended_on_region(tmp_path))
    assert (status.stop["stage"], status.stop["rule"], status.stop["not_run"]) == (
        "human", "region", ["hardest", "decision"])
    report = status_to_dict("c", "service", status)
    assert (report["ended_at_stage"], report["stage_rule"], report["stages_not_run"]) == (
        "human", "region", ["hardest", "decision"])


def test_the_live_status_says_the_same_as_the_record(tmp_path):
    state = ControllerState()
    _ended_on_region(tmp_path, state=state)
    stop = state.snapshot().stop
    assert (stop["kind"], stop["stage"], stop["idx"], stop["of"], stop["rule"]) == (
        "stage_condition", "human", 1, 4, "region")


def test_an_external_stop_mid_stage_is_an_end_at_that_stage_too(tmp_path):
    state = ControllerState()
    evaluator = LandscapeEvaluator(["margin", "hardest"])
    original = evaluator.evaluate

    def stop_in_human(config_dir, ps):
        if ps.values["speed"] != 0.0:
            state.request_stop()
        return original(config_dir, ps)
    evaluator.evaluate = stop_in_human
    controller, store, _, _ = _controller(tmp_path, _search(FOUR), state=state,
                                          evaluator=evaluator)
    controller.run()
    store.close()
    stop = state.snapshot().stop
    assert (stop["kind"], stop["stage"], stop["rule"], stop["not_run"]) == (
        "external", "human", "stopped", ["hardest", "decision"])
    assert read_stage_end(tmp_path / "camp")["rule"] == "stopped"


def test_the_listing_and_the_cli_name_the_stage(tmp_path, monkeypatch):
    end = read_stage_end(_ended_on_region(tmp_path))
    summary = CampaignSummary(campaign_id="c", phase="finished", mode="search",
                              ended_at_stage=end)
    entry = summary_to_dict(summary)
    assert (entry["status"], entry["ended_at_stage"], entry["stages_not_run"]) == (
        "finished", "human", ["hardest", "decision"])

    from robovast.client import campaign_cli
    status = reconstruct_status_from_disk(tmp_path / "camp")

    class _Client:
        def get_status(self, campaign_id):
            return status

        def get_search_history(self, campaign_id):
            from robovast.service.interface import SearchHistory
            return SearchHistory(unavailable="no_store")

    class _Ctx:
        def __enter__(self):
            return _Client(), "test"

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(campaign_cli, "service_client", _Ctx)
    out = CliRunner().invoke(campaign_cli.campaign, ["status", "c"]).output
    assert "  ended     at stage 2/4 human (region): stage 'human' has" in out


def test_a_stopped_search_without_stages_shows_no_stage_end_live_or_recovered(tmp_path):
    """Its one stage is the whole search: a stop ends the search, not a line of stages."""
    from robovast.common.config import SearchConfig
    from .test_stages_loop import SPACE

    space = {name: {k: v for k, v in dim.items() if k != "initial"}
             for name, dim in SPACE.items()}
    cfg = SearchConfig(strategy="random", search_space=space, extract={"plugin": "failure_rate"},
                       objectives=[{"name": "margin", "direction": "minimize"}], per_batch=2,
                       seed=1, budget=[{"batches": 5}])
    state = ControllerState()
    evaluator = LandscapeEvaluator(["margin"])
    original = evaluator.evaluate

    def stop_at_once(config_dir, ps):
        state.request_stop()
        return original(config_dir, ps)
    evaluator.evaluate = stop_at_once
    controller, store, _, _ = _controller(tmp_path, cfg, state=state, evaluator=evaluator)
    controller.run()
    store.close()

    live = state.snapshot().stop
    assert live["kind"] == "external" and "stage" not in live
    assert read_stage_end(tmp_path / "camp") is None
    recovered = reconstruct_status_from_disk(tmp_path / "camp")
    assert "stage" not in (recovered.stop or {})
    assert "ended_at_stage" not in status_to_dict("c", "service", recovered)
