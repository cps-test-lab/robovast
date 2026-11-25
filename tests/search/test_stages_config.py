# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""``search.stages`` and ``initial``: what the schema accepts, and every sentence it refuses with."""

import copy

import pytest

from robovast.common.config import SearchConfig

#: The four-stage sketch of docs/search.rst, "A search in stages".
SKETCH = {
    "search_space": {
        "form": {"type": "choice", "values": ["rod", "box"]},
        "width": {"type": "float", "low": 0, "high": 10},
        "speed": {"type": "float", "low": 0.0, "high": 2.0, "initial": 0.0},
        "config": {"type": "choice", "values": ["default", "careful"], "initial": "default"},
    },
    "variations": [{"ParameterVariationList": {
        "sim": ["obstacle.form", "obstacle.width", "human.speed", "nav.config"],
        "values": [["$form", "$width", "$speed", "$config"]]}}],
    "extract": {"plugin": "failure_rate"},
    "objectives": [{"name": "margin", "direction": "minimize"}],
    "per_batch": 4,
    "stages": [
        {"name": "obstacle", "over": ["form", "width"], "strategy": "boundary",
         "strategy_parameters": {"level": 0.0}, "budget": [{"runs": 512}],
         "answer": {"select": "nearest-level", "feasible": "success"}},
        {"name": "human", "over": ["speed"], "strategy": "boundary",
         "strategy_parameters": {"level": 0.0}, "budget": [{"runs": 512}],
         "region": {"band": 5, "min_cells": 3}},
        {"name": "hardest", "over": ["speed"], "within": "human", "strategy": "optuna",
         "objectives": [{"name": "hardest", "direction": "minimize"}],
         "budget": [{"runs": 256}], "answer": {"select": "feasible-min", "feasible": "success"}},
        {"name": "decision", "over": ["config"], "strategy": "grid"},
    ],
}


def _sketch(**edits):
    search = copy.deepcopy(SKETCH)
    for path, value in edits.items():
        node = search
        *parents, last = path.split(".")
        for part in parents:
            node = node[int(part)] if isinstance(node, list) else node[part]
        if value is _DROP:
            del node[int(last) if isinstance(node, list) else last]
        elif isinstance(node, list):
            node[int(last)] = value
        else:
            node[last] = value
    return search


_DROP = object()


def _refused(search) -> str:
    with pytest.raises(ValueError) as info:
        SearchConfig(**search)
    return str(info.value)


def test_the_sketch_is_a_valid_search():
    cfg = SearchConfig(**SKETCH)
    assert [st.name for st in cfg.stages] == ["obstacle", "human", "hardest", "decision"]
    assert cfg.strategy is None and cfg.search_space["speed"].initial == 0.0


def test_a_file_without_stages_is_valid_as_written_today():
    cfg = SearchConfig(strategy="random", search_space={"x": {"type": "float", "low": 0,
                                                              "high": 1}},
                       extract={"plugin": "failure_rate"}, objectives=[{"name": "f"}],
                       per_batch=2, budget=[{"batches": 1}])
    assert cfg.stages is None


@pytest.mark.parametrize("edits, sentence", [
    ({"stages": []}, "search.stages is empty: leave it out for a search in one stage"),
    ({"stages.1.name": "obstacle"},
     "search.stages: stage 'obstacle' is named twice; within: and the record address a stage "
     "by its name"),
    ({"stages.0.over": []}, "stage 'obstacle' searches nothing: over: names at least one dimension"),
    ({"stages.0.over": ["form", "width", "form"]}, "stage 'obstacle' names 'form' twice in over:"),
    ({"stages.3.over": ["foo"]},
     "stage 'decision' searches 'foo', which search_space does not declare; declared: "
     "[config, form, speed, width]"),
    ({"search_space.speed.initial": _DROP},
     "stage 'obstacle' stands on 'speed' before any stage searches it, and search_space.speed "
     "has no initial: give it one"),
    ({"search_space.form.initial": "rod"},
     "search_space.form: initial would change nothing: 'obstacle', the first stage, searches it"),
    ({"search_space.width.initial": "wide"}, "search_space.width: initial 'wide' is not a float"),
    ({"stages.2.within": "humans"},
     "stage 'hardest' searches within 'humans', which is no earlier stage; earlier: "
     "[obstacle, human]"),
    ({"stages.2.within": "obstacle"},
     "stage 'hardest' searches within 'obstacle', which leaves no region: give 'obstacle' a region"),
    ({"stages.0.region": {"band": 1}, "stages.2.within": "obstacle"},
     "stage 'hardest' searches [speed] within 'obstacle', whose region is over [form, width]; a "
     "stage within a region searches that region's dimensions"),
    ({"stages.2.answer": _DROP},
     "stage 'decision' stands on 'speed' at the answer of 'hardest', the last stage to search "
     "it, and 'hardest' has no answer: give it one"),
    ({"stages.2.objectives": [{"name": "a"}, {"name": "b"}]},
     "stage 'hardest' has 2 objectives; an answer and a region read one: give the stage one "
     "objective"),
    ({"stages.2.answer": {"select": "worst", "level": 1.0}},
     "stage 'hardest': answer.level would change nothing: only select nearest-level reads it"),
    ({"stages.0.strategy_parameters": {}, "stages.0.strategy": "halton"},
     "stage 'obstacle': answer select nearest-level needs a level: give answer.level, or a "
     "strategy_parameters.level"),
    ({"stages.1.strategy": "random", "stages.1.strategy_parameters": {}},
     "stage 'human': a region is the cells near a level, and it has none: give region.level, or "
     "a strategy_parameters.level"),
    ({"stages.0.region": {"band": 1, "pad": 0.2}, "stages.0.over": ["form"],
      "stages.0.answer": {"select": "worst"}, "search_space.width.initial": 1.0},
     "stage 'obstacle': region.pad would change nothing: [form] are choices or booleans, which "
     "have no range to widen"),
    ({"budget": [{"runs": 10}]},
     "search.budget is refused with search.stages: each stage states its own, so one here would "
     "bound none of them"),
    ({"strategy": "random"},
     "search.strategy is refused with search.stages: each stage states its own, so one here "
     "would choose for none of them"),
    ({"stopping": [{"target_objective": 0.0}]},
     "search.stopping is refused with search.stages: each stage states its own, so one here "
     "would end none of them"),
    ({"strategy_parameters": {"level": 0.0}},
     "search.strategy_parameters is refused with search.stages: each stage states its own, so "
     "one here would tune none of them"),
    ({"stages.0.name": "Obstacle 1"}, "stage name 'Obstacle 1' must match [a-z][a-z0-9_-]*"),
    ({"stages.0.name": "search"},
     "stage name 'search' is taken: it names the one stage of a search without stages"),
])
def test_every_refusal_names_what_to_change(edits, sentence):
    assert sentence in _refused(_sketch(**edits))


