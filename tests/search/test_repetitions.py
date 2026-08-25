# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Repetition allocation policy — unit tests.

The measured problem this exists for: in a quadrotor search campaign, 3 of 32
configurations produced a mixed outcome across 5 repetitions. The other 29 spent
5 runs each to learn a single bit — 145 of 160 runs. A policy that spends
repetitions where the outcome is actually uncertain is the fix.
"""

import pytest

from robovast.common.config import RepetitionsConfig, SearchConfig
from robovast.search.repetitions import build_repetition_policy
from robovast.search.types import Evaluation, ParamSet

_RAW_SPACE = {'x': {'type': 'float', 'low': 0.0, 'high': 1.0}}


def _validated_space():
    """The controller hands the policy validated dims, not raw dicts — build the same
    thing here so the test exercises the type the real caller passes."""
    return SearchConfig(strategy='random', search_space=_RAW_SPACE,
                        extract={'plugin': 'failure_rate'},
                        objectives=[{'name': 'f'}], per_batch=2,
                        budget=[{'batches': 1}]).search_space


SPACE = _validated_space()


def _ps(x):
    return ParamSet(values={'x': x})


def _ev(x, objective, n_samples=1):
    return Evaluation(params=_ps(x), objectives={'f': objective}, n_samples=n_samples)


def _policy(**kw):
    kw.setdefault('policy', 'adaptive')
    return build_repetition_policy(RepetitionsConfig(**kw), SPACE, default_runs=3)


# -- the seam ---------------------------------------------------------------

def test_absent_config_means_no_policy():
    """No `repetitions:` block must behave exactly as today: n_reps untouched."""
    assert build_repetition_policy(None, SPACE, default_runs=3) is None


def test_fixed_policy_assigns_the_campaign_default():
    pol = build_repetition_policy(RepetitionsConfig(policy='fixed'), SPACE, default_runs=3)
    out = pol.assign([_ps(0.1), _ps(0.9)], history=[])
    assert [p.n_reps for p in out] == [3, 3]


def test_assignment_preserves_parameter_set_identity():
    """id is content-derived and deliberately independent of n_reps -- a re-allocated
    set must stay the same cell, or its results land in a different directory."""
    ps = _ps(0.4)
    (out,) = _policy(min=1, max=8).assign([ps], history=[])
    assert out.id == ps.id and out.values == ps.values


def test_a_strategy_that_set_n_reps_itself_is_not_overridden():
    """ParamSet.n_reps is documented as the strategy's own channel; the policy only
    supplies a default for sets that did not ask for anything."""
    out = _policy(min=1, max=8).assign([ParamSet(values={'x': 0.5}, n_reps=7)], history=[])
    assert out[0].n_reps == 7


# -- adaptive allocation ----------------------------------------------------

def test_adaptive_starts_at_min_with_no_history():
    """Nothing is known yet, so nothing justifies spending more than the floor."""
    out = _policy(min=1, max=10).assign([_ps(0.2), _ps(0.8)], history=[])
    assert [p.n_reps for p in out] == [1, 1]


def test_adaptive_spends_more_where_neighbours_disagree():
    """The whole point: a point whose neighbourhood is settled gets the floor; one
    sitting where nearby evaluations disagree gets more."""
    history = [
        # a settled region around x=0.1 -- every neighbour agrees
        _ev(0.05, 0.0), _ev(0.10, 0.0), _ev(0.15, 0.0),
        # a contested region around x=0.9 -- neighbours disagree sharply
        _ev(0.85, 0.0), _ev(0.90, 1.0), _ev(0.95, 0.0),
    ]
    settled, contested = _policy(min=1, max=9, neighbours=3).assign(
        [_ps(0.10), _ps(0.90)], history=history)
    assert settled.n_reps == 1
    assert contested.n_reps > settled.n_reps


def test_adaptive_respects_min_and_max():
    history = [_ev(0.0, 0.0), _ev(0.5, 1.0), _ev(1.0, 0.0)]
    out = _policy(min=2, max=4, neighbours=3).assign(
        [_ps(v / 10) for v in range(11)], history=history)
    assert all(2 <= p.n_reps <= 4 for p in out)


def test_uniform_history_spends_the_floor_everywhere():
    """If every observation agrees, nothing is uncertain and nothing earns extra runs
    -- this is the 29-of-32 case that motivated the feature."""
    history = [_ev(v / 10, 0.0) for v in range(11)]
    out = _policy(min=1, max=10, neighbours=3).assign(
        [_ps(0.25), _ps(0.75)], history=history)
    assert [p.n_reps for p in out] == [1, 1]


# -- schema -----------------------------------------------------------------

def test_max_must_not_be_below_min():
    with pytest.raises(ValueError):
        RepetitionsConfig(policy='adaptive', min=5, max=2)


@pytest.mark.parametrize('field,value', [('min', 0), ('max', 0), ('neighbours', 0)])
def test_positive_fields(field, value):
    with pytest.raises(ValueError):
        RepetitionsConfig(policy='adaptive', **{field: value})


def test_unknown_policy_is_rejected_by_name():
    with pytest.raises(ValueError):
        RepetitionsConfig(policy='sqrt-n')


def test_search_config_accepts_a_repetitions_block():
    cfg = SearchConfig(strategy='random', search_space=_RAW_SPACE,
                       extract={'plugin': 'failure_rate'},
                       objectives=[{'name': 'failure_rate'}], per_batch=4,
                       budget=[{'batches': 2}],
                       repetitions={'policy': 'adaptive', 'min': 1, 'max': 8})
    assert cfg.repetitions.policy == 'adaptive' and cfg.repetitions.max == 8


def test_repetitions_defaults_to_absent():
    cfg = SearchConfig(strategy='random', search_space=_RAW_SPACE,
                       extract={'plugin': 'failure_rate'},
                       objectives=[{'name': 'failure_rate'}], per_batch=4,
                       budget=[{'batches': 2}])
    assert cfg.repetitions is None


# -- seed delivery is not plumbed yet, and says so ---------------------------

def test_seed_parameter_is_refused_until_per_run_delivery_exists():
    """Accepting it silently would be the worst outcome available.

    A simulator override document is written per CONFIGURATION, so every repetition of a
    cell reads the same one: delivering a seed down that channel would give all N runs of
    a cell identical noise, turning repetitions into duplicates and collapsing
    failure_rate back into a bit -- strictly worse than the current behaviour, where an
    unseeded run draws its own.

    The simulator's own episode counter cannot stand in for it either: jobs are packed by
    simulator settings, not by configuration (execution/packer.py, FixedK groups on
    WorkItem.sim_key), so one process's episodes run across several cells and "episode i"
    is not "repetition i". Refuse until a per-run seed exists.
    """
    with pytest.raises(ValueError, match='per-run'):
        RepetitionsConfig(policy='adaptive', seed_parameter={'sim': 'seed'})


def test_paired_requires_seeds_and_so_is_refused_for_the_same_reason():
    """`paired` means "reuse one seed list across cells". With no way to deliver a
    per-run seed there is no list to reuse, so claiming pairing would be a lie."""
    with pytest.raises(ValueError, match='per-run'):
        RepetitionsConfig(policy='adaptive', paired=True)


def test_repetitions_without_seeding_is_still_useful():
    """The allocation half stands on its own: unseeded repetitions still vary, they
    just cannot be paired or replayed."""
    cfg = RepetitionsConfig(policy='adaptive', min=1, max=8)
    assert cfg.paired is False and cfg.seed_parameter is None
