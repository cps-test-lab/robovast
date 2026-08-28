# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""A sampler whose backing package is missing is refused when the strategy is BUILT.

optuna's ``CmaEsSampler`` constructs fine without the ``cmaes`` distribution and imports it
lazily, inside ``sample_relative`` -- so the failure lands on the first ``ask``, which is
after the campaign has started, staged its configs and taken a lane. What surfaces there is
a bare ``No module named 'cmaes'`` from inside optuna's sampler, on a campaign that has
produced nothing.

Declaring the dependency (the ``optuna`` extra now carries ``cmaes``) fixes it going
forward. This check is what protects a deployment whose image predates that, and it turns a
spent campaign into a refusal naming the package and the sampler that needs it.
"""

import pytest

from robovast.common.config import SearchConfig
from robovast.search.strategy import build_strategy

pytest.importorskip("optuna")

SPACE = {"x": {"type": "float", "low": 0.0, "high": 1.0}}


def _config(sampler):
    return SearchConfig(
        strategy="optuna", strategy_parameters={"sampler": sampler}, search_space=SPACE,
        objectives=[{"name": "robustness", "direction": "minimize"}],
        per_batch=4, extract={"plugin": "failure_rate"}, budget=[{"batches": 2}])


def test_cmaes_without_its_package_is_refused_by_name(monkeypatch):
    """The message must name the missing distribution and the sampler that needs it --
    'No module named cmaes' from inside optuna names neither the campaign nor the choice
    that caused it."""
    import robovast.search.strategies.optuna as mod

    monkeypatch.setattr(mod, "_sampler_package_available", lambda name: False)
    with pytest.raises(ValueError) as excinfo:
        build_strategy(_config("cmaes"), "")
    message = str(excinfo.value)
    assert "cmaes" in message
    assert "sampler" in message.lower()


def test_the_refusal_happens_at_build_not_on_the_first_ask(monkeypatch):
    """The point of the check: before a campaign takes a lane, not after."""
    import robovast.search.strategies.optuna as mod

    monkeypatch.setattr(mod, "_sampler_package_available", lambda name: False)
    with pytest.raises(ValueError):
        build_strategy(_config("cmaes"), "")


def test_cmaes_builds_when_the_package_is_present(monkeypatch):
    import robovast.search.strategies.optuna as mod

    monkeypatch.setattr(mod, "_sampler_package_available", lambda name: True)
    assert build_strategy(_config("cmaes"), "") is not None


@pytest.mark.parametrize("sampler", ["tpe", "random"])
def test_samplers_that_need_nothing_extra_are_unaffected(sampler, monkeypatch):
    """Only the sampler that has an out-of-tree dependency may be gated on one; gating the
    others would refuse a working campaign."""
    import robovast.search.strategies.optuna as mod

    monkeypatch.setattr(mod, "_sampler_package_available", lambda name: False)
    assert build_strategy(_config(sampler), "") is not None


def test_the_optuna_extra_declares_cmaes():
    """The real fix, and the one a fresh install needs: the extra that provides the
    strategy provides everything its declared samplers can use. Without this the check
    above just refuses politely forever."""
    import pathlib
    import tomllib

    root = pathlib.Path(__file__).resolve().parents[2]
    data = tomllib.loads((root / "pyproject.toml").read_text())
    extras = data["tool"]["poetry"]["extras"]
    assert "cmaes" in extras["optuna"], (
        "the 'optuna' extra offers a cmaes sampler but does not install the package it "
        "needs")
