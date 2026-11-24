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
