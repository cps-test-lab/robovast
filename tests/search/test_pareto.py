# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Non-dominated (Pareto) selection — unit tests.

With one objective the deliverable is "the best". With two it is the set of points
that no other point beats on every objective at once: fast-and-close against
slow-and-safe, with the trade-off between them the actual result.
"""

import pytest

from robovast.common.config import ObjectiveSpec
from robovast.search.pareto import dominates, pareto_front
from robovast.search.types import Evaluation, ParamSet

# clearance: more is better. time: less is better. The canonical pairing.
SPECS = [ObjectiveSpec(name='clearance', direction='maximize'),
         ObjectiveSpec(name='time', direction='minimize')]


def _ev(clearance, time, tag=None):
    return Evaluation(params=ParamSet(values={'t': tag if tag is not None else clearance}),
                      objectives={'clearance': clearance, 'time': time})


def _front_tags(evs):
    return sorted(ev.params.values['t'] for ev in pareto_front(evs, SPECS))


# -- domination -------------------------------------------------------------

def test_better_on_both_dominates():
    assert dominates(_ev(1.0, 5.0), _ev(0.5, 9.0), SPECS)


def test_better_on_one_and_equal_on_the_other_dominates():
    assert dominates(_ev(1.0, 5.0), _ev(1.0, 9.0), SPECS)


def test_a_trade_off_dominates_nothing():
    """Safer but slower beats nothing; that is what makes it a trade-off."""
    assert not dominates(_ev(1.0, 9.0), _ev(0.5, 5.0), SPECS)
    assert not dominates(_ev(0.5, 5.0), _ev(1.0, 9.0), SPECS)


def test_identical_points_do_not_dominate_each_other():
    assert not dominates(_ev(1.0, 5.0), _ev(1.0, 5.0), SPECS)


def test_direction_is_honoured_per_objective():
    """Both objectives minimized: now the LOW clearance wins, which is only correct
    because the spec says so -- getting this backwards silently inverts the result."""
    specs = [ObjectiveSpec(name='clearance', direction='minimize'),
             ObjectiveSpec(name='time', direction='minimize')]
    assert dominates(_ev(0.5, 5.0), _ev(1.0, 9.0), specs)


# -- the front --------------------------------------------------------------

def test_front_keeps_the_trade_offs_and_drops_the_dominated():
    evs = [
        _ev(1.0, 9.0, 'safe_slow'),      # on the front
        _ev(0.5, 5.0, 'risky_fast'),     # on the front
        _ev(0.4, 9.5, 'dominated'),      # worse than safe_slow on both
    ]
    assert _front_tags(evs) == ['risky_fast', 'safe_slow']


def test_a_single_point_is_its_own_front():
    assert len(pareto_front([_ev(1.0, 5.0)], SPECS)) == 1


def test_empty_input_gives_an_empty_front():
    assert pareto_front([], SPECS) == []


def test_duplicates_are_all_kept():
    """Two cells that measured the same are both real results; silently dropping one
    would misreport how much of the space reaches that trade-off."""
    evs = [_ev(1.0, 5.0, 'a'), _ev(1.0, 5.0, 'b')]
    assert _front_tags(evs) == ['a', 'b']


def test_an_evaluation_missing_an_objective_is_skipped_not_guessed():
    """A cell that did not report every objective cannot be compared on all of them;
    substituting a value would fabricate the comparison."""
    partial = Evaluation(params=ParamSet(values={'t': 'partial'}),
                         objectives={'clearance': 5.0})
    front = pareto_front([partial, _ev(1.0, 5.0, 'full')], SPECS)
    assert [ev.params.values['t'] for ev in front] == ['full']


def test_single_objective_front_is_just_the_best():
    specs = [ObjectiveSpec(name='clearance', direction='maximize')]
    evs = [_ev(1.0, 0.0, 'best'), _ev(0.5, 0.0, 'worse')]
    front = pareto_front(evs, specs)
    assert [ev.params.values['t'] for ev in front] == ['best']


def test_front_is_computed_in_quadratic_comfort_not_sampled():
    """A large front must come back whole -- truncating it would silently answer a
    different question than 'what are the available trade-offs'.

    Clearance and time rise together, so every point buys safety with time and none
    dominates another: the front is all 101. (Were time to *fall* as clearance rose,
    the last point would beat every other on both and the front would be one.)"""
    evs = [_ev(i / 100.0, i / 100.0, f'p{i}') for i in range(101)]
    assert len(pareto_front(evs, SPECS)) == 101


@pytest.mark.parametrize('bad', [float('nan')])
def test_non_finite_objective_is_refused(bad):
    with pytest.raises(ValueError, match='finite'):
        pareto_front([_ev(bad, 1.0)], SPECS)
