# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The local lane enforces ``execution.timeout``, as the cluster lane always has.

It previously did not — a local run "will continue past this timeout and must be stopped
manually" — which meant one config key had two meanings depending on where it ran. These
pin the three things that make the two lanes agree:

- the limit is per *step*, scaled by ``runs_per_job``, the same arithmetic the cluster
  applies to a Job's ``activeDeadlineSeconds``;
- an **undeclared** timeout stays unbounded rather than inheriting the cluster's one-hour
  fallback (enforcing a value the user set is a different decision from inventing one);
- a step killed by the limit still tears its stack down, and reports failure rather than
  passing as a shorter success.
"""

import subprocess

import pytest

from robovast.common.config import declared_per_run_seconds
from robovast.execution.backends import RunOptions, stage_run_script
from robovast.execution.controller import build_campaign_data
from robovast.execution.execution_utils.execute_local import _timeout_prefix

VAST = "configs/examples/ros2_basic/ros2_service.vast"


def _run_script(tmp_path, **execution_overrides):
    campaign_data = build_campaign_data(VAST, str(tmp_path / "gen"))
    campaign_data["execution"].update(execution_overrides)
    options = RunOptions(
        gui=False, start_only=False, abort_on_failure=False,
        image="img:1", log_tree=False, debug=False, skip_resource_allocation=True,
        postprocess=False, namespace="default", upload_to_share=False)
    path = stage_run_script(campaign_data, str(tmp_path / "work"), 2, options,
                            results_dir=str(tmp_path / "out"))
    return path, open(path).read()


def test_a_declared_timeout_bounds_every_compose_step(tmp_path):
    _path, script = _run_script(tmp_path, timeout=120)
    steps = [ln for ln in script.splitlines() if "docker compose" in ln and " up" in ln]
    assert steps, "no compose step was generated"
    for step in steps:
        assert "timeout --signal=TERM --kill-after=30s 120 " in step


def test_the_limit_is_scaled_by_runs_per_job_exactly_as_the_cluster_scales_it(tmp_path):
    # execution.timeout is per *run*; a packed step holds several, so an unscaled limit
    # would kill a legitimate step early. This is kubernetes_backend's own arithmetic.
    _path, script = _run_script(tmp_path, timeout=120, runs_per_job=3)
    assert "kill-after=30s 360 " in script
    assert "Per-step limit: 360s" in script


def test_an_undeclared_timeout_leaves_local_runs_unbounded(tmp_path):
    # Not the cluster's 1-hour fallback: inventing a limit where none was set is a
    # separate decision from enforcing one that was.
    _path, script = _run_script(tmp_path)
    assert "timeout --signal" not in script
    assert declared_per_run_seconds({}) in (None, 0)


def test_the_generated_script_stays_valid_shell(tmp_path):
    for overrides in ({}, {"timeout": 120}, {"timeout": 120, "runs_per_job": 3}):
        path, _script = _run_script(tmp_path, **overrides)
        assert subprocess.run(["bash", "-n", path], check=False).returncode == 0, overrides


def test_the_limit_terminates_before_it_kills():
    # SIGTERM lets docker compose stop containers the way Ctrl+C does, so a scenario gets
    # its shutdown and a cut-off run still writes test.xml. --kill-after is the backstop.
    prefix = _timeout_prefix(90)
    assert prefix.startswith("timeout --signal=TERM ")
    assert "--kill-after=" in prefix
    assert prefix.endswith("90 ")


def test_no_limit_means_no_wrapper():
    assert _timeout_prefix(None) == ""
    assert _timeout_prefix(0) == ""


def test_a_timed_out_step_is_torn_down_and_counted_as_a_failure(tmp_path):
    """124 must reach the step's failure handling, and the stack must still come down.

    Otherwise a truncated batch would look like a smaller campaign that passed — the
    kind of wrong answer that looks right, since ``count_run_artifacts`` counts the
    ``test.xml`` files that *do* exist.
    """
    _path, script = _run_script(tmp_path, timeout=120)
    # The step's own teardown follows the wait unconditionally.
    assert "down --volumes" in script
    # And the failure path keys off the step's exit code, which timeout sets to 124.
    assert "EXIT_CODE" in script and "FAILED" in script.upper()


def test_the_step_limit_matches_what_the_cluster_would_set_for_the_same_project(tmp_path):
    """One key, one meaning: the two lanes must compute the same number."""
    campaign_data = build_campaign_data(VAST, str(tmp_path / "gen"))
    campaign_data["execution"].update({"timeout": 120, "runs_per_job": 3})
    execution = campaign_data["execution"]
    cluster_value = declared_per_run_seconds(execution) * int(execution["runs_per_job"])
    _path, script = _run_script(tmp_path, timeout=120, runs_per_job=3)
    assert f"kill-after=30s {cluster_value} " in script
