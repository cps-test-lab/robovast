# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Combined budget + stopping evaluation (StopConditions) — unit tests."""

import pytest

from robovast.common.config import (BatchesBudget, MetricStop, NoImprovementStop,
                                    SearchConfig, TargetObjectiveStop, TimeBudget)
from robovast.search.stopping import (StopConditions, StopSnapshot,
                                      build_stop_conditions)


def _sc(budget=(), stopping=(), name='failure_rate', direction='maximize'):
    return StopConditions(list(budget), list(stopping), name, direction)


def _snap(batch=1, elapsed=1.0, best=None, metrics=None):
    return StopSnapshot(batch=batch, elapsed=elapsed,
                        best_objective=best, metrics=metrics or {})


# -- budget criteria ---------------------------------------------------------

def test_batches_budget():
    sc = _sc(budget=[BatchesBudget(type='batches', value=3)])
    assert sc.should_stop(_snap(batch=2)) is None
    r = sc.should_stop(_snap(batch=3))
    assert r.kind == 'batches' and 'batches budget' in r.reason


def test_time_budget():
    sc = _sc(budget=[TimeBudget(type='time', seconds=10)])
    assert sc.should_stop(_snap(elapsed=5)) is None
    assert sc.should_stop(_snap(elapsed=10)).kind == 'time'


# -- stopping criteria -------------------------------------------------------

def test_target_objective_maximize():
    sc = _sc(stopping=[TargetObjectiveStop(type='target_objective', value=0.9)])
    assert sc.should_stop(_snap(best=0.5)) is None
    assert sc.should_stop(_snap(best=0.95)).kind == 'target_objective'


def test_target_objective_minimize():
    sc = _sc(stopping=[TargetObjectiveStop(type='target_objective', value=0.1)],
             name='cost', direction='minimize')
    assert sc.should_stop(_snap(best=0.5)) is None
    assert sc.should_stop(_snap(best=0.05)).kind == 'target_objective'


def test_no_improvement_stops_when_flat():
    sc = _sc(stopping=[NoImprovementStop(type='no_improvement', patience=2)])
    fired = [sc.should_stop(_snap(batch=i + 1, best=b)) is not None
             for i, b in enumerate([0.3, 0.5, 0.5, 0.5])]
    assert fired == [False, False, False, True]


def test_no_improvement_resets_on_gain():
    sc = _sc(stopping=[NoImprovementStop(type='no_improvement', patience=2)])
    fired = [sc.should_stop(_snap(batch=i + 1, best=b)) is not None
             for i, b in enumerate([0.1, 0.2, 0.3, 0.4, 0.5])]
    assert not any(fired)


def test_no_improvement_min_delta_minimize():
    sc = _sc(stopping=[NoImprovementStop(type='no_improvement', patience=2, min_delta=0.05)],
             name='cost', direction='minimize')
    fired = [sc.should_stop(_snap(batch=i + 1, best=b)) is not None
             for i, b in enumerate([1.0, 0.5, 0.49, 0.48])]
    assert fired == [False, False, False, True]


def test_metric_op_and_missing_name():
    sc = _sc(stopping=[MetricStop(type='metric', name='coverage', op='>=', value=0.8)])
    assert sc.should_stop(_snap(metrics={'qd_score': 5})) is None      # name absent -> no-op
    assert sc.should_stop(_snap(metrics={'coverage': 0.5})) is None
    assert sc.should_stop(_snap(metrics={'coverage': 0.85})).kind == 'metric'


# -- OR semantics, progress, builder ----------------------------------------

def test_or_returns_first_met():
    sc = _sc(budget=[BatchesBudget(type='batches', value=100)],
             stopping=[TargetObjectiveStop(type='target_objective', value=0.9)])
    assert sc.should_stop(_snap(best=0.95)).kind == 'target_objective'


def test_progress_reports_current_vs_limit():
    sc = _sc(budget=[BatchesBudget(type='batches', value=20),
                     TimeBudget(type='time', seconds=3600)],
             stopping=[MetricStop(type='metric', name='coverage', op='>=', value=0.3)])
    prog = sc.progress(_snap(batch=3, elapsed=95, metrics={'coverage': 0.21}))
    by = {p.label: (p.current, p.limit, p.done) for p in prog}
    assert by['batches'] == (3, 20, False)
    assert by['time'] == (95.0, 3600, False)
    assert by['coverage'] == (0.21, 0.3, False)


def test_needs_metrics_and_has_budget():
    assert _sc(stopping=[MetricStop(type='metric', name='c', value=1)]).needs_metrics
    assert not _sc(budget=[TimeBudget(type='time', seconds=1)]).needs_metrics
    assert _sc(budget=[BatchesBudget(type='batches', value=1)]).has_budget
    assert not _sc(stopping=[TargetObjectiveStop(type='target_objective', value=1)]).has_budget


def test_build_stop_conditions_from_config():
    cfg = SearchConfig(
        strategy='random', search_space={'x': {'type': 'float', 'low': 0, 'high': 1}},
        extract={'plugin': 'failure_rate'},
        objectives=[{'name': 'failure_rate', 'direction': 'maximize'}], per_batch=4,
        budget=[{'batches': 20}],
        stopping=[{'target_objective': 0.9}])
    sc = build_stop_conditions(cfg)
    assert sc.objective_name == 'failure_rate' and sc.direction == 'maximize'
    assert sc.has_budget


def test_requires_at_least_one_criterion():
    with pytest.raises(ValueError, match="budget.*stopping|at least one"):
        SearchConfig(
            strategy='random', search_space={'x': {'type': 'float', 'low': 0, 'high': 1}},
            extract={'plugin': 'failure_rate'}, objectives=[{'name': 'failure_rate'}],
            per_batch=4)


def test_multi_objective_target_rejected():
    with pytest.raises(ValueError, match="single objective"):
        SearchConfig(
            strategy='random', search_space={'x': {'type': 'float', 'low': 0, 'high': 1}},
            extract={'plugin': 'failure_rate'},
            objectives=[{'name': 'a'}, {'name': 'b'}], per_batch=4,
            stopping=[{'target_objective': 1.0}])
