# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A campaign directory assembled from the ``nav_run`` fixture recording."""

import os
import shutil
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "nav_run"

#: The decoder configuration the fixture's expected tables were produced with.
NAV_CONFIG = {"groups": [
    {"bag_dir": "rosbag2", "plugins": [
        {"type": "tf_to_csv", "frames": "all", "require": ["base_link", "robot_gt"]},
        {"type": "nav2_bt_to_csv"},
        {"type": "costmap_to_csv", "topics": ["/global_costmap/costmap"]},
        {"type": "to_csv", "topics": ["/collision", "/scan"]},
        {"type": "action_to_csv", "action": "navigate_to_pose"}]},
    {"bag_dir": "logs/rosout_bag", "plugins": [
        {"type": "rosout_to_csv"}, {"type": "clock_to_csv"}]}]}


def make_campaign(root: Path, runs=(("cfg", 0),), shared_job=False, verdict=True) -> Path:
    """A campaign whose every run carries the fixture recording and its job's."""
    for i, (config, run_id) in enumerate(runs):
        run = root / config / str(run_id)
        run.mkdir(parents=True)
        shutil.copytree(FIXTURE / "0" / "rosbag2", run / "rosbag2")
        job = root / "_jobs" / ("job-0" if shared_job else f"job-{i}")
        if not (job / "logs" / "rosout_bag").exists():
            (job / "logs").mkdir(parents=True, exist_ok=True)
            shutil.copytree(FIXTURE / "0" / "logs" / "rosout_bag", job / "logs" / "rosout_bag")
        (run / "job").symlink_to(os.path.relpath(job, run))
        if verdict:
            (run / "test.xml").write_text("<testsuite/>")
    return root


@pytest.fixture
def campaign(tmp_path) -> Path:
    return make_campaign(tmp_path / "nav-2026-01-01-00000000")
