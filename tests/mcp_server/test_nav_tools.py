# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The nav tools read the database, and refuse in the shape every other tool refuses in.

Three defect classes are pinned here, all found in the CSV-reading version these replaced:

* it opened ``poses.csv`` on this host, so every cluster campaign was "not found";
* it read ``orientation.x/y/z/w`` from a file that records ``orientation.roll/pitch/yaw``,
  so ``_quaternion_to_yaw(0, 0, 0, 1)`` returned **0.0 for every pose** — a wrong answer
  that looked like a real one;
* it let ``ValueError`` escape, so an unknown campaign reached the client as a protocol
  error rather than as ``{"error": …}``.
"""

import math
import sqlite3

import pytest

from robovast.mcp_server import service_access
from robovast_nav import mcp_plugin as nav

_CAMPAIGN = "nav-2026-07-16-120000"

#: A quarter-circle drive: distance and heading are both non-trivial, so a helper that
#: drops the yaw or mis-sums the segments cannot pass by accident.
_POSES = [(t, math.cos(t / 10.0), math.sin(t / 10.0), t / 10.0) for t in range(60)]


@pytest.fixture
def campaign(tmp_path, monkeypatch):
    """A campaign whose data.db holds a ``poses`` table, reached over the local lane."""
    # The results root is derived from the workspaces store, and a CWD .robovast_project
    # would otherwise win the precedence and point the tools at the developer's own tree.
    monkeypatch.setattr(
        "robovast.common.cli.project_config.ProjectConfig.load",
        staticmethod(lambda *a, **k: None))
    monkeypatch.setenv("ROBOVAST_WORKSPACES_ROOT", str(tmp_path / "workspaces"))
    cdir = tmp_path / "results" / _CAMPAIGN
    (cdir / "_execution").mkdir(parents=True)
    db = sqlite3.connect(cdir / "_execution" / "data.db")
    db.execute('CREATE TABLE poses (config_name TEXT, run_id INTEGER, frame TEXT, '
               '"timestamp" REAL, "position.x" REAL, "position.y" REAL, '
               '"orientation.yaw" REAL)')
    db.executemany("INSERT INTO poses VALUES ('cfg-a', 0, 'base_link', ?, ?, ?, ?)",
                   _POSES)
    db.executemany("INSERT INTO poses VALUES ('cfg-a', 0, 'odom', ?, ?, ?, ?)", _POSES)
    db.commit()
    db.close()
    monkeypatch.setattr(service_access, "service_client", lambda: None)
    return str(cdir)


def test_trajectory_reports_the_recorded_yaw(campaign):
    """The euler yaw the recording holds — not 0.0 from a quaternion that is not there."""
    result = nav.nav_get_trajectory(_CAMPAIGN, "cfg-a", 0, limit=len(_POSES))
    assert result["total_points"] == len(_POSES)
    assert result["sampled"] is False
    assert [round(p["yaw"], 6) for p in result["points"]] == \
        [round(p[3], 6) for p in _POSES]


def test_trajectory_samples_at_a_stride_and_says_so(campaign):
    """A thinned trajectory that does not report the true count reads as the whole one."""
    result = nav.nav_get_trajectory(_CAMPAIGN, "cfg-a", 0, limit=10)
    assert result["returned_points"] <= 10
    assert result["total_points"] == len(_POSES)
    assert result["sampled"] is True


def test_stats_are_computed_over_every_pose_not_the_returned_ones(campaign):
    """``limit`` bounds the points; a statistic taken from a sample is a different number."""
    stats = nav.nav_get_trajectory(_CAMPAIGN, "cfg-a", 0, limit=5, stats_only=True)
    assert stats["num_points"] == len(_POSES)

    expected = sum(math.dist(_POSES[i][1:3], _POSES[i - 1][1:3])
                   for i in range(1, len(_POSES)))
    assert stats["total_distance_m"] == pytest.approx(expected)
    assert stats["duration_sec"] == pytest.approx(_POSES[-1][0] - _POSES[0][0])
    assert stats["start_pose"]["yaw"] == pytest.approx(_POSES[0][3])
    assert stats["end_pose"]["yaw"] == pytest.approx(_POSES[-1][3])


def test_sqrt_is_registered_rather_than_assumed(campaign):
    """The distance sum needs SQRT; SQLite's own is a compile-time option, so we ship one."""
    from robovast.results_processing.data_query import open_data_db
    conn = open_data_db(campaign)
    try:
        assert conn.execute("SELECT SQRT(9)").fetchone()[0] == 3.0
        assert conn.execute("SELECT SQRT(-1)").fetchone()[0] is None
        assert conn.execute("SELECT SQRT('nope')").fetchone()[0] is None
    finally:
        conn.close()


def test_an_unrecorded_frame_is_refused_with_the_ones_that_exist(campaign):
    """An empty result and a typo look identical; naming the frames tells them apart."""
    result = nav.nav_get_trajectory(_CAMPAIGN, "cfg-a", 0, frame="base_footprint")
    assert "base_link" in result["error"] and "odom" in result["error"]


def test_a_missing_table_names_the_postprocessing_step_that_makes_it(campaign):
    """"no such table: …" is not something a caller can act on; the plugin name is."""
    result = nav.nav_get_action_feedback(_CAMPAIGN, "cfg-a", 0)
    assert "rosbags_action_to_csv" in result["error"]
    assert "poses" in result["error"]  # lists what the campaign does have


def test_an_unknown_campaign_is_an_error_result_not_an_exception(campaign):
    """It used to raise, which reaches an MCP client as a broken server."""
    result = nav.nav_get_trajectory("no-such-2026-01-01-000000", "cfg-a", 0)
    assert "error" in result


def test_obstacles_refuse_cleanly_when_the_config_has_no_scenario_config(campaign):
    result = nav.nav_get_obstacles(_CAMPAIGN, "cfg-a")
    assert "error" in result


def test_map_info_says_this_is_not_a_navigation_config(campaign):
    result = nav.nav_get_map_info(_CAMPAIGN, "cfg-a")
    assert "not a navigation configuration" in result["error"]
