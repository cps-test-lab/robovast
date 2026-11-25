# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""A file's stages as configs a strategy is built from, the point they carry, and the rules
applied between them."""

import pytest

from robovast.common.config import SearchConfig
from robovast.common.store import STORE_FILENAME, CampaignStore
from robovast.search import chain
from robovast.search.stages import (IMPLICIT_STAGE, StageCondition, answer_of, carried,
                                    declared_space, evaluation_objectives, full_values,
                                    initial_point, region_of, stages_of)
from robovast.search.strategy import build_strategy

from .test_stages_config import SKETCH


def test_the_sketch_plans_four_stages_with_their_own_configs():
    cfg = SearchConfig(**SKETCH)
    stages = stages_of(cfg)
    assert [(s.name, s.over) for s in stages] == [
        ("obstacle", ("form", "width")), ("human", ("speed",)), ("hardest", ("speed",)),
        ("decision", ("config",))]
    obstacle, human, hardest, decision = stages
    assert list(obstacle.config.search_space) == ["form", "width"]
    assert obstacle.config.per_batch == 4 and obstacle.config.variations is None
    assert [o.name for o in hardest.config.objectives] == ["hardest"]
    assert [o.name for o in human.config.objectives] == ["margin"]
    assert (hardest.within, decision.config.strategy) == ("human", "grid")
    assert obstacle.level(obstacle.answer) == 0.0
    assert [o.name for o in evaluation_objectives(cfg)] == ["margin", "hardest"]
    # Every stage config builds its strategy over a search_space without the initials.
    assert hardest.config.search_space["speed"].initial is None
    for stage in stages:
        build_strategy(stage.config)


def test_a_file_without_stages_is_one_stage_over_the_whole_space():
    cfg = SearchConfig(strategy="random", search_space={"x": {"type": "float", "low": 0,
                                                              "high": 1}},
                       extract={"plugin": "failure_rate"}, objectives=[{"name": "f"}],
                       per_batch=2, budget=[{"batches": 1}])
    [stage] = stages_of(cfg)
    assert (stage.name, stage.over, stage.config) == (IMPLICIT_STAGE, ("x",), cfg)
    assert initial_point(cfg) == {}


def test_the_point_starts_at_the_initials_and_carries_each_answer():
    cfg = SearchConfig(**SKETCH)
    obstacle, human, _, _ = stages_of(cfg)
    point = initial_point(cfg)
    assert point == {"speed": 0.0, "config": "default"}
    assert carried(point, obstacle) == point
    point.update({"form": "box", "width": 3.5})        # obstacle's answer
    assert carried(point, human) == {"config": "default", "form": "box", "width": 3.5}
    assert full_values(carried(point, human), {"speed": 1.2}) == {
        "config": "default", "form": "box", "width": 3.5, "speed": 1.2}


def _stage_record(tmp_path, cells):
    db = tmp_path / STORE_FILENAME
    store = CampaignStore(db)
    cid = store.create_campaign("c", {"search": SKETCH})
    ids = store.declare_stages(cid, [s.described() for s in stages_of(SearchConfig(**SKETCH))])
    batch = store.open_batch(cid, 0, ".", stage_id=ids[1])
    for i, (speed, margin, ok) in enumerate(cells):
        store.record_unit(batch_id=batch, paramset_id=f"p{i}", config_name=f"c{i}",
                          params={"form": "box", "width": 3.5, "speed": speed,
                                  "config": "default"},
                          objectives={"margin": margin}, measures={"success": ok},
                          status="evaluated", result_dir=f"c{i}")
    store.close()
    return chain.load_record(db, objective="margin", stage="human")


def test_a_region_is_the_box_of_the_cells_near_the_level_over_the_declared_domain(tmp_path):
    cfg = SearchConfig(**SKETCH)
    human = stages_of(cfg)[1]
    record = _stage_record(tmp_path, [(0.4, -1.0, 1), (0.8, 2.0, 1), (1.6, 4.0, 0),
                                      (1.9, 9.0, 1)])
    region = region_of(human, record, declared_space(cfg, human))
    assert region["cells"] == 3
    assert region["rule"] == {"level": 0.0, "band": 5, "pad": 0.1, "min_cells": 3}
    assert region["box"] == {"speed": {"type": "float", "low": 0.2, "high": 1.8, "log": False}}
    # The stage within it searches that box.
    hardest = stages_of(cfg)[2]
    assert hardest.config_over(region["box"]).search_space["speed"].high == 1.8


def test_a_region_with_too_few_cells_ends_on_its_condition(tmp_path):
    cfg = SearchConfig(**SKETCH)
    human = stages_of(cfg)[1]
    record = _stage_record(tmp_path, [(0.4, -1.0, 1), (1.9, 9.0, 1)])
    with pytest.raises(StageCondition) as info:
        region_of(human, record, declared_space(cfg, human))
    assert str(info.value) == ("stage 'human' has 1 cell(s) within 5 of the level 0.0 for "
                               "'margin', fewer than the 3 its region needs")
    assert info.value.rule == "region" and info.value.record["cells"] == 1


def test_an_answer_is_one_cell_and_one_with_no_feasible_cell_ends_on_its_condition(tmp_path):
    cfg = SearchConfig(**SKETCH)
    obstacle = stages_of(cfg)[0]
    record = _stage_record(tmp_path, [(0.4, -1.0, 1), (0.8, 0.5, 0)])
    record.stage = "obstacle"
    answer = answer_of(obstacle, record)
    assert answer["values"] == {"form": "box", "width": 3.5}
    assert answer["rule"] == {"select": "nearest-level", "feasible": "success", "level": 0.0}
    assert answer["objective"] == -1.0

    nothing = _stage_record(tmp_path / "none", [(0.4, -1.0, 0)])
    nothing.stage = "obstacle"
    with pytest.raises(StageCondition, match="stage 'obstacle' has no cell with 'success' true"):
        answer_of(obstacle, nothing)
