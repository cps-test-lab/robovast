# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Combined budget + stopping evaluation (StopConditions) — unit tests."""

import pytest

from robovast.common.config import (BatchesBudget, EvaluationsBudget, MetricStop,
                                    NoImprovementStop, RunsBudget, SearchConfig,
                                    TargetObjectiveStop, TimeBudget)
from robovast.search.stopping import StopConditions, StopSnapshot, build_stop_conditions


def _sc(budget=(), stopping=(), name='failure_rate', direction='maximize'):
    return StopConditions(list(budget), list(stopping), name, direction)


def _snap(batch=1, elapsed=1.0, best=None, metrics=None, evaluations=0, runs=0):
    return StopSnapshot(batch=batch, elapsed=elapsed,
                        best_objective=best, metrics=metrics or {},
                        evaluations=evaluations, runs=runs)


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


def test_evaluations_budget_counts_parameter_sets():
    sc = _sc(budget=[EvaluationsBudget(type='evaluations', value=20)])
    assert sc.should_stop(_snap(evaluations=19)) is None
    r = sc.should_stop(_snap(evaluations=20))
    assert r.kind == 'evaluations' and 'evaluations budget' in r.reason


def test_runs_budget_counts_executions():
    """`runs` bounds wall-clock; it is NOT batches x per_batch once reps go adaptive."""
    sc = _sc(budget=[RunsBudget(type='runs', value=150)])
    assert sc.should_stop(_snap(runs=149)) is None
    assert sc.should_stop(_snap(runs=150)).kind == 'runs'


def test_evaluations_and_runs_are_independent():
    """A cell evaluated once may cost many runs; the two caps must not be conflated."""
    sc = _sc(budget=[EvaluationsBudget(type='evaluations', value=100),
                     RunsBudget(type='runs', value=10)])
    # few evaluations, but their repetitions already blew the run cap
    assert sc.should_stop(_snap(evaluations=4, runs=10)).kind == 'runs'


def test_run_budget_progress_reports_current_vs_limit():
    sc = _sc(budget=[RunsBudget(type='runs', value=150)])
    (cp,) = sc.progress(_snap(runs=30))
    assert (cp.kind, cp.label, cp.current, cp.limit, cp.done) == ('runs', 'runs', 30, 150, False)


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


def test_evaluations_and_runs_scalar_shorthand():
    """`- runs: 150` is shorthand for the full mapping, like batches/time."""
    cfg = SearchConfig(strategy='random',
                       search_space={'a': {'type': 'float', 'low': 0.0, 'high': 1.0}},
                       extract={'plugin': 'failure_rate'},
                       objectives=[{'name': 'failure_rate'}], per_batch=4,
                       budget=[{'evaluations': 20}, {'runs': 150}])
    kinds = {(c.type, c.value) for c in cfg.budget}
    assert kinds == {('evaluations', 20), ('runs', 150)}


@pytest.mark.parametrize('kind', ['evaluations', 'runs'])
def test_run_and_evaluation_budgets_reject_non_positive(kind):
    with pytest.raises(ValueError):
        SearchConfig(strategy='random',
                     search_space={'a': {'type': 'float', 'low': 0.0, 'high': 1.0}},
                     extract={'plugin': 'failure_rate'},
                     objectives=[{'name': 'failure_rate'}], per_batch=4,
                     budget=[{kind: 0}])


# -- the comparison each criterion fires on ----------------------------------


def _rows(sc, snap):
    return {p.label: p for p in sc.progress(snap)}


def test_resource_caps_and_no_improvement_fire_at_or_above_their_limit():
    """Five of the seven kinds fire at ``>=``, which is why that is the reader's fallback for a
    status written before ``op`` existed -- the fallback is the old behaviour, not a guess."""
    sc = _sc(budget=[BatchesBudget(type='batches', value=10),
                     TimeBudget(type='time', seconds=600),
                     EvaluationsBudget(type='evaluations', value=20),
                     RunsBudget(type='runs', value=40)],
             stopping=[NoImprovementStop(type='no_improvement', patience=3)])
    rows = _rows(sc, _snap(best=0.5))
    for label in ('batches', 'time', 'evaluations', 'runs', 'stale_batches'):
        assert rows[label].op == '>=', label


def test_target_objective_reports_the_direction_it_is_compared_on():
    """A minimize search reaches its target from ABOVE, so the row must say ``<=``. Same
    comparison ``_meets_target`` applies, so the row cannot describe a different test from the
    one that actually stops the search."""
    crit = [TargetObjectiveStop(type='target_objective', value=-2.0)]
    snap = _snap(best=-1.42)
    assert _rows(_sc(stopping=crit, direction='minimize'), snap)['failure_rate'].op == '<='
    assert _rows(_sc(stopping=crit, direction='maximize'), snap)['failure_rate'].op == '>='


def test_a_metric_reports_the_op_the_user_wrote():
    """The case that motivated publishing this: ``<= 0.8`` at 0.1 has already FIRED, and a bare
    ``0.1 / 0.8`` pair reads as 12% of the way there."""
    sc = _sc(stopping=[MetricStop(type='metric', name='err', op='<=', value=0.8)])
    row = _rows(sc, _snap(best=0.5, metrics={'err': 0.1}))['err']
    assert row.op == '<=' and row.done is True


def test_the_op_reaches_the_wire():
    """``_budget_item`` is what the status, the CLI and the MCP all read."""
    from robovast.execution.controller import CampaignController
    from robovast.search.stopping import CriterionProgress
    item = CampaignController._budget_item(
        CriterionProgress('coverage', 0.1, 0.8, True, kind='metric', op='<='))
    assert item['op'] == '<=' and item['kind'] == 'metric' and item['done'] is True


def test_the_stale_row_measures_the_window_the_criterion_fires_on():
    """A gain under min_delta every round, and over it across the window.

    The criterion asks whether the WHOLE window improved, so a search gaining 0.05 a round
    against a min_delta of 0.1 has improved by 0.15 over three rounds and must not stop.
    Counting consecutive rounds that each failed to clear min_delta answers a different
    question and reaches 3/3, so the row said the criterion had fired while the search ran
    on -- and kept saying it for as long as the search kept improving.
    """
    sc = _sc(stopping=[NoImprovementStop(type='no_improvement', patience=3, min_delta=0.1)])
    rows = []
    for i, best in enumerate([0.0, 0.05, 0.10, 0.15], start=1):
        snap = _snap(batch=i, best=best)
        assert sc.should_stop(snap) is None, f"batch {i} must not stop while improving"
        rows.append(_rows(sc, snap)['stale_batches'])
    assert [r.current for r in rows] == [0, 1, 2, 2]
    assert not any(r.done for r in rows)


def test_the_stale_row_and_the_stop_agree_when_the_search_is_flat():
    """The other direction of the same rule: the row must reach its limit exactly when the
    criterion fires, so a reader is never told the search will run on after it has stopped."""
    sc = _sc(stopping=[NoImprovementStop(type='no_improvement', patience=2)])
    seen = []
    for i, best in enumerate([0.3, 0.5, 0.5, 0.5], start=1):
        snap = _snap(batch=i, best=best)
        fired = sc.should_stop(snap) is not None
        seen.append((fired, _rows(sc, snap)['stale_batches'].done))
    assert seen == [(False, False), (False, False), (False, False), (True, True)]
