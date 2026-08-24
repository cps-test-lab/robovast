# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Aggregating a cell's repetitions into one score — unit tests.

An extractor turns N runs into one number, and *which* number is a real choice with
a measured consequence. Averaging is the obvious one and usually the wrong one: it
hides the run that nearly crashed behind the four that did not, and on a
quality-diversity archive it collapses the very spread the archive exists to map.
"""

import math

import pytest

from robovast.search.aggregate import aggregate


# -- worst case -------------------------------------------------------------

def test_worst_of_a_safety_margin_is_the_smallest():
    """Higher margin = safer, so the worst run is the one with the least room."""
    assert aggregate([0.8, 0.2, 0.5], how='worst', higher_is_safer=True) == 0.2


def test_worst_of_a_cost_is_the_largest():
    """Lower is safer for a cost (time, effort), so the worst run is the largest."""
    assert aggregate([3.0, 9.0, 4.0], how='worst', higher_is_safer=False) == 9.0


def test_worst_is_the_default_because_mean_hides_the_bad_run():
    assert aggregate([1.0, 1.0, 1.0, 1.0, -0.1]) == -0.1


# -- quantile ---------------------------------------------------------------

def test_quantile_is_less_brittle_than_the_single_worst_run():
    """A low quantile keeps the pessimism without letting one freak run define the
    cell -- the reason it is offered alongside `worst`."""
    values = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    assert aggregate(values, how='quantile', quantile=0.1) == pytest.approx(0.1)


def test_quantile_flips_with_direction():
    """For a cost, the pessimistic tail is the UPPER one."""
    values = [float(v) for v in range(11)]
    assert aggregate(values, how='quantile', quantile=0.1,
                     higher_is_safer=False) == pytest.approx(9.0)


@pytest.mark.parametrize('q', [-0.1, 1.5])
def test_quantile_out_of_range_is_rejected(q):
    with pytest.raises(ValueError, match='quantile'):
        aggregate([1.0, 2.0], how='quantile', quantile=q)


# -- mean -------------------------------------------------------------------

def test_mean_is_available_but_must_be_asked_for_by_name():
    assert aggregate([0.0, 1.0], how='mean') == 0.5


# -- refusals ---------------------------------------------------------------

def test_no_samples_is_refused_rather_than_scored():
    """The same rule the built-in extractor follows: inventing a number for a cell
    that produced nothing is worse than having none, because a maximizing search
    would then steer AWAY from the cells whose runs are dying."""
    with pytest.raises(ValueError, match='no values'):
        aggregate([], how='worst')


def test_unknown_method_is_rejected_by_name():
    with pytest.raises(ValueError, match='geometric'):
        aggregate([1.0], how='geometric')


def test_a_non_finite_sample_is_refused():
    """NaN silently wins or loses every comparison depending on the operator, so a
    cell containing one would score arbitrarily rather than loudly."""
    with pytest.raises(ValueError, match='finite'):
        aggregate([1.0, math.nan], how='worst')


# -- single sample ----------------------------------------------------------

def test_one_sample_aggregates_to_itself_under_every_method():
    for how in ('worst', 'mean', 'quantile'):
        assert aggregate([0.42], how=how) == pytest.approx(0.42)
