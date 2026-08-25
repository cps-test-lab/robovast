# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Low-discrepancy sampling — unit tests.

`random` is the coverage baseline every other strategy is measured against, and it is a
weak one: uniform sampling clumps and leaves gaps, so a failure region can sit between
draws and an estimate carries more variance than the sample size suggests. A Halton
sequence fills the space evenly by construction, giving the same estimate from the same
number of runs with a visibly tighter interval — which is the whole claim, so it is what
these tests check.

Halton rather than Sobol because it needs no direction-number tables and therefore no
dependency beyond numpy: a *baseline* that only runs when an extra is installed is not a
baseline. Its weakness is high dimensions, which a search space of this size never reaches.
"""

import pytest

from robovast.common.config import SearchConfig
from robovast.search.strategy import build_strategy
from robovast.search.types import Evaluation


def _cfg(strategy='halton', dims=2, seed=1, params=None):
    space = {f'x{i}': {'type': 'float', 'low': 0.0, 'high': 1.0} for i in range(dims)}
    return SearchConfig(
        strategy=strategy, search_space=space, extract={'plugin': 'failure_rate'},
        objectives=[{'name': 'f', 'direction': 'maximize'}], per_batch=8,
        budget=[{'batches': 4}], seed=seed, strategy_parameters=params or {},
    )


def _points(strategy, batches=4, per_batch=8, dims=2):
    out = []
    for _ in range(batches):
        proposals = strategy.ask(per_batch)
        out.extend([tuple(ps.values[f'x{i}'] for i in range(dims)) for ps in proposals])
        strategy.tell([Evaluation(params=ps, objectives={'f': 0.0}, n_samples=1)
                       for ps in proposals])
    return out


def _max_gap(points, dims=2, bins=4):
    """How badly the worst cell of a bins^dims grid is under-filled.

    A crude discrepancy stand-in, chosen because it is the thing a reader cares about:
    the largest hole a search left in the space it claimed to cover.
    """
    from collections import Counter
    counts = Counter(
        tuple(min(int(v * bins), bins - 1) for v in p) for p in points)
    cells = bins ** dims
    return max(0, max(0 for _ in range(1)) if not counts else
               max((len(points) / cells) - counts.get(c, 0)
                   for c in _all_cells(bins, dims)))


def _all_cells(bins, dims):
    if dims == 1:
        return [(i,) for i in range(bins)]
    return [(i, *rest) for i in range(bins) for rest in _all_cells(bins, dims - 1)]


# -- registration and basic contract ----------------------------------------

def test_halton_is_registered_as_a_strategy():
    """It must be reachable by name from a .vast, like random/qd/optuna."""
    assert build_strategy(_cfg()) is not None


def test_it_proposes_exactly_what_was_asked_for():
    strategy = build_strategy(_cfg())
    assert len(strategy.ask(8)) == 8
    assert len(strategy.ask(3)) == 3


def test_every_proposal_lies_inside_the_declared_domain():
    strategy = build_strategy(_cfg())
    for ps in strategy.ask(16):
        assert all(0.0 <= v <= 1.0 for v in ps.values.values())


def test_it_continues_the_sequence_across_batches():
    """The evenness is a property of the whole series: restarting it each batch would
    re-draw the same points and cover less than random."""
    strategy = build_strategy(_cfg())
    first = {tuple(ps.values.values()) for ps in strategy.ask(8)}
    second = {tuple(ps.values.values()) for ps in strategy.ask(8)}
    assert first.isdisjoint(second)


def test_the_same_seed_reproduces_the_sequence():
    a = _points(build_strategy(_cfg(seed=7)))
    b = _points(build_strategy(_cfg(seed=7)))
    assert a == b


def test_a_different_seed_changes_a_scrambled_sequence():
    a = _points(build_strategy(_cfg(seed=7, params={'scramble': True})))
    b = _points(build_strategy(_cfg(seed=8, params={'scramble': True})))
    assert a != b


# -- the claim --------------------------------------------------------------

def test_it_covers_the_space_more_evenly_than_random():
    """The reason the strategy exists. Same budget, same space, fewer holes."""
    sob = _points(build_strategy(_cfg(strategy='halton')))
    rnd = _points(build_strategy(_cfg(strategy='random')))
    assert len(sob) == len(rnd)
    assert _max_gap(sob) < _max_gap(rnd)


def test_it_handles_every_dimension_type():
    """A coverage baseline that silently skipped int/choice/bool dims would be comparing
    a different space than the strategies it is the baseline for."""
    cfg = SearchConfig(
        strategy='halton',
        search_space={
            'f': {'type': 'float', 'low': -2.0, 'high': 2.0},
            'i': {'type': 'int', 'low': 1, 'high': 9},
            'c': {'type': 'choice', 'values': ['a', 'b', 'c']},
            'b': {'type': 'bool'},
        },
        extract={'plugin': 'failure_rate'},
        objectives=[{'name': 'f', 'direction': 'maximize'}], per_batch=8,
        budget=[{'batches': 1}], seed=3,
    )
    for ps in build_strategy(cfg).ask(16):
        assert -2.0 <= ps.values['f'] <= 2.0
        assert isinstance(ps.values['i'], int) and 1 <= ps.values['i'] <= 9
        assert ps.values['c'] in ('a', 'b', 'c')
        assert isinstance(ps.values['b'], bool)


def test_report_ranks_by_the_objective():
    strategy = build_strategy(_cfg())
    proposals = strategy.ask(4)
    strategy.tell([
        Evaluation(params=ps, objectives={'f': float(i)}, n_samples=1)
        for i, ps in enumerate(proposals)])
    report = strategy.report()
    assert report.best.objectives['f'] == 3.0
    assert len(report.evaluations) == 4


def test_a_short_generation_is_tolerated():
    """A draw that composes to nothing never comes back; the strategy must cope rather
    than assume tell() mirrors ask()."""
    strategy = build_strategy(_cfg())
    proposals = strategy.ask(8)
    strategy.tell([Evaluation(params=proposals[0], objectives={'f': 1.0}, n_samples=1)])
    assert len(strategy.report().evaluations) == 1


def test_unknown_strategy_parameter_is_refused():
    with pytest.raises(Exception):
        build_strategy(_cfg(params={'nonsense': 1}))
