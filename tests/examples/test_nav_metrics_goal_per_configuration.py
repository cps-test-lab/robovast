# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""``NavMetrics`` measures arrival against the goal each configuration navigated to.

A campaign may vary the goal: a search over where the robot is sent is the natural shape
of a coverage experiment. The goal is then a property of the configuration, carried by its
resolved scenario parameters, and a plugin parameter naming one goal for the whole campaign
would score every such run against the wrong point -- a run that arrived reads as metres
short, and a search optimising that arrival margin chases the plugin's constant rather than
the stack's behaviour.

A configuration without a goal pose is refused by name. Measuring it against a default
would be the same wrong number with nothing saying so.
"""

import csv

import pytest

from tests.examples.nav_search_fixtures import example_module, write_scenario_config
from tests.robovast_data.conftest import write_store


def _campaign(tmp_path, goals):
    """One passed run per configuration, each arriving at its own configuration's goal."""
    campaign = tmp_path / "nav-2026-01-01-00000000"
    for name, (gx, gy) in goals.items():
        run = campaign / name / "0"
        run.mkdir(parents=True)
        (run / "poses.csv").write_text(
            "timestamp,frame,position.x,position.y\n"
            "0.0,base_link_gt,-2.5,0.0\n"
            f"9.0,base_link_gt,{gx - 0.1},{gy}\n", encoding="utf-8")
        (run / "rosbag2_collision.csv").write_text("data\nfalse\n", encoding="utf-8")
        (run / "rosbag2_clearance.csv").write_text("data\n0.42\n", encoding="utf-8")
        if goals[name] is not None:
            write_scenario_config(campaign / name, goal=(gx, gy))
    write_store(campaign, {name: {"runs": {0: "passed"}} for name in goals})
    return campaign


def _distance(run_dir):
    with open(run_dir / "nav_metrics.csv", newline="", encoding="utf-8") as handle:
        return float(next(csv.DictReader(handle))["final_distance_to_goal"])


def test_each_configuration_is_scored_against_its_own_goal(tmp_path):
    module = example_module("nav_metrics")
    campaign = _campaign(tmp_path, {"near": (1.0, 1.0), "far": (-1.5, 1.5)})

    ok, note = module.NavMetrics()(str(campaign), str(tmp_path))

    assert ok and "2 run(s)" in note
    assert _distance(campaign / "near" / "0") == pytest.approx(0.1)
    assert _distance(campaign / "far" / "0") == pytest.approx(0.1)


def test_the_goal_is_read_from_the_resolved_scenario_parameters(tmp_path):
    module = example_module("nav_metrics")
    config_dir = tmp_path / "c0"
    write_scenario_config(config_dir, goal=(0.5, -1.25))

    assert module.goal_of(config_dir) == (0.5, -1.25)


def test_a_configuration_without_resolved_parameters_is_refused(tmp_path):
    module = example_module("nav_metrics")
    (tmp_path / "c0").mkdir()

    with pytest.raises(FileNotFoundError, match="scenario.config"):
        module.goal_of(tmp_path / "c0")


def test_a_configuration_without_a_goal_pose_is_refused_by_name(tmp_path):
    module = example_module("nav_metrics")
    write_scenario_config(tmp_path / "c0", goal=None)

    with pytest.raises(ValueError, match="goal_pose"):
        module.goal_of(tmp_path / "c0")


def test_the_plugin_refuses_rather_than_measures_against_a_default(tmp_path):
    """End to end: the refusal reaches the caller of the plugin, where the search loop sees it."""
    module = example_module("nav_metrics")
    campaign = _campaign(tmp_path, {"c0": (1.0, 1.0)})
    (campaign / "c0" / "_config" / "scenario.config").unlink()

    with pytest.raises(FileNotFoundError, match="c0"):
        module.NavMetrics()(str(campaign), str(tmp_path))


def test_a_goal_given_as_a_plugin_parameter_is_refused_by_name(tmp_path):
    """A ``.vast`` that still states ``goal_x`` would otherwise be scored against its
    configurations' goals with nothing saying the parameter it states is ignored."""
    from robovast.results_processing.postprocessing import execute_postprocessing_plugin

    module = example_module("nav_metrics")
    campaign = _campaign(tmp_path, {"near": (1.0, 1.0)})

    ok, message, _ = execute_postprocessing_plugin(
        "NavMetrics", module.NavMetrics(), {"goal_x": 2.5, "goal_y": 0.0},
        str(campaign), str(tmp_path), provenance_file=str(tmp_path / "prov.json"), debug=True)

    assert not ok
    assert "goal_x" in message
