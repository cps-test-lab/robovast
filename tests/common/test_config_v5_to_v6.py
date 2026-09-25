# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Config 5 -> 6: a job is one run, so ``runs_per_job`` goes and ``timeout`` is one run's budget."""

import pytest

from robovast.common.migrations.config.v5_to_v6 import migrate


def _v5(**execution):
    """A minimal v5 config carrying *execution* keys, for the v5 -> v6 step."""
    return {"version": 5, "metadata": {"name": "x"},
            "execution": {"runs": 1, "scenario_file": "s.osc", **execution}}


def test_v6_drops_runs_per_job():
    out = migrate(_v5(runs_per_job=1))

    assert "runs_per_job" not in out["execution"]
    assert out["version"] == 6


def test_v6_gives_each_run_its_share_of_a_packed_job_budget():
    """v5's ``timeout`` bounded the whole packed job; v6's bounds one run, which is one job.

    So 100 runs behind 600s had 6s each, and 7 runs behind 600s had 86 -- rounded up, since
    rounding down would kill a run the file gave its share.
    """
    assert migrate(_v5(timeout=600, runs_per_job=100))["execution"]["timeout"] == 6
    assert migrate(_v5(timeout=600, runs_per_job=7))["execution"]["timeout"] == 86


def test_v6_leaves_an_unpacked_timeout_exactly_as_written():
    assert migrate(_v5(timeout=300))["execution"]["timeout"] == 300
    assert migrate(_v5(timeout=300, runs_per_job=1))["execution"]["timeout"] == 300
    assert "timeout" not in migrate(_v5(runs_per_job=100))["execution"]


@pytest.mark.parametrize("execution", [
    {"timeout": "600", "runs_per_job": 100},
    {"timeout": True, "runs_per_job": 100},
    {"timeout": 600, "runs_per_job": "many"},
])
def test_v6_does_not_rewrite_a_malformed_timeout(execution):
    """A malformed value is the schema's to reject, where the message names the field."""
    out = migrate(_v5(**execution))

    assert out["execution"]["timeout"] == execution["timeout"]
    assert "runs_per_job" not in out["execution"]
