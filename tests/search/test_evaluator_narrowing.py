# Copyright (C) 2026 Frederik Pasch
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

"""``Evaluator`` narrows an extractor's report to the objectives the campaign DECLARED.

An extractor may return more than it was asked for. Until it was narrowed here, that extra
travelled the whole way into the store as though it were an objective, and the damage was
silent and total: ``record_unit`` lifts the queryable ``unit.objective`` only out of a
single-objective dict, so a single-objective search whose extractor also reported two
diagnostics wrote NULL into every row -- and ``run_view.objective``, ``runs.objective`` and
the per-batch objective trajectory behind the campaign card's chart all went with it, while
the campaign reported itself as running perfectly.
"""
import pytest

from robovast.common.config import SearchConfig
from robovast.search.evaluator import Evaluator
from robovast.search.extractor import ExtractResult
from robovast.search.types import ParamSet


def _evaluator(objectives, reported, measures=None):
    """An Evaluator over *objectives*, whose extractor reports *reported* + *measures*."""
    cfg = SearchConfig(
        strategy="random",
        search_space={"x": {"type": "float", "low": 0, "high": 1}},
        extract={"plugin": "failure_rate"},
        objectives=objectives, per_batch=1, budget=[{"batches": 1}], seed=0,
    )
    ev = Evaluator(cfg)
    ev.extractor = _Stub(reported, measures or {})
    return ev


class _Stub:
    def __init__(self, objectives, measures):
        self._result = ExtractResult(objectives=objectives, measures=measures)

    def extract(self, config_dir):  # noqa: ARG002 - the directory is irrelevant here
        return self._result


NAV = [{"name": "robustness", "direction": "minimize"}]
# What a nav extractor actually reports: the declared objective, plus two diagnostics.
NAV_REPORT = {"robustness": -0.09, "failure_rate": 0.0, "time_to_goal": 24.5}


def test_objectives_hold_the_declared_names_and_nothing_else(tmp_path):
    """The dict's SIZE is load-bearing downstream, so it must mean the declaration.

    `record_unit` reads exactly this to decide whether there is a scalar objective to lift.
    With the diagnostics still in, it saw three values, concluded "multi-objective", and
    stored no objective at all for a search that has precisely one.
    """
    got = _evaluator(NAV, NAV_REPORT).evaluate(tmp_path, ParamSet(id="p", values={}))
    assert got.objectives == {"robustness": -0.09}
    assert len(got.objectives) == 1, "one declared objective means one entry"


def test_the_extras_survive_as_measures_rather_than_being_dropped(tmp_path):
    """The extractor measured them; narrowing must not silently discard a measurement."""
    got = _evaluator(NAV, NAV_REPORT).evaluate(tmp_path, ParamSet(id="p", values={}))
    assert got.measures == {"failure_rate": 0.0, "time_to_goal": 24.5}


def test_declared_measures_are_kept_alongside_the_extras(tmp_path):
    got = _evaluator(NAV, NAV_REPORT, measures={"coverage": 0.7}).evaluate(
        tmp_path, ParamSet(id="p", values={}))
    assert got.measures == {"coverage": 0.7, "failure_rate": 0.0, "time_to_goal": 24.5}


def test_objectives_follow_the_declared_order_not_the_extractor_s(tmp_path):
    """Multi-objective strategies index the front by position, so the order is the .vast's."""
    two = [{"name": "clearance", "direction": "maximize"},
           {"name": "time", "direction": "minimize"}]
    got = _evaluator(two, {"time": 3.0, "clearance": 0.4, "noise": 1.0}).evaluate(
        tmp_path, ParamSet(id="p", values={}))
    assert list(got.objectives) == ["clearance", "time"]
    assert got.measures == {"noise": 1.0}


def test_a_name_that_is_both_a_measure_and_an_extra_is_a_defect(tmp_path):
    """Merging would have to pick a winner silently; the same refusal a missing objective gets."""
    ev = _evaluator(NAV, {"robustness": -0.09, "coverage": 0.2}, measures={"coverage": 0.9})
    with pytest.raises(ValueError, match="coverage"):
        ev.evaluate(tmp_path, ParamSet(id="p", values={}))


def test_a_missing_declared_objective_still_raises(tmp_path):
    """Narrowing must not turn "the extractor never returned it" into a KeyError."""
    ev = _evaluator(NAV, {"failure_rate": 0.0})
    with pytest.raises(ValueError, match="robustness"):
        ev.evaluate(tmp_path, ParamSet(id="p", values={}))
