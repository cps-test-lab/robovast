# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""A boundary batch proposes distinct points.

The strategy scores candidates on nearness to the level plus a bonus for being far from
what is already sampled, and picks one at a time so that a batch spreads along the contour
instead of piling onto one point. It said so and did not do it.

Measured on a real campaign: batch 2 asked for 8 proposals and got the SAME point 8 times.
Identical parameter values hash to one ``ParamSet`` id, so composition produced five configs
under one id and the campaign died on "Each search param set must map to exactly one config"
-- an error that points at the variations, while the cause was here.

The reason the penalty could not work: nearness is in [0, 1] and the spread bonus is
``exploration * distance`` with exploration 0.35, so a candidate sitting exactly on the level
outscores every rival even with its own bonus driven to zero. Keeping a batch apart needs
EXCLUSION; a preference is not enough.
"""

import yaml
import pathlib
import random

import pytest

from robovast.common.config import SearchConfig
from robovast.search.strategy import build_strategy
from robovast.search.types import Evaluation

EXAMPLE = (pathlib.Path(__file__).resolve().parents[2]
           / "configs" / "examples" / "nav_search" / "nav_search_boundary.vast")


def _strategy(**overrides):
    space = {"gap_width": {"type": "float", "low": 0.4, "high": 1.6},
             "walker_dwell": {"type": "float", "low": 0.0, "high": 8.0}}
    params = {"level": 0.0, "neighbours": 5, "exploration": 0.35}
    params.update(overrides)
    return build_strategy(SearchConfig(
        strategy="boundary", strategy_parameters=params, search_space=space,
        objectives=[{"name": "robustness", "direction": "minimize"}],
        per_batch=8, extract={"plugin": "failure_rate"},
        budget=[{"batches": 6}]), "")


def _round(param_sets):
    return [tuple(round(v, 10) for v in ps.values.values()) for ps in param_sets]


def _tell(strategy, param_sets, rnd):
    strategy.tell([Evaluation(params=ps, objectives={"robustness": rnd.uniform(-0.6, 0.07)},
                              measures={}, n_samples=3) for ps in param_sets])


def test_a_warm_batch_proposes_distinct_points():
    """The batch after the cold start is where this broke: with two points known the
    strategy switches from Halton to its own scoring."""
    strategy = _strategy()
    rnd = random.Random(0)
    _tell(strategy, strategy.ask(8), rnd)

    got = strategy.ask(8)
    values = _round(got)
    assert len(got) == 8
    assert len(set(values)) == 8, (
        f"only {len(set(values))} distinct point(s) in a batch of 8 -- identical param sets "
        f"share one id, and composition then maps one id to several configs")


def test_it_stays_distinct_over_several_batches():
    """Once the model has more points the best candidate gets sharper, which is exactly
    when a preference-only rule collapses hardest."""
    strategy = _strategy()
    rnd = random.Random(1)
    _tell(strategy, strategy.ask(8), rnd)
    for batch in range(2, 6):
        got = strategy.ask(8)
        assert len(set(_round(got))) == 8, f"batch {batch} collapsed onto fewer points"
        _tell(strategy, got, rnd)


def test_zero_exploration_still_yields_distinct_points():
    """Distinctness must not depend on the spread bonus being generous. With exploration 0
    a penalty-based rule degenerates completely; exclusion still holds."""
    strategy = _strategy(exploration=0.0)
    rnd = random.Random(2)
    _tell(strategy, strategy.ask(8), rnd)
    assert len(set(_round(strategy.ask(8)))) == 8


def test_the_cold_start_was_already_distinct():
    """Guards the half that worked: the first batch is a Halton sequence, and the campaign
    that failed got through batch 0 fine."""
    assert len(set(_round(_strategy().ask(8)))) == 8


def test_the_example_config_is_covered_by_these_parameters():
    """So this test keeps testing what the shipped campaign actually runs."""
    if not EXAMPLE.is_file():
        pytest.skip("nav_search example not present")
    declared = yaml.safe_load(EXAMPLE.read_text())["search"]["strategy_parameters"]
    assert declared["level"] == 0.0
    assert declared["exploration"] == 0.35
