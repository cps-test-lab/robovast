# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""The grid strategy: every cell of an int, choice and bool space, once, then the end."""

import sqlite3

import pytest

from robovast.common.config import SearchConfig
from robovast.common.config_validation import _search_problems
from robovast.search.history import RecordedBatch
from robovast.search.strategy import build_strategy
from robovast.search.types import Evaluation

from .test_loop_and_store import _search_controller

_SPACE = {"mode": {"type": "choice", "values": ["a", "b"]},
          "n": {"type": "int", "low": 1, "high": 5, "step": 2},
          "on": {"type": "bool"}}


def _cfg(per_batch=4, space=None, **extra):
    return SearchConfig(strategy="grid", search_space=space or _SPACE,
                        extract={"plugin": "failure_rate"},
                        objectives=[{"name": "failure_rate"}], per_batch=per_batch, **extra)


def test_the_cells_are_the_product_in_declared_order_sliced_per_batch():
    grid = build_strategy(_cfg())
    assert grid.cells == 12
    first = [ps.values for ps in grid.ask(4)]
    assert first == [{"mode": "a", "n": 1, "on": False}, {"mode": "a", "n": 1, "on": True},
                     {"mode": "a", "n": 3, "on": False}, {"mode": "a", "n": 3, "on": True}]
    rest = grid.ask(4)
    assert not grid.exhausted
    rest += grid.ask(4)
    assert grid.exhausted and len(rest) == 8
    assert rest[-1].values == {"mode": "b", "n": 5, "on": True}
    assert grid.ask(4) == []


def test_a_float_is_refused_before_the_run():
    space = {"x": {"type": "float", "low": 0, "high": 1}}
    search = {"strategy": "grid", "search_space": space, "extract": {"plugin": "failure_rate"},
              "objectives": [{"name": "failure_rate"}], "per_batch": 2}
    problems = _search_problems(search, "")
    assert [p["message"] for p in problems] == [
        "grid cannot search 'x', a float: it searches int, choice, bool dimensions"]
    with pytest.raises(ValueError, match="grid cannot search 'x', a float"):
        build_strategy(_cfg(space=space))


def test_a_grid_takes_no_parameters():
    search = {"strategy": "grid", "search_space": _SPACE, "extract": {"plugin": "failure_rate"},
              "objectives": [{"name": "failure_rate"}], "per_batch": 2,
              "strategy_parameters": {"points": 3}}
    assert [p["stage"] for p in _search_problems(search, "")] == ["search-strategy-params"]


def test_a_resumed_grid_carries_on_where_it_stopped():
    live = build_strategy(_cfg())
    batches = []
    for _ in range(2):
        proposed = live.ask(4)
        evs = [Evaluation(params=ps, objectives={"failure_rate": 0.0}) for ps in proposed]
        live.tell(evs)
        batches.append(RecordedBatch(asked=len(proposed), evaluations=evs))
    resumed = build_strategy(_cfg())
    resumed.resume(batches)
    assert [ps.values for ps in resumed.ask(4)] == [ps.values for ps in live.ask(4)]
    assert resumed.exhausted and live.exhausted


def test_the_loop_ends_a_grid_as_exhausted_and_a_budget_cuts_it_short(tmp_path):
    space = {"mode": {"type": "choice", "values": ["a", "b", "c"]}}
    cfg = _cfg(per_batch=2, space=space)
    controller, store, _ = _search_controller(cfg, tmp_path / "full")
    report = controller.run()
    store.close()
    assert report.extra["stop"]["kind"] == "exhausted"
    assert len(report.evaluations) == 3
    con = sqlite3.connect(store.db_path)
    assert con.execute("SELECT stop_kind, batches FROM campaign").fetchone() == ("exhausted", 2)
    con.close()

    cut = _cfg(per_batch=2, space=space, budget=[{"batches": 1}])
    controller, store, _ = _search_controller(cut, tmp_path / "pilot")
    report = controller.run()
    store.close()
    assert report.extra["stop"]["kind"] == "batches"
    assert len(report.evaluations) == 2