def test_initial_needs_stages():
    search = {"strategy": "random", "extract": {"plugin": "failure_rate"},
              "objectives": [{"name": "f"}], "per_batch": 2,
              "search_space": {"x": {"type": "float", "low": 0, "high": 1, "initial": 0.5}}}
    assert ("search_space.x: initial would change nothing without stages: every dimension is "
            "searched from the first batch") in _refused(search)


def test_a_search_without_stages_names_its_strategy():
    search = {"extract": {"plugin": "failure_rate"}, "objectives": [{"name": "f"}],
              "per_batch": 2, "search_space": {"x": {"type": "float", "low": 0, "high": 1}}}
    assert "search.strategy is required: a search without stages names its strategy" in \
        _refused(search)


@pytest.mark.parametrize("dim, initial, ok", [
    ({"type": "int", "low": 1, "high": 3}, 0, True),       # outside the domain is fine
    ({"type": "int", "low": 1, "high": 3}, 0.5, False),
    ({"type": "int", "low": 1, "high": 3}, True, False),
    ({"type": "bool"}, False, True),
    ({"type": "bool"}, 0, False),
    ({"type": "choice", "values": ["a"]}, "z", True),
    ({"type": "choice", "values": ["a"]}, ["a"], False),
    ({"type": "float", "low": 0, "high": 1}, 3, True),
])
def test_an_initial_is_a_value_of_its_type_wherever_it_lies(dim, initial, ok):
    search = _sketch(**{"search_space.count": {**dim, "initial": initial}})
    if ok:
        SearchConfig(**search)
    else:
        assert "search_space.count: initial" in _refused(search)


def test_a_stage_takes_the_search_s_defaults_and_its_own_overrides():
    cfg = SearchConfig(**_sketch(**{"stages.1.per_batch": 2,
                                    "stages.1.repetitions": {"policy": "fixed"}}))
    assert cfg.stages[0].per_batch is None and cfg.stages[1].per_batch == 2
    assert cfg.stages[2].objectives[0].name == "hardest"


def test_a_stage_key_without_stages_is_an_unknown_key():
    search = {"strategy": "random", "extract": {"plugin": "failure_rate"},
              "objectives": [{"name": "f"}], "per_batch": 2, "over": ["x"],
              "search_space": {"x": {"type": "float", "low": 0, "high": 1}}}
    assert "over" in _refused(search)


@pytest.mark.parametrize("edits, sentence", [
    ({"stages.1.region": {"band": -1}}, "region band must be >= 0, got -1"),
    ({"stages.1.region": {"band": 1, "pad": -0.5}}, "region pad must be >= 0, got -0.5"),
    ({"stages.1.region": {"band": 1, "min_cells": 0}},
     "region min_cells must be >= 1, got 0: a box needs a cell"),
    ({"stages.0.budget": {"runs": 512}},
     "a stage's budget must be a list of single-key mappings"),
    ({"stages.3.stopping": {"no_improvement": 3}},
     "a stage's stopping must be a list of single-key mappings"),
    ({"stages.3.per_batch": 0}, "a stage's per_batch must be >= 1, got 0"),
    ({"stages.3.objectives": []}, "a stage's objectives, when given, declare at least one"),
    ({"stages.3.objectives": [{"name": "a"}, {"name": "b"}],
      "stages.3.stopping": [{"target_objective": 0.0}]},
     "stage 'decision': stopping 'target_objective' requires a single objective, but 2 are "
     "configured"),
])
def test_every_field_refusal_of_a_stage_names_its_bound(edits, sentence):
    assert sentence in _refused(_sketch(**edits))
