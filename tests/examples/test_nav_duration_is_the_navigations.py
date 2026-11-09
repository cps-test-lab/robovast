# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""The time margin reads the navigation's duration, not the recording's.

A recording starts with the simulator: the stack launches, the initial pose is set, and
only then is a goal sent. The trial's timeout bounds the navigation, so a duration measured
from the first pose sample carries the bring-up and a crossing that navigated well inside
the timeout reads as having run past it -- a passing run scored as a timeout, which is what
a search minimising the margin then chases.

``NavMetrics`` takes the navigation's start from the first behaviour-tree transition, which
is nav2 accepting the goal. A run without one has no measured start and no duration; the
extractor drops that margin for the run and refuses a cell where every run lacks it, as it
does for a missing clearance, rather than defaulting the duration to the timeout and scoring
the margin at exactly the level a boundary search traces.
"""

import csv

import pytest

from tests.examples.nav_search_fixtures import (example_module, write_metrics_row,
                                                write_scenario_config, write_verdict)
from tests.robovast_data.conftest import write_store


def _run(tmp_path, *, transitions):
    """A run whose recording starts 30 s before the goal is sent and ends 40 s after."""
    campaign = tmp_path / "nav-2026-01-01-00000000"
    run = campaign / "c0" / "0"
    run.mkdir(parents=True)
    write_store(campaign, {"c0": {"runs": {0: "passed"}}})
    write_scenario_config(campaign / "c0")
    (run / "poses.csv").write_text(
        "timestamp,frame,position.x,position.y\n"
        "0.0,base_link_gt,-2.5,0.0\n"
        "30.0,base_link_gt,-2.5,0.0\n"
        "70.0,base_link_gt,2.4,0.0\n", encoding="utf-8")
    (run / "rosbag2_collision.csv").write_text("data\nfalse\n", encoding="utf-8")
    (run / "rosbag2_clearance.csv").write_text("data\n0.42\n", encoding="utf-8")
    with open(run / "nav2_behaviors.csv", "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["timestamp", "behavior_name", "status_name"])
        writer.writeheader()
        for row in transitions:
            writer.writerow(row)
    return run


BASELINE = {"timestamp": 30.0, "behavior_name": "NavigateRecovery", "status_name": "INVALID"}
ACCEPTED = {"timestamp": 30.0, "behavior_name": "NavigateRecovery", "status_name": "RUNNING"}
ARRIVED = {"timestamp": 70.0, "behavior_name": "NavigateRecovery", "status_name": "SUCCESS"}


def test_the_duration_runs_from_the_first_transition_to_the_last_pose(tmp_path):
    module = example_module("nav_metrics")
    run = _run(tmp_path, transitions=[BASELINE, ACCEPTED, ARRIVED])

    metrics = module._metrics_for_run(run, "ground_truth_poses", (2.5, 0.0))

    assert metrics["duration_s"] == 40.0


def test_a_baseline_row_is_not_a_transition(tmp_path):
    """The tree plugin writes one INVALID row per node that never ran, stamped at the first
    event; a run whose only rows are those has no navigation start."""
    module = example_module("nav_metrics")
    run = _run(tmp_path, transitions=[{**BASELINE, "timestamp": 0.0}])

    metrics = module._metrics_for_run(run, "ground_truth_poses", (2.5, 0.0))

    assert metrics is not None and metrics["duration_s"] == ""


def test_a_run_without_a_transition_has_no_duration_and_is_otherwise_measured(tmp_path):
    module = example_module("nav_metrics")
    run = _run(tmp_path, transitions=[])

    metrics = module._metrics_for_run(run, "ground_truth_poses", (2.5, 0.0))

    assert metrics["duration_s"] == ""
    assert metrics["min_clearance"] == 0.42
    assert metrics["final_distance_to_goal"] == pytest.approx(0.1)


# -- the extractor's half ------------------------------------------------------------------

def _cell(tmp_path, durations):
    config_dir = tmp_path / "cell"
    for index, duration in enumerate(durations):
        run_dir = config_dir / str(index)
        run_dir.mkdir(parents=True)
        write_metrics_row(run_dir, clearance=0.9, duration=duration, to_goal=0.1, collided=False)
        write_verdict(run_dir, passed=True)
    return config_dir


def test_a_run_without_a_duration_is_scored_on_its_other_margins(tmp_path):
    extractor = example_module("nav_extract").NavExtract(timeout=120.0)
    measured = extractor.extract(_cell(tmp_path / "a", [20.0, 20.0]))
    partial = extractor.extract(_cell(tmp_path / "b", [20.0, ""]))

    # With a duration of 20 s the time margin is 0.83 and never the minimum, so dropping it
    # for one run leaves the cell's score where it was -- and no run scores the time margin
    # at the 0.0 a defaulted duration would give it.
    assert partial.objectives["robustness"] == measured.objectives["robustness"]
    assert partial.measures["time_to_goal"] == 20.0


def test_a_cell_whose_every_run_lacks_a_duration_is_refused_by_name(tmp_path):
    extractor = example_module("nav_extract").NavExtract(timeout=120.0)

    with pytest.raises(ValueError, match="navigation duration"):
        extractor.extract(_cell(tmp_path, ["", ""]))


def test_a_run_that_navigated_inside_the_timeout_is_not_a_timeout(tmp_path):
    """The whole point: the margin is positive for a navigation of 100 s under a 120 s
    timeout, whatever the recording's length was."""
    extractor = example_module("nav_extract").NavExtract(timeout=120.0)
    result = extractor.extract(_cell(tmp_path, [100.0]))

    assert result.objectives["robustness"] > 0
    assert result.measures["failure_mode"] == "none"
