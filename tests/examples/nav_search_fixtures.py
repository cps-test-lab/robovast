# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""What the nav_search example's tests build a campaign directory from.

Shared because three plugins read the same layout -- ``NavMetrics`` the per-run tables and
the configuration's resolved scenario parameters, ``NavExtract`` the per-run metrics row and
the verdict -- and a fixture written twice drifts into two layouts that each test believes.
"""

import csv
import importlib.util
import pathlib

import pytest
import yaml

EXAMPLE = pathlib.Path(__file__).resolve().parents[2] / "configs" / "examples" / "nav_search"


def example_module(name: str):
    """The example's ``search/<name>.py`` loaded as a module, or a skip when absent."""
    path = EXAMPLE / "search" / f"{name}.py"
    if not path.is_file():
        pytest.skip("nav_search example not present")
    spec = importlib.util.spec_from_file_location(f"nav_search_{name}_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_scenario_config(config_dir: pathlib.Path, *, goal=(2.5, 0.0), scenario="test_scenario"):
    """The configuration's resolved scenario parameters, as the campaign freezes them."""
    (config_dir / "_config").mkdir(parents=True, exist_ok=True)
    doc = {scenario: {"start_pose": {"position": {"x": -2.5, "y": 0.0}}}}
    if goal is not None:
        doc[scenario]["goal_pose"] = {"position": {"x": goal[0], "y": goal[1]}}
    (config_dir / "_config" / "scenario.config").write_text(yaml.safe_dump(doc), encoding="utf-8")


def write_verdict(run_dir: pathlib.Path, *, passed: bool):
    """The run's ``test.xml`` with its verdict."""
    verdict = "" if passed else '<failure message="x"/>'
    (run_dir / "test.xml").write_text(
        '<?xml version="1.0"?>\n'
        f'<testsuite tests="1" failures="{0 if passed else 1}" errors="0">'
        f'<testcase name="t">{verdict}</testcase></testsuite>\n', encoding="utf-8")


def write_metrics_row(run_dir: pathlib.Path, *, clearance, duration, to_goal, collided,
                      recoveries=0):
    """The per-run row ``NavMetrics`` leaves; an empty value is written as an empty cell."""
    with open(run_dir / "nav_metrics.csv", "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "min_clearance", "duration_s", "final_distance_to_goal", "collided",
            "recovery_count"])
        writer.writeheader()
        writer.writerow({"min_clearance": clearance, "duration_s": duration,
                         "final_distance_to_goal": to_goal,
                         "collided": 1 if collided else 0, "recovery_count": recoveries})


def write_run(config_dir: pathlib.Path, index: int, *, clearance, duration, to_goal, collided,
              passed=True):
    """One run directory shaped the way the postprocessing plugin and the trial leave it."""
    run_dir = config_dir / str(index)
    run_dir.mkdir(parents=True)
    write_metrics_row(run_dir, clearance=clearance, duration=duration, to_goal=to_goal,
                      collided=collided)
    write_verdict(run_dir, passed=passed)
    return run_dir
