# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""A check that could not run must not take the checks that could down with it.

Composing a ``.vast`` asks the simulator's own image one question -- which files a world
made of several actually needs -- and that question needs a container. A deployment whose
exec path does not stream cannot answer it, and the failure propagated out of composition
into the pre-flight report's generic arm: a hard ``error`` with ``configs: 0``, for every
project on that deployment.

Zero configurations is not what the file expands to, and the cartesian expansion that
produces the real number needs no container at all. So the unanswerable half is dropped
and *said*, and the half that needs nothing is reported -- the same move the world query
already makes when it is asked again without its overrides.
"""

import pytest

from robovast.common import config_generation
from robovast.common.config_validation import validate_project_file
from robovast.common.errors import ExecPathUnavailable

#: A roqsim campaign, whose backend answers `input_files` with a ContainerQuery. Five
#: configurations, five runs each -- the numbers a report must still produce.
BATCH_VAST = "configs/examples/basic_nav/basic_nav_roqsim.vast"
SEARCH_VAST = "configs/examples/nav_search/nav_search_tpe_6d.vast"


@pytest.fixture
def exec_path_down(monkeypatch):
    """Every input-files query refuses the way an unstreamable exec path refuses."""

    def _refuse(*_a, **_k):
        raise ExecPathUnavailable(
            "no command can run in a container on this deployment: the connection was "
            "never upgraded to a websocket. Nothing that has to ask a container a "
            "question can be answered here; everything that needs none is unaffected")

    monkeypatch.setattr(config_generation, "_run_input_files_query", _refuse)


def _generation_problems(report):
    return [p for p in report["problems"] if p.get("stage") == "generation"]


def test_a_deployment_that_cannot_exec_still_reports_what_the_file_expands_to(exec_path_down):
    report = validate_project_file(BATCH_VAST)
    assert report["configs"] == 5, "the cartesian expansion needs no container"
    assert report["total_trials"] == 25


def test_the_query_that_did_not_run_is_unchecked_rather_than_an_error(exec_path_down):
    """``unchecked`` is the contract this report already keeps for the world and scenario
    checks: not verified must never read as fine, and must never read as broken either."""
    report = validate_project_file(BATCH_VAST)
    problems = _generation_problems(report)
    assert len(problems) == 1, problems
    assert problems[0]["severity"] == "unchecked"
    assert "did NOT run" in problems[0]["message"]
    assert not report["valid"], "an unchecked query leaves the file unverified"


def test_the_unchecked_problem_says_what_is_unanswered_not_that_the_vast_is_wrong(
        exec_path_down):
    """A reader who acts on this must not go looking for a mistake in their file, and must
    not read the configuration count as fully checked."""
    message = _generation_problems(validate_project_file(BATCH_VAST))[0]["message"]
    assert "nothing about the .vast changes this" in message
    assert "input-files query" in message


def test_a_search_vast_degrades_the_same_way(exec_path_down):
    """The search preview composes through the same query. Reported separately because it
    reaches it by its own path, and a fix to one arm has silently missed the other."""
    report = validate_project_file(SEARCH_VAST)
    assert report["configs"] > 0, "the sampled draws compose without a container"
    problems = _generation_problems(report)
    assert len(problems) == 1 and problems[0]["severity"] == "unchecked", problems


def test_any_other_composition_failure_is_still_fatal(monkeypatch):
    """Only an unanswerable exec path degrades. A query that ran and failed answered the
    caller's own question, and softening that would hide the very failure the query
    exists to prevent."""
    def _broken(*_a, **_k):
        raise RuntimeError("the simulator exited 1 without printing JSON")

    monkeypatch.setattr(config_generation, "_run_input_files_query", _broken)
    report = validate_project_file(BATCH_VAST)
    assert report["configs"] == 0
    assert _generation_problems(report)[0]["severity"] == "error"


def test_composing_to_run_still_asks_and_still_fails(exec_path_down):
    """The degraded composition is for a REPORT. A campaign composed to run must keep
    failing loudly: its world would otherwise travel without the parent it extends, and
    the run dies on a missing file after the image pull."""
    with pytest.raises(ExecPathUnavailable):
        config_generation.generate_scenario_variations(
            variation_file=BATCH_VAST, output_dir=None)
