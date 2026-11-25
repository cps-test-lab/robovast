# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""What a strategy declares it can do, and the refusals that read it before a campaign runs."""

import pytest

from robovast.common.config import SearchConfig
from robovast.common.config_validation import _search_problems
from robovast.search.strategy import build_strategy

_FLOAT_ONLY = (
    "from robovast.search.strategies.random_search import RandomSearch\n"
    "class FloatOnly(RandomSearch):\n"
    "    DIMENSION_TYPES = frozenset({'float'})\n")


def _search(strategy, space, **extra):
    return {"strategy": strategy, "search_space": space,
            "extract": {"plugin": "failure_rate"},
            "objectives": [{"name": "failure_rate"}], "per_batch": 2, **extra}


def test_a_dimension_type_the_strategy_cannot_search_is_refused_by_name(tmp_path):
    (tmp_path / "float_only.py").write_text(_FLOAT_ONLY)
    search = _search("float_only.py:FloatOnly",
                     {"x": {"type": "float", "low": 0, "high": 1},
                      "mode": {"type": "choice", "values": ["a", "b"]}},
                     budget=[{"batches": 1}])
    problems = _search_problems(search, str(tmp_path))
    assert [p["message"] for p in problems] == [
        "float_only.py:FloatOnly cannot search 'mode', a choice: it searches float dimensions"]
    assert problems[0]["field"] == "search.search_space"
    with pytest.raises(ValueError, match="cannot search 'mode', a choice"):
        build_strategy(SearchConfig(**search), str(tmp_path))


@pytest.mark.parametrize("strategy", ["random", "halton", "boundary"])
def test_the_shipped_strategies_search_every_type(strategy):
    space = {"f": {"type": "float", "low": 0, "high": 1},
             "i": {"type": "int", "low": 0, "high": 3},
             "c": {"type": "choice", "values": ["a", "b"]},
             "b": {"type": "bool"}}
    params = {"level": 0.5} if strategy == "boundary" else {}
    search = _search(strategy, space, budget=[{"batches": 1}], strategy_parameters=params)
    assert _search_problems(search, "") == []
    build_strategy(SearchConfig(**search))


_ENDS_ITSELF = (
    "from robovast.search.strategies.random_search import RandomSearch\n"
    "class Finite(RandomSearch):\n"
    "    ENDS_ITSELF = True\n")


def test_a_search_with_no_way_to_end_is_refused_where_its_strategy_is_known(tmp_path):
    search = _search("random", {"x": {"type": "float", "low": 0, "high": 1}})
    cfg = SearchConfig(**search)    # the model alone cannot know the strategy's class
    problems = _search_problems(search, "")
    assert [p["field"] for p in problems] == ["search.budget"]
    assert "at least one 'budget' or 'stopping'" in problems[0]["message"]
    with pytest.raises(ValueError, match="at least one 'budget' or 'stopping'"):
        build_strategy(cfg)


def test_a_strategy_that_ends_by_itself_needs_no_budget(tmp_path):
    (tmp_path / "finite.py").write_text(_ENDS_ITSELF)
    search = _search("finite.py:Finite", {"x": {"type": "float", "low": 0, "high": 1}})
    assert _search_problems(search, str(tmp_path)) == []
    build_strategy(SearchConfig(**search), str(tmp_path))


def _staged(**stage_edits):
    stages = [
        {"name": "shape", "over": ["width"], "strategy": "boundary",
         "strategy_parameters": {"level": 0.0}, "budget": [{"batches": 1}],
         "answer": {"select": "nearest-level"}},
        {"name": "decision", "over": ["config"], "strategy": "grid"},
    ]
    for key, value in stage_edits.items():
        i, field = key.split("__")
        stages[int(i)][field] = value
    return {"search_space": {"width": {"type": "float", "low": 0, "high": 1},
                             "config": {"type": "choice", "values": ["a", "b"],
                                        "initial": "a"}},
            "extract": {"plugin": "failure_rate"}, "objectives": [{"name": "failure_rate"}],
            "per_batch": 2, "stages": stages}


def test_each_stage_s_strategy_is_resolved_and_checked_by_name():
    assert _search_problems(_staged(), "") == []
    SearchConfig(**_staged())

    problems = _search_problems(_staged(**{"1__over": ["width"]}), "")
    assert [(p["field"], p["message"]) for p in problems] == [
        ("search.stages[1].over",
         "stage 'decision': grid cannot search 'width', a float: it searches int, choice, "
         "bool dimensions")]


def test_a_stage_without_an_end_or_with_bad_parameters_is_refused_where_written():
    problems = _search_problems(_staged(**{"0__budget": None,
                                           "1__strategy_parameters": {"points": 2}}), "")
    by_field = {p["field"]: p for p in problems}
    assert by_field["search.stages[0].budget"]["message"].startswith(
        "stage 'shape': a search must define at least one 'budget' or 'stopping'")
    assert by_field["search.stages[1].strategy_parameters"]["stage"] == "search-strategy-params"
    assert by_field["search.stages[1].strategy_parameters"]["message"].startswith(
        "stage 'decision': ")
