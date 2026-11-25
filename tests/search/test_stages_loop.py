# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""The controller runs a search stage by stage: the point each stage stands on, what it tells
its strategy, what it records, and how the line ends."""

# pylint: disable=protected-access

import json
import os
import sqlite3

from robovast.common.config import SearchConfig
from robovast.common.store import STORE_FILENAME, CampaignStore
from robovast.execution.backends import RunOptions
from robovast.execution.control_server import ControllerState, Phase, stop_scope_for_phase
from robovast.execution.controller import CampaignController
from robovast.search.repetitions import build_repetition_policy
from robovast.search.stages import evaluation_objectives, initial_point, stages_of
from robovast.search.stopping import build_stop_conditions
from robovast.search.strategy import build_strategy
from robovast.search.types import Evaluation

from .test_loop_and_store import FakeBackend, FakeCompose

SPACE = {
    "form": {"type": "choice", "values": ["rod", "box"]},
    "width": {"type": "float", "low": 0.0, "high": 10.0},
    "speed": {"type": "float", "low": 0.0, "high": 2.0, "initial": 0.0},
    "config": {"type": "choice", "values": ["default", "careful"], "initial": "default"},
}


def margin(v):
    """A known landscape: wider and slower is safer, a box a little more so."""
    return v["width"] - 5.0 - 3.0 * v["speed"] + (1.0 if v["form"] == "box" else 0.0) \
        - (0.5 if v["config"] == "careful" else 0.0)


class LandscapeEvaluator:
    """Scores a cell from its values: ``margin`` and ``hardest`` (the same), ``success``."""

    def __init__(self, objective_names):
        self.objective_names = objective_names
        self.seen = []

    def evaluate(self, config_dir, ps):
        self.seen.append(dict(ps.values))
        m = margin(ps.values)
        objectives = {"margin": m, "hardest": m}
        return Evaluation(params=ps, objectives={n: objectives[n] for n in self.objective_names},
                          measures={"success": m > -2.0}, n_samples=1)


def _search(stages, **extra):
    return SearchConfig(search_space=SPACE, extract={"plugin": "failure_rate"},
                        objectives=[{"name": "margin", "direction": "minimize"}],
                        per_batch=4, seed=3, stages=stages, **extra)


FOUR = [
    {"name": "obstacle", "over": ["form", "width"], "strategy": "boundary",
     "strategy_parameters": {"level": 0.0}, "budget": [{"batches": 3}],
     "answer": {"select": "nearest-level", "feasible": "success"}},
    {"name": "human", "over": ["speed"], "strategy": "boundary",
     "strategy_parameters": {"level": 0.0}, "budget": [{"batches": 3}],
     "region": {"band": 2.0, "min_cells": 2}},
    {"name": "hardest", "over": ["speed"], "within": "human", "strategy": "optuna",
     "objectives": [{"name": "hardest", "direction": "minimize"}], "budget": [{"batches": 2}],
     "answer": {"select": "feasible-min", "feasible": "success"}},
    {"name": "decision", "over": ["config"], "strategy": "grid"},
]


class _Spy:
    """Records what a strategy is told."""

    def __init__(self, strategy, told):
        self._strategy, self._told = strategy, told

    def __getattr__(self, name):
        return getattr(self._strategy, name)

    def tell(self, evaluations):
        self._told.append([dict(e.params.values) for e in evaluations])
        return self._strategy.tell(evaluations)


def _controller(tmp_path, cfg, *, state=None, evaluator=None, told=None):
    stages = stages_of(cfg)

    def factory(stage_cfg):
        strategy = build_strategy(stage_cfg)
        if told is not None:
            strategy = _Spy(strategy, told.setdefault(len(told), []))
        return (strategy, build_stop_conditions(stage_cfg),
                build_repetition_policy(stage_cfg.repetitions, stage_cfg.search_space, 1))

    store = CampaignStore(tmp_path / "camp" / STORE_FILENAME)
    backend = FakeBackend()
    evaluator = evaluator or LandscapeEvaluator([o.name for o in evaluation_objectives(cfg)])
    controller = CampaignController(
        campaign_id="camp", results_dir=str(tmp_path), runs=1, backend=backend,
        options=RunOptions(), store=store, campaign_config_dump={"search": cfg.model_dump()},
        vast_dir=str(tmp_path), evaluator=evaluator, compose=FakeCompose(),
        stages=stages, stage_factory=factory, initial_point=initial_point(cfg), state=state)
    return controller, store, backend, evaluator


def _rows(store, sql, *args):
    con = sqlite3.connect(store.db_path)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, args)]
    finally:
        con.close()


def _stage_units(store, name):
    return [json.loads(r["params_json"]) for r in _rows(
        store, "SELECT u.params_json FROM unit u JOIN batch b ON u.batch_id = b.id "
               "JOIN stage s ON b.stage_id = s.id WHERE s.name = ? AND u.status = 'evaluated' "
               "ORDER BY u.id", name)]


def test_four_stages_run_in_line_each_standing_on_the_answers_before_it(tmp_path):
    told = {}
    controller, store, _, _ = _controller(tmp_path, _search(FOUR), told=told)
    report = controller.run()
    store.close()

    stages = _rows(store, "SELECT * FROM stage ORDER BY idx")
    assert [(s["name"], s["outcome"]) for s in stages] == [
        ("obstacle", "done"), ("human", "done"), ("hardest", "done"), ("decision", "done")]
    obstacle, human, hardest, decision = stages
    answer = json.loads(obstacle["answer_json"])
    assert answer["rule"] == {"select": "nearest-level", "feasible": "success", "level": 0.0}
    assert set(answer["values"]) == {"form", "width"}

    # obstacle stood at the initials; human on obstacle's answer.
    assert json.loads(obstacle["point_json"]) == {"speed": 0.0, "config": "default"}
    assert all(u["speed"] == 0.0 and u["config"] == "default"
               for u in _stage_units(store, "obstacle"))
    assert all((u["form"], u["width"]) == (answer["values"]["form"], answer["values"]["width"])
               for u in _stage_units(store, "human"))

    # hardest searched human's region, and decision stood on hardest's answer.
    region = json.loads(human["region_json"])
    assert json.loads(hardest["space_json"]) == region["box"]
    assert json.loads(hardest["region_from_json"])["stage"] == "human"
    speeds = [u["speed"] for u in _stage_units(store, "hardest")]
    assert all(region["box"]["speed"]["low"] <= v <= region["box"]["speed"]["high"]
               for v in speeds)
    hardest_answer = json.loads(hardest["answer_json"])
    decision_units = [(r["status"], json.loads(r["params_json"])) for r in _rows(
        store, "SELECT u.status, u.params_json FROM unit u JOIN batch b ON u.batch_id = b.id "
               "JOIN stage s ON b.stage_id = s.id WHERE s.name = 'decision' ORDER BY u.id")]
    # Its 'default' cell is the very cell hardest selected, so it is recalled, not run again.
    assert sorted((status, u["config"]) for status, u in decision_units) == [
        ("evaluated", "careful"), ("recalled", "default")]
    assert {u["speed"] for _, u in decision_units} == {hardest_answer["values"]["speed"]}
    assert decision["stop_kind"] == "exhausted"

    # Each strategy was told its cells over its own dimensions only.
    assert {k for batch in told[2] for cell in batch for k in cell} == {"speed"}
    assert {k for batch in told[0] for cell in batch for k in cell} == {"form", "width"}

    # The unit's scalar objective is the proposing stage's.
    assert report.extra["stop"]["kind"] == "exhausted"
    assert [s["name"] for s in report.extra["stages"]] == [
        "obstacle", "human", "hardest", "decision"]
    batches = _rows(store, "SELECT idx, stage_id FROM batch ORDER BY idx")
    assert [b["idx"] for b in batches] == list(range(len(batches)))      # campaign-wide


def test_a_cell_an_earlier_stage_measured_is_recalled_not_run_again(tmp_path):
    stages = [{"name": "first", "over": ["config"], "strategy": "grid",
               "answer": {"select": "worst"}},
              {"name": "again", "over": ["config"], "strategy": "grid"}]
    space = {**SPACE, "form": {**SPACE["form"], "initial": "rod"},
             "width": {**SPACE["width"], "initial": 4.0}}
    space["config"] = {"type": "choice", "values": ["default", "careful"]}
    cfg = SearchConfig(search_space=space, extract={"plugin": "failure_rate"},
                       objectives=[{"name": "margin", "direction": "minimize"}],
                       per_batch=2, stages=stages)
    controller, store, backend, evaluator = _controller(tmp_path, cfg)
    controller.run()
    store.close()
    assert len(evaluator.seen) == 2 and len(backend.batch_runs) == 1
    recalled = _rows(store, "SELECT u.status FROM unit u JOIN batch b ON u.batch_id = b.id "
                            "JOIN stage s ON b.stage_id = s.id WHERE s.name = 'again'")
    assert [r["status"] for r in recalled] == ["recalled", "recalled"]


def test_an_answer_no_cell_satisfies_ends_the_campaign_there_by_name(tmp_path):
    stages = [dict(FOUR[0], answer={"select": "worst", "feasible": "never"})] + FOUR[1:]
    evaluator = LandscapeEvaluator(["margin", "hardest"])
    original = evaluator.evaluate

    def with_never(config_dir, ps):
        ev = original(config_dir, ps)
        ev.measures["never"] = False
        return ev
    evaluator.evaluate = with_never
    state = ControllerState()
    controller, store, backend, _ = _controller(tmp_path, _search(stages), state=state,
                                                evaluator=evaluator)
    report = controller.run()
    store.close()
    stages = _rows(store, "SELECT name, outcome, stop_kind, answer_json FROM stage ORDER BY idx")
    assert [(s["name"], s["outcome"]) for s in stages] == [
        ("obstacle", "condition"), ("human", "not_run"), ("hardest", "not_run"),
        ("decision", "not_run")]
    assert json.loads(stages[0]["answer_json"])["cells"] == 0
    [campaign] = _rows(store, "SELECT stop_kind, stop_reason FROM campaign")
    assert campaign["stop_kind"] == "stage_condition"
    assert campaign["stop_reason"] == ("stage 'obstacle' has no cell with 'never' true to select "
                                       "from; not run: human, hardest, decision")
    assert len(backend.batch_runs) == 3                  # obstacle's runs are kept
    stop = state.snapshot().stop
    assert (stop["stage"], stop["rule"], stop["not_run"]) == (
        "obstacle", "answer", ["human", "hardest", "decision"])
    assert report.extra["stop"]["not_run"] == ["human", "hardest", "decision"]


def test_a_region_below_min_cells_ends_the_campaign_with_the_cells_it_saw(tmp_path):
    stages = FOUR[:1] + [dict(FOUR[1], region={"band": 0.001, "min_cells": 50})] + FOUR[2:]
    controller, store, _, _ = _controller(tmp_path, _search(stages))
    controller.run()
    store.close()
    human = _rows(store, "SELECT outcome, region_json FROM stage WHERE name = 'human'")[0]
    region = json.loads(human["region_json"])
    assert human["outcome"] == "condition"
    assert region["rule"]["min_cells"] == 50 and region["cells"] < 50
    assert "fewer than the 50 its region needs" in region["failed"]
    assert [r["outcome"] for r in _rows(store, "SELECT outcome FROM stage ORDER BY idx")] == [
        "done", "condition", "not_run", "not_run"]


def test_a_stop_in_the_middle_of_a_stage_leaves_the_stages_after_it_not_run(tmp_path):
    state = ControllerState()
    evaluator = LandscapeEvaluator(["margin", "hardest"])
    original = evaluator.evaluate

    def stop_in_human(config_dir, ps):
        if ps.values["speed"] != 0.0:                   # human searches speed
            state.request_stop()
        return original(config_dir, ps)
    evaluator.evaluate = stop_in_human
    controller, store, _, _ = _controller(tmp_path, _search(FOUR), state=state,
                                          evaluator=evaluator)
    controller.run()
    store.close()
    assert [(r["name"], r["outcome"]) for r in _rows(
        store, "SELECT name, outcome FROM stage ORDER BY idx")] == [
        ("obstacle", "done"), ("human", "stopped"), ("hardest", "not_run"),
        ("decision", "not_run")]
    assert _rows(store, "SELECT stop_kind FROM campaign")[0]["stop_kind"] == "external"


def test_a_stop_the_service_sends_in_the_second_stage_stops_its_runs(tmp_path):
    """A stop lands where ``ClusterService.stop`` aims it: at the scope the phase names."""
    state = ControllerState()
    evaluator = LandscapeEvaluator(["margin", "hardest"])
    original = evaluator.evaluate
    seen = []

    def stop_in_human(config_dir, ps):
        if ps.values["speed"] != 0.0 and not seen:      # human searches speed
            snap = state.snapshot()
            seen.append((snap.phase, snap.stop))
            state.request_stop(stop_scope_for_phase(snap.phase))
        return original(config_dir, ps)
    evaluator.evaluate = stop_in_human
    controller, store, _, _ = _controller(tmp_path, _search(FOUR), state=state,
                                          evaluator=evaluator)
    controller.run()
    store.close()
    assert seen == [(Phase.RUNNING, None)]
    assert [(r["name"], r["outcome"]) for r in _rows(
        store, "SELECT name, outcome FROM stage ORDER BY idx")] == [
        ("obstacle", "done"), ("human", "stopped"), ("hardest", "not_run"),
        ("decision", "not_run")]
    stop = state.snapshot().stop
    assert (stop["kind"], stop["stage"], stop["not_run"]) == (
        "external", "human", ["hardest", "decision"])


def test_a_restart_in_the_middle_of_the_second_stage_carries_on_identically(tmp_path):
    whole, store, _, _ = _controller(tmp_path / "whole", _search(FOUR))
    whole.run()
    store.close()

    class _Killed(BaseException):
        pass

    evaluator = LandscapeEvaluator(["margin", "hardest"])
    original = evaluator.evaluate
    calls = {"human": 0}

    def die_in_human(config_dir, ps):
        if ps.values["speed"] != 0.0 and "form" in ps.values:
            calls["human"] += 1
            if calls["human"] == 6:                     # the second batch of human
                raise _Killed()
        return original(config_dir, ps)
    evaluator.evaluate = die_in_human
    first, store, _, _ = _controller(tmp_path / "cut", _search(FOUR), evaluator=evaluator)
    try:
        first.run()
    except _Killed:
        pass
    store.close()
    assert calls["human"] == 6
    started = _rows(store, "SELECT started_at FROM stage WHERE name = 'human'")[0]["started_at"]

    second, store2, _, _ = _controller(tmp_path / "cut", _search(FOUR))
    second.run()
    store2.close()

    def stages_of_run(root):
        return [(r["name"], r["outcome"], r["answer_json"], r["region_json"]) for r in _rows(
            CampaignStore(root / "camp" / STORE_FILENAME),
            "SELECT * FROM stage ORDER BY idx")]
    assert stages_of_run(tmp_path / "cut") == stages_of_run(tmp_path / "whole")
    # The resumed stage's time budget is measured from when it began, not from the restart.
    assert _rows(store2, "SELECT started_at FROM stage WHERE name = 'human'")[0][
        "started_at"] == started
    assert os.path.isfile(tmp_path / "cut" / "camp" / STORE_FILENAME)


def test_a_restart_between_two_stages_goes_on_with_the_next_not_the_closed_one(tmp_path):
    """Killed after obstacle closed and before human opened: obstacle is taken over, not run
    one batch more. elapsed_s means what it means for any resumed search: about the campaign's
    age, since the record had batches when this run began."""
    import time

    class _Killed(BaseException):
        pass

    first, store, _, _ = _controller(tmp_path, _search(FOUR))
    begin, calls = first._begin_stage, []

    def begin_then_die(*args, **kwargs):
        calls.append(args[1].name)
        if len(calls) == 2:
            raise _Killed()
        return begin(*args, **kwargs)
    first._begin_stage = begin_then_die
    try:
        first.run()
    except _Killed:
        pass
    store.close()
    stage_batches = "SELECT COUNT(*) AS n FROM batch b JOIN stage s ON b.stage_id = s.id " \
                    "WHERE s.name = 'obstacle'"
    assert _rows(store, stage_batches)[0]["n"] == 3
    assert [r["outcome"] for r in _rows(store, "SELECT outcome FROM stage ORDER BY idx")] == [
        "done", "not_run", "not_run", "not_run"]
    con = sqlite3.connect(store.db_path)
    con.execute("UPDATE campaign SET created_at = ?", (time.time() - 10_000,))
    con.commit()
    con.close()

    second, store2, _, _ = _controller(tmp_path, _search(FOUR))
    report = second.run()
    store2.close()
    assert _rows(store2, stage_batches)[0]["n"] == 3        # obstacle ran no batch more
    assert [r["outcome"] for r in _rows(store2, "SELECT outcome FROM stage ORDER BY idx")] == [
        "done"] * 4
    # The search's own figure, which record_outcome writes; run() then stamps the row with this
    # process's run time, for a search as for a batch campaign.
    assert report.extra["stop"]["elapsed_s"] >= 10_000


def test_a_rule_that_cannot_be_applied_fails_the_campaign_and_closes_its_stage(tmp_path):
    import pytest

    from robovast.search.chain import ChainError
    stages = [dict(FOUR[0], answer={"select": "worst", "feasible": "nowhere"})] + FOUR[1:]
    controller, store, _, _ = _controller(tmp_path, _search(stages))
    with pytest.raises(ChainError, match="carry no measure 'nowhere'"):
        controller.run()
    store.close()
    assert [(r["name"], r["outcome"]) for r in _rows(
        store, "SELECT name, outcome FROM stage ORDER BY idx")] == [
        ("obstacle", "error"), ("human", "not_run"), ("hardest", "not_run"),
        ("decision", "not_run")]
    [campaign] = _rows(store, "SELECT stop_kind, stop_reason FROM campaign")
    assert campaign["stop_kind"] == "error"
    assert campaign["stop_reason"].startswith("stage 'obstacle': ")


def test_a_fresh_search_records_its_loop_time(tmp_path):
    controller, _, _, _ = _controller(tmp_path, _search(FOUR))
    assert 0 < controller.run().extra["stop"]["elapsed_s"] < 1_000


class _Killed(BaseException):
    pass


def _kill_once(controller, method):
    original, calls = getattr(controller, method), []

    def die(*args, **kwargs):
        calls.append(1)
        raise _Killed()
    setattr(controller, method, die)
    return original


def _stage_table(store):
    return [(r["name"], r["outcome"], r["stop_kind"], r["batches"]) for r in _rows(
        store, "SELECT name, outcome, stop_kind, batches FROM stage ORDER BY idx")]


def test_a_restart_after_the_last_stage_closed_records_its_outcome_and_runs_nothing(tmp_path):
    """Killed after decision closed and before the campaign's outcome was recorded."""
    first, store, _, _ = _controller(tmp_path, _search(FOUR))
    _kill_once(first, "_finish_search")
    try:
        first.run()
    except _Killed:
        pass
    store.close()
    before = _stage_table(store)
    assert [o for _, o, _, _ in before] == ["done"] * 4
    assert _rows(store, "SELECT stop_kind FROM campaign")[0]["stop_kind"] is None

    second, store2, backend, _ = _controller(tmp_path, _search(FOUR))
    report = second.run()
    store2.close()
    assert backend.batch_runs == []                       # nothing ran again
    assert _stage_table(store2) == before
    assert _rows(store2, "SELECT stop_kind FROM campaign")[0]["stop_kind"] == "exhausted"
    assert report.extra["stop"]["kind"] == "exhausted"


def test_a_restart_after_a_stage_ended_on_its_condition_ends_the_campaign_as_it_did(tmp_path):
    stages = FOUR[:1] + [dict(FOUR[1], region={"band": 0.001, "min_cells": 50})] + FOUR[2:]
    whole, store, _, _ = _controller(tmp_path / "whole", _search(stages))
    whole.run()
    store.close()
    [ended] = _rows(store, "SELECT stop_kind, stop_reason FROM campaign")

    first, store, _, _ = _controller(tmp_path / "cut", _search(stages))
    _kill_once(first, "_end_on_condition")
    try:
        first.run()
    except _Killed:
        pass
    store.close()
    before = _stage_table(store)
    assert [o for _, o, _, _ in before] == ["done", "condition", "not_run", "not_run"]

    state = ControllerState()
    second, store2, backend, _ = _controller(tmp_path / "cut", _search(stages), state=state)
    second.run()
    store2.close()
    assert backend.batch_runs == []                       # human is not run again
    assert _stage_table(store2) == before
    assert _rows(store2, "SELECT stop_kind, stop_reason FROM campaign")[0] == ended
    assert (state.snapshot().stop["stage"], state.snapshot().stop["not_run"]) == (
        "human", ["hardest", "decision"])
