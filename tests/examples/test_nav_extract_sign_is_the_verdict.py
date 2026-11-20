# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""A negative robustness margin is a failed run, and a failed run has a negative margin.

Nav2 declares arrival against its own pose estimate and its own tolerance; the arrival
margin reads the ground-truth distance at the last recorded sample, which carries the
localisation error and whatever settling happened after the recording closed. The two are
different quantities, so a run nav2 declared arrived can end a little beyond the arrival
radius and a run it aborted can end a little inside it -- and a cell whose runs all passed
then carries a slightly negative margin, which every reader of ``robustness < 0`` counts as
a failure.

The verdict is the arrival oracle, as the contact monitor is the collision oracle, and it
bounds the measurement the same way a recorded contact bounds the clearance: a passed run's
distance is at most the arrival radius, a failed run's that did not collide is beyond it.
Correcting the input keeps the margin continuous and ordered; forcing the score would put a
verdict inside a margin, which this objective exists to avoid.
"""

import pytest

from tests.examples.nav_search_fixtures import example_module, write_run


def _score(tmp_path, name, **run):
    config_dir = tmp_path / name
    write_run(config_dir, 0, **run)
    result = example_module("nav_extract").NavExtract(timeout=120.0).extract(config_dir)
    return result.objectives["robustness"], result.measures["failure_mode"]


def test_a_passed_run_that_settled_past_the_radius_does_not_fail(tmp_path):
    robustness, mode = _score(tmp_path, "settled", clearance=0.3, duration=40.0,
                              to_goal=0.615, collided=False, passed=True)
    assert robustness >= 0
    assert mode == "none"


def test_a_failed_run_that_stopped_inside_the_radius_fails(tmp_path):
    robustness, mode = _score(tmp_path, "aborted", clearance=0.3, duration=40.0,
                              to_goal=0.5, collided=False, passed=False)
    assert robustness < 0
    assert mode == "goal_miss"


def test_a_timed_out_run_fails_whatever_its_last_sample_says(tmp_path):
    """A navigation 1 s short of the timeout has a time margin that is still positive; the
    verdict says it did not arrive, and the arrival margin carries that."""
    robustness, mode = _score(tmp_path, "slow", clearance=0.3, duration=119.0,
                              to_goal=0.4, collided=False, passed=False)
    assert robustness < 0
    assert mode == "timeout"


def test_the_bound_keeps_passed_runs_ordered_by_how_well_they_arrived(tmp_path):
    centred, _ = _score(tmp_path, "centred", clearance=0.3, duration=40.0, to_goal=0.3,
                        collided=False, passed=True)
    edge, _ = _score(tmp_path, "edge", clearance=0.3, duration=40.0, to_goal=0.615,
                     collided=False, passed=True)
    assert centred > edge >= 0


def test_a_collision_is_scored_from_its_clearance_not_its_arrival(tmp_path):
    """The bound is for runs the arrival oracle decided; a contact ended the trial and the
    clearance term already carries the failure."""
    robustness, mode = _score(tmp_path, "hit", clearance=-0.02, duration=30.0,
                              to_goal=3.0, collided=True, passed=False)
    assert robustness < -0.5
    assert mode == "collision"


@pytest.mark.parametrize("passed,to_goal,duration,collided", [
    (True, 0.1, 30.0, False), (True, 0.55, 30.0, False), (True, 0.7, 30.0, False),
    (False, 0.2, 30.0, False), (False, 0.59, 30.0, False), (False, 2.0, 30.0, False),
    (False, 0.4, 125.0, False), (False, 0.1, 20.0, True), (False, 4.0, 20.0, True),
])
def test_the_sign_is_the_verdict(tmp_path, passed, to_goal, duration, collided):
    robustness, _ = _score(tmp_path, "run", clearance=-0.03 if collided else 0.3,
                           duration=duration, to_goal=to_goal, collided=collided,
                           passed=passed)
    assert (robustness < 0) == (not passed)
