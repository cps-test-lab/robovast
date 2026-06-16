# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Validation of the ``search:`` config block and typed search space."""

import pytest
from pydantic import ValidationError

from robovast.common.config import (ChoiceDim, ConfigV1, FloatDim, IntDim,
                                    validate_config)

BASE = {"version": 1, "execution": {"image": "img", "runs": 2}}


def _with_search(**search):
    cfg = dict(BASE)
    cfg["search"] = {
        "strategy": "random",
        "extract": {"plugin": "failure_rate"},
        "objectives": [{"name": "failure_rate", "direction": "maximize"}],
        "per_step": 4,
        "search_space": {"x": {"type": "float", "low": 0, "high": 1}},
        **search,
    }
    return cfg


def test_search_absent_is_batch():
    assert ConfigV1(**BASE).search is None


def test_typed_search_space_discriminates():
    m = ConfigV1(**_with_search(search_space={
        "a": {"type": "float", "low": 1, "high": 5, "log": True},
        "b": {"type": "int", "low": 1, "high": 10, "step": 2},
        "c": {"type": "choice", "values": [0.0, 0.1]},
    }))
    ss = m.search.search_space
    assert isinstance(ss["a"], FloatDim) and ss["a"].log
    assert isinstance(ss["b"], IntDim) and ss["b"].step == 2
    assert isinstance(ss["c"], ChoiceDim)


def test_extract_and_objectives():
    m = ConfigV1(**_with_search())
    assert m.search.extract.plugin == "failure_rate"
    assert m.search.extract.params == {}
    assert [(o.name, o.direction) for o in m.search.objectives] == [("failure_rate", "maximize")]


def test_strategy_parameters_free_form():
    m = ConfigV1(**_with_search(
        strategy="qd",
        strategy_parameters={"archive": {"type": "cvt", "cells": 64,
                                         "measures": {"m": {"low": 0, "high": 1}}}}))
    # ConfigV1 keeps it as a free-form dict; the strategy validates it later.
    assert m.search.strategy_parameters["archive"]["cells"] == 64


@pytest.mark.parametrize("space", [
    {"x": {"type": "float", "low": 10, "high": 1}},
    {"x": {"type": "int", "low": 5, "high": 1}},
    {"x": {"type": "choice", "values": []}},
    {"x": {"type": "float", "low": 0, "high": 1, "log": True}},
    {"x": {"type": "bogus", "low": 0, "high": 1}},
])
def test_bad_search_space_rejected(space):
    with pytest.raises(ValidationError):
        ConfigV1(**_with_search(search_space=space))


def test_required_fields_and_bounds():
    with pytest.raises(ValidationError):
        ConfigV1(**_with_search(per_step=0))
    with pytest.raises(ValidationError):
        ConfigV1(**_with_search(budget={"generations": 0}))
    with pytest.raises(ValidationError):
        ConfigV1(**_with_search(search_space={}))
    with pytest.raises(ValidationError):
        ConfigV1(**_with_search(objectives=[]))


def test_validate_config_roundtrip():
    m = validate_config(_with_search())
    assert m.search.strategy == "random"


def test_search_without_configuration_validates():
    # A search config is self-contained — no configuration: block needed.
    m = ConfigV1(**_with_search())
    assert m.configuration is None


def test_search_and_configuration_are_mutually_exclusive():
    cfg = _with_search()
    cfg["configuration"] = [{"name": "base", "parameters": [{"x": 1.0}]}]
    with pytest.raises(ValidationError, match="mutually exclusive"):
        ConfigV1(**cfg)
