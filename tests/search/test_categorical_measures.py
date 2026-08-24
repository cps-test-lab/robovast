# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Categorical quality-diversity measures — unit tests.

A QD archive answers "how many *distinct kinds* of behaviour are there", and the most
useful kind is often not a number. "It collided" / "it timed out" / "it never reached the
goal" is the answer an engineer wants, and a measure axis of `low`/`high`/`bins` could
only express it by having the extractor invent an encoding and the reader decode it from
a comment.

So a measure may declare its categories instead, and an extractor may hand back the name.
"""

import pytest

from robovast.common.config import SearchConfig

pytest.importorskip('ribs', reason="quality-diversity needs the 'qd' extra")

from robovast.search.strategies.qd import MeasureSpec, measure_value  # noqa: E402

MODES = ['collision', 'timeout', 'goal_miss', 'stuck']


def _cfg(measures, archive_type='grid'):
    return SearchConfig(
        strategy='qd',
        search_space={'x': {'type': 'float', 'low': 0.0, 'high': 1.0}},
        extract={'plugin': 'failure_rate'},
        objectives=[{'name': 'failure_rate', 'direction': 'maximize'}],
        per_batch=4, budget=[{'batches': 1}], seed=1,
        strategy_parameters={'archive': {'type': archive_type, 'measures': measures}},
    )


# -- the axis ---------------------------------------------------------------

def test_a_categorical_measure_derives_its_own_axis():
    """The author states the categories; the bounds and bin count follow. Stating them
    separately would be two sources of truth for one fact."""
    spec = MeasureSpec(values=MODES)
    assert (spec.low, spec.high, spec.bins) == (0.0, 4.0, 4)


def test_each_category_lands_in_its_own_bin():
    """The point of deriving the axis: k categories, k bins, no two sharing one."""
    spec = MeasureSpec(values=MODES)
    bins = {int(measure_value(spec, m, 'failure_mode')) for m in MODES}
    assert bins == {0, 1, 2, 3}


def test_a_category_lands_inside_its_bin_not_on_the_boundary():
    """An integer index sits exactly on a bin edge, where which side it falls is the
    binning library's business rather than ours."""
    spec = MeasureSpec(values=MODES)
    assert measure_value(spec, 'collision', 'failure_mode') == 0.5


def test_an_unknown_category_is_refused_by_name():
    """Silently binning it -- or dropping the evaluation -- would put a behaviour the
    archive cannot represent into a cell that means something else."""
    spec = MeasureSpec(values=MODES)
    with pytest.raises(ValueError, match='landed_upside_down'):
        measure_value(spec, 'landed_upside_down', 'failure_mode')


def test_the_refusal_lists_what_was_declared():
    spec = MeasureSpec(values=MODES)
    with pytest.raises(ValueError) as exc:
        measure_value(spec, 'nope', 'failure_mode')
    assert 'collision' in str(exc.value) and 'failure_mode' in str(exc.value)


# -- numeric measures are unchanged -----------------------------------------

def test_a_numeric_measure_passes_through():
    spec = MeasureSpec(low=0.0, high=1.5)
    assert measure_value(spec, 0.75, 'clearance') == 0.75


def test_a_numeric_measure_still_accepts_a_numeric_string():
    """Extractors read CSVs; a value arriving as text is ordinary and not an error."""
    spec = MeasureSpec(low=0.0, high=1.5)
    assert measure_value(spec, '0.75', 'clearance') == 0.75


def test_a_non_numeric_value_on_a_numeric_measure_is_refused():
    spec = MeasureSpec(low=0.0, high=1.5)
    with pytest.raises(ValueError, match='clearance'):
        measure_value(spec, 'collision', 'clearance')


# -- schema -----------------------------------------------------------------

def test_categories_and_bounds_are_mutually_exclusive():
    """Two ways of saying where the axis runs, which could disagree."""
    with pytest.raises(ValueError, match='values'):
        MeasureSpec(values=MODES, low=0.0, high=10.0)


def test_a_numeric_measure_still_needs_its_bounds():
    with pytest.raises(ValueError):
        MeasureSpec(bins=5)


@pytest.mark.parametrize('bad', [[], ['a', 'a']])
def test_empty_or_duplicate_categories_are_refused(bad):
    with pytest.raises(ValueError):
        MeasureSpec(values=bad)


def test_a_campaign_may_mix_categorical_and_numeric_measures():
    """The realistic archive: which kind of failure, against how close it came."""
    cfg = _cfg({'failure_mode': {'values': MODES},
                'min_clearance': {'low': 0.0, 'high': 1.5}})
    archive = cfg.strategy_parameters['archive']
    assert set(archive['measures']) == {'failure_mode', 'min_clearance'}


def test_a_categorical_measure_builds_a_working_archive():
    """End to end: the strategy accepts it and tells the archive without complaint."""
    from robovast.search.strategy import build_strategy
    from robovast.search.types import Evaluation

    strategy = build_strategy(_cfg({'failure_mode': {'values': MODES},
                                    'min_clearance': {'low': 0.0, 'high': 1.5}}))
    proposals = strategy.ask(4)
    strategy.tell([
        Evaluation(params=ps,
                   objectives={'failure_rate': 1.0},
                   measures={'failure_mode': MODES[i % len(MODES)],
                             'min_clearance': 0.2 * i},
                   n_samples=1)
        for i, ps in enumerate(proposals)])
    report = strategy.report()
    assert report.extra['num_elites'] >= 1
