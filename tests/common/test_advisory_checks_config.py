# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``execution.advisory_checks``: the health checks a campaign expects to fire."""

import pytest

from robovast.common.config import validate_config


def _cfg(**execution):
    return {"version": 4,
            "execution": {"containers": {"scenario": {"image": "base:1"}}, "runs": 1,
                          **execution}}


def test_slugs_are_taken_as_written():
    c = validate_config(_cfg(advisory_checks=["sim-time-rate", "robot-motion"]))
    assert c.execution.advisory_checks == ["sim-time-rate", "robot-motion"]


def test_undeclared_means_none():
    assert validate_config(_cfg()).execution.advisory_checks is None


def test_a_blank_slug_is_refused():
    """It matches no check, so a list carrying one reads as configured while doing nothing."""
    with pytest.raises(ValueError, match="advisory_checks"):
        validate_config(_cfg(advisory_checks=["sim-time-rate", "  "]))
