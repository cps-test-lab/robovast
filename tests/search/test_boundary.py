# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Boundary seeking — unit tests.

"Maximize failures" has a trivial answer: crank the worst factor to its limit. The
engineering question is narrower and harder — *where does it start failing?* — and no
amount of budget spent on the interior of the failure region answers it.

A boundary search spends its evaluations on the level set instead: the contour where the
objective crosses a stated value. The level lives in ``strategy_parameters`` rather than
on the objective, because seeking a level is a property of **how you search**, not of what
you measure — and ``direction`` already means "which way is better", which a target does
not answer.
"""

import pytest

from robovast.common.config import SearchConfig
from robovast.search.strategy import build_strategy
from robovast.search.types import Evaluation


def _cfg(params=None, seed=1, per_batch=8):
    return SearchConfig(
        strategy='boundary',
        search_space={'x': {'type': 'float', 'low': 0.0, 'high': 1.0}},
        extract={'plugin': 'failure_rate'},
        objectives=[{'name': 'margin', 'direction': 'minimize'}],
        per_batch=per_batch, budget=[{'batches': 4}], seed=seed,
        strategy_parameters=params if params is not None else {'level': 0.5},
    )


def _teach(strategy, f, n=24):
    """Let the strategy see a landscape: objective = f(x)."""
    proposals = strategy.ask(n)
    strategy.tell([
        Evaluation(params=ps, objectives={'margin': f(float(ps.values['x']))}, n_samples=1)
        for ps in proposals])
    return proposals


# -- contract ---------------------------------------------------------------

def test_boundary_is_registered_as_a_strategy():
    assert build_strategy(_cfg()) is not None


def test_it_proposes_exactly_what_was_asked_for():
    strategy = build_strategy(_cfg())
    assert len(strategy.ask(8)) == 8


def test_every_proposal_lies_inside_the_declared_domain():
    strategy = build_strategy(_cfg())
    _teach(strategy, lambda x: x)
    for ps in strategy.ask(16):
        assert 0.0 <= ps.values['x'] <= 1.0


def test_the_same_seed_reproduces_the_search():
    a, b = build_strategy(_cfg(seed=5)), build_strategy(_cfg(seed=5))
    _teach(a, lambda x: x)
    _teach(b, lambda x: x)
    assert [p.values['x'] for p in a.ask(6)] == [p.values['x'] for p in b.ask(6)]


# -- cold start -------------------------------------------------------------

def test_with_no_history_it_covers_rather_than_guesses():
    """Nothing is known, so there is no level to seek yet. Spreading out is the honest
    move -- and it is the same low-discrepancy coverage the baseline uses, so the first
    batch is not wasted."""
    xs = sorted(float(ps.values['x']) for ps in build_strategy(_cfg()).ask(8))
    spread = max(b - a for a, b in zip(xs, xs[1:]))
    assert spread < 0.5, 'a cold start must not clump'


# -- the claim --------------------------------------------------------------

def test_it_concentrates_on_the_level_once_it_has_seen_the_landscape():
    """The whole point. objective = x, level = 0.5, so the boundary is at x = 0.5 and
    that is where the evaluations should go."""
    strategy = build_strategy(_cfg({'level': 0.5}))
    _teach(strategy, lambda x: x, n=24)
    proposals = [float(ps.values['x']) for ps in strategy.ask(8)]
    assert sum(abs(x - 0.5) < 0.2 for x in proposals) >= 6


def test_the_level_is_what_it_seeks_and_not_a_hardcoded_zero():
    strategy = build_strategy(_cfg({'level': 0.8}))
    _teach(strategy, lambda x: x, n=24)
    proposals = [float(ps.values['x']) for ps in strategy.ask(8)]
    assert sum(abs(x - 0.8) < 0.2 for x in proposals) >= 6


def test_it_does_not_pile_every_proposal_on_one_point():
    """A batch of identical points would evaluate one cell eight times and learn the
    boundary's location no better than one evaluation would."""
    strategy = build_strategy(_cfg({'level': 0.5}))
    _teach(strategy, lambda x: x, n=24)
    proposals = [round(float(ps.values['x']), 6) for ps in strategy.ask(8)]
    assert len(set(proposals)) >= 4


def test_a_boundary_outside_the_sampled_range_is_still_approached():
    """The level sits where nothing has been evaluated yet; the search must move toward
    it rather than sitting among the points it already has."""
    strategy = build_strategy(_cfg({'level': 0.9}))
    # only the lower half has been seen
    proposals = strategy.ask(12)
    strategy.tell([
        Evaluation(params=ps, objectives={'margin': float(ps.values['x']) * 0.5},
                   n_samples=1)
        for ps in proposals])
    nxt = [float(ps.values['x']) for ps in strategy.ask(8)]
    assert max(nxt) > 0.6


# -- housekeeping -----------------------------------------------------------

def test_a_short_generation_is_tolerated():
    strategy = build_strategy(_cfg())
    proposals = strategy.ask(8)
    strategy.tell([Evaluation(params=proposals[0], objectives={'margin': 0.4},
                              n_samples=1)])
    assert len(strategy.report().evaluations) == 1


def test_report_names_the_level_and_how_close_it_got():
    strategy = build_strategy(_cfg({'level': 0.5}))
    _teach(strategy, lambda x: x, n=16)
    extra = strategy.report().extra
    assert extra['level'] == 0.5
    assert 'closest_to_level' in extra


def test_unknown_strategy_parameter_is_refused():
    with pytest.raises(Exception):
        build_strategy(_cfg({'level': 0.5, 'nonsense': 1}))


def test_it_needs_a_single_objective():
    """A level set is a contour of one quantity; with two there is no single surface to
    trace, and silently using the first would answer a different question."""
    cfg = SearchConfig(
        strategy='boundary',
        search_space={'x': {'type': 'float', 'low': 0.0, 'high': 1.0}},
        extract={'plugin': 'failure_rate'},
        objectives=[{'name': 'a'}, {'name': 'b'}],
        per_batch=4, budget=[{'batches': 1}], seed=1,
        strategy_parameters={'level': 0.0},
    )
    with pytest.raises(ValueError, match='single-objective'):
        build_strategy(cfg).ask(4)
