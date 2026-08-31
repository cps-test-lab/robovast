# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""A draw that produced no evaluation closes its optuna trial instead of leaking it.

``ask`` opens one optuna trial per proposed parameter set. A draw the variation pipeline
could not realize, or one whose every run was lost, never comes back -- ``tell`` is handed
a shorter list than ``ask`` proposed, which the search interface explicitly allows. The
trial behind that draw was left open, and an open trial is not inert: it stays RUNNING for
the rest of the campaign, and TPE's ``constant_liar`` (this strategy's default) feeds
RUNNING trials to its ABOVE estimator. So every unrealizable draw permanently marked its
own region as bad and the sampler steered away from it for good -- more strongly with each
batch, on exactly the campaigns where draws fail often enough to matter.

Closed as FAIL rather than told a value: optuna excludes FAIL trials from sampling
entirely, so the draw costs its trial and nothing else, and no objective is invented for a
cell that produced none.
"""

import pytest

from robovast.common.config import SearchConfig
from robovast.search.strategy import build_strategy
from robovast.search.types import Evaluation

pytest.importorskip("optuna")

from optuna.trial import TrialState  # noqa: E402


def _cfg(space=None, objectives=None, sampler="tpe"):
    return SearchConfig(
        strategy="optuna", strategy_parameters={"sampler": sampler},
        search_space=space or {"x": {"type": "float", "low": 0.0, "high": 1.0}},
        objectives=objectives or [{"name": "f", "direction": "maximize"}],
        per_batch=4, extract={"plugin": "failure_rate"}, budget=[{"batches": 3}], seed=1)


def _score(param_sets, objectives=("f",)):
    return [Evaluation(params=ps, objectives={name: 0.5 for name in objectives},
                       n_samples=1)
            for ps in param_sets]


def test_a_draw_that_never_came_back_leaves_no_running_trial():
    strategy = build_strategy(_cfg(), "")
    for _ in range(3):
        param_sets = strategy.ask(4)
        strategy.tell(_score(param_sets[:2]))       # two of four produced nothing

    study = strategy._study                          # noqa: SLF001 - the state under test
    assert study.get_trials(states=[TrialState.RUNNING]) == []
    assert len(study.get_trials(states=[TrialState.COMPLETE])) == 6
    assert len(study.get_trials(states=[TrialState.FAIL])) == 6


def test_an_abandoned_trial_is_not_scored():
    """FAIL, not a value: a cell that produced nothing must not enter the sampler's
    picture of the landscape at either end of it."""
    strategy = build_strategy(_cfg(), "")
    param_sets = strategy.ask(4)
    strategy.tell(_score(param_sets[:1]))

    study = strategy._study                          # noqa: SLF001
    failed = study.get_trials(states=[TrialState.FAIL])
    assert len(failed) == 3
    assert all(t.values is None for t in failed)
    # And the search's own record counts only what was measured.
    assert strategy.report().extra["n_trials"] == 1


def test_a_full_generation_leaves_nothing_failed():
    strategy = build_strategy(_cfg(), "")
    for _ in range(3):
        strategy.tell(_score(strategy.ask(4)))

    study = strategy._study                          # noqa: SLF001
    assert study.get_trials(states=[TrialState.FAIL]) == []
    assert len(study.get_trials(states=[TrialState.COMPLETE])) == 12


def test_two_draws_with_the_same_values_keep_two_trials():
    """A discrete space repeats: ``ParamSet.id`` is derived from the values, so one batch
    can carry the same id twice. Both trials must be closed -- keyed by id, one of them
    was overwritten before it could be, and leaked."""
    strategy = build_strategy(
        _cfg(space={"c": {"type": "choice", "values": ["a"]}}), "")
    param_sets = strategy.ask(4)
    assert len({ps.id for ps in param_sets}) == 1     # every draw is the same cell

    strategy.tell(_score(param_sets[:1]))

    study = strategy._study                           # noqa: SLF001
    assert study.get_trials(states=[TrialState.RUNNING]) == []
    assert len(study.get_trials(states=[TrialState.COMPLETE])) == 1
    assert len(study.get_trials(states=[TrialState.FAIL])) == 3


def test_a_short_multi_objective_generation_closes_its_trials_too():
    objectives = [{"name": "f", "direction": "maximize"},
                  {"name": "g", "direction": "minimize"}]
    strategy = build_strategy(_cfg(objectives=objectives, sampler="nsga2"), "")
    param_sets = strategy.ask(4)
    strategy.tell(_score(param_sets[:2], objectives=("f", "g")))

    study = strategy._study                           # noqa: SLF001
    assert study.get_trials(states=[TrialState.RUNNING]) == []
    assert len(study.get_trials(states=[TrialState.COMPLETE])) == 2
    assert len(study.get_trials(states=[TrialState.FAIL])) == 2
