# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""A choice or a boolean is near only its own value.

The codec puts a choice's values in buckets of the unit interval in the order they were
written. Measured as a coordinate, that order would read as nearness: the first value would
be nearer the second than the last, and a boundary search's surrogate would let one value's
outcome stand in for its neighbour in the list. The distance measures such a dimension by
mismatch instead.
"""

import numpy as np

from robovast.common.config import SearchConfig
from robovast.search.space import SearchSpaceCodec
from robovast.search.strategies.boundary import mismatch_distances
from robovast.search.strategy import build_strategy
from robovast.search.types import Evaluation


def _space():
    cfg = SearchConfig(
        strategy='boundary',
        search_space={
            'form': {'type': 'choice', 'values': ['a', 'b', 'c']},
            'flag': {'type': 'bool'},
            'x': {'type': 'float', 'low': 0.0, 'high': 10.0},
        },
        extract={'plugin': 'failure_rate'},
        objectives=[{'name': 'margin', 'direction': 'minimize'}],
        per_batch=4, budget=[{'batches': 2}], seed=1,
        strategy_parameters={'level': 0.0},
    )
    return cfg


def _vec(codec, **values):
    return codec.encode(values)


def test_every_other_value_of_a_choice_is_equally_far():
    codec = SearchSpaceCodec(_space().search_space)
    a = _vec(codec, form='a', flag=False, x=5.0)
    b = _vec(codec, form='b', flag=False, x=5.0)
    c = _vec(codec, form='c', flag=False, x=5.0)
    d = mismatch_distances(codec.dims, np.array([b, c]), a)
    assert d[0] == d[1] == 1.0


def test_the_same_value_is_at_distance_zero_wherever_in_its_bucket():
    codec = SearchSpaceCodec(_space().search_space)
    a = _vec(codec, form='a', flag=True, x=5.0)
    inside = a.copy()
    inside[0] = 0.01       # still the first of three buckets
    inside[1] = 0.99       # still True
    assert mismatch_distances(codec.dims, np.array([inside]), a)[0] == 0.0


def test_a_boolean_differs_by_one():
    codec = SearchSpaceCodec(_space().search_space)
    t = _vec(codec, form='a', flag=True, x=5.0)
    f = _vec(codec, form='a', flag=False, x=5.0)
    assert mismatch_distances(codec.dims, np.array([f]), t)[0] == 1.0


def test_a_continuous_coordinate_keeps_its_unit_cube_difference():
    codec = SearchSpaceCodec(_space().search_space)
    p = _vec(codec, form='b', flag=False, x=2.0)
    q = _vec(codec, form='b', flag=False, x=7.0)
    assert np.isclose(mismatch_distances(codec.dims, np.array([q]), p)[0], 0.5)


def test_the_surrogate_does_not_borrow_a_neighbouring_choice_s_outcome():
    """With 'a' and 'b' measured on either side of the level and 'c' unmeasured, a 'c'
    candidate is at mismatch distance from both: its estimate is their mean, not mostly the
    value of 'b', which sits next to it in the list."""
    strategy = build_strategy(_space())
    codec = strategy._codec
    measured = [('a', -5.0), ('b', 5.0)]
    strategy.tell([
        Evaluation(params=type(strategy.ask(1)[0])(values={'form': f, 'flag': False, 'x': 5.0}),
                   objectives={'margin': v}, n_samples=1)
        for f, v in measured])
    known = np.array(strategy._points)
    candidate = _vec(codec, form='c', flag=False, x=5.0)
    predicted, nearest = strategy._predict(candidate, known)
    assert nearest == 1.0
    assert np.isclose(predicted, 0.0)
