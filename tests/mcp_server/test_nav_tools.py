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

import itertools
import math
import sqlite3

import pytest

from robovast.mcp_server import service_access
from robovast_nav import mcp_plugin as nav

_CAMPAIGN = "nav-2026-07-16-120000"

#: A quarter-circle drive: distance and heading are both non-trivial, so a helper that
#: drops the yaw or mis-sums the segments cannot pass by accident.
_POSES = [(float(t), math.cos(t / 10.0), math.sin(t / 10.0), t / 10.0) for t in range(60)]

#: Arrival grid: 1.5 s, against a 1.0 s measurement period. 1.5 does not divide 1.0, so arrival
#: times alternate 1/2/1/2 exactly as a real /clock grid makes them -- the artefact this contract
#: exists to keep out of a derivative.
_ARRIVAL_GRID = 1.5


def _arrival(stamp: float) -> float:
    """The arrival time a sample published at *stamp* would be recorded with."""
    return math.floor(stamp / _ARRIVAL_GRID) * _ARRIVAL_GRID


@pytest.fixture
def campaign(tmp_path, monkeypatch):
    """A campaign whose data.db holds a ``poses`` table, reached over the local lane."""
    # The results root is derived from the workspaces store, and a CWD .robovast_project
    # would otherwise win the precedence and point the tools at the developer's own tree.
    monkeypatch.setenv("ROBOVAST_WORKSPACES_ROOT", str(tmp_path / "workspaces"))
    cdir = tmp_path / "results" / _CAMPAIGN
    (cdir / "_execution").mkdir(parents=True)
    db = sqlite3.connect(cdir / "_execution" / "data.db")
    # The pose contract's two clocks. `timestamp` is arrival: deliberately quantised onto a coarse
    # grid here, the way a real /clock grid quantises it, so a tool that differentiates the wrong
    # column reports a different number and the test can tell. `stamp` is the true measurement
    # time, evenly spaced -- which is what the speeds below are the truth for.
    db.execute('CREATE TABLE poses (config_name TEXT, run_id INTEGER, frame TEXT, '
               '"timestamp" REAL, "stamp" REAL, "position.x" REAL, "position.y" REAL, '
               '"orientation.yaw" REAL)')
    rows = [(_arrival(t), t, x, y, yaw) for (t, x, y, yaw) in _POSES]
    db.executemany("INSERT INTO poses VALUES ('cfg-a', 0, 'base_link', ?, ?, ?, ?, ?)", rows)
    db.executemany("INSERT INTO poses VALUES ('cfg-a', 0, 'odom', ?, ?, ?, ?, ?)", rows)
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
    """Raising here reaches an MCP client as a broken server."""
    result = nav.nav_get_trajectory("no-such-2026-01-01-000000", "cfg-a", 0)
    assert "error" in result


def test_obstacles_refuse_cleanly_when_the_config_has_no_scenario_config(campaign):
    result = nav.nav_get_obstacles(_CAMPAIGN, "cfg-a")
    assert "error" in result


def test_map_info_says_this_is_not_a_navigation_config(campaign):
    result = nav.nav_get_map_info(_CAMPAIGN, "cfg-a")
    assert "not a navigation configuration" in result["error"]


def test_max_speed_is_computed_from_the_measurement_clock_not_the_arrival_clock(campaign):
    """The regression this contract exists for.

    ``max_speed_m_s`` is a ``MAX(distance/dt)``, so it structurally picks whichever pair of samples
    got the smallest ``dt``. On an arrival clock that is the worst quantisation artefact in the run
    rather than the robot's fastest moment: here the poses are evenly spaced 1 s apart in true time,
    but arrive on a 1.5 s grid, so a third of the pairs report an arrival ``dt`` of 0 and the rest
    alternate 1 s / 2 s.

    Truth: a unit circle sampled every 0.1 rad, so every step is the same 2*sin(0.05) ~ 0.09992 m
    over 1 s. The maximum speed is that, and nothing in the run is faster.
    """
    result = nav.nav_get_trajectory(_CAMPAIGN, "cfg-a", 0, stats_only=True)
    true_step = 2 * math.sin(0.05)
    assert result["max_speed_m_s"] == pytest.approx(true_step, rel=1e-6)

    # What the arrival clock would have said, computed the same way, for contrast: the 1 s pairs
    # are reported over an arrival gap of 0 or 1 s, so the figure is at best unchanged and at worst
    # unbounded. Anything that differentiates `timestamp` cannot land on the truth above.
    arrivals = [_arrival(t) for (t, *_rest) in _POSES]
    gaps = {round(b - a, 6) for a, b in itertools.pairwise(arrivals)}
    assert gaps == {0.0, 1.5}, "the fixture must actually exhibit the aliasing it is testing for"


#: One cell per class, by the map server's rule with the usual thresholds
#: (``occupied_thresh`` 0.65, ``free_thresh`` 0.196): occupancy is ``1 - pixel/255``, so
#: 254 is free, 0 and 60 are occupied, and 205 and 200 fall in the unknown band. 60 and
#: 200 are the ones that separate the two readings — against the *shade* instead of the
#: occupancy, 60 reads unknown and 200 reads free.
_MAP_PIXELS = [254, 205, 0, 60, 200]
_EXPECTED_CELLS = {"occupied": 2, "free": 1, "unknown": 2}


@pytest.fixture
def map_campaign(tmp_path, monkeypatch):
    """A campaign whose configuration carries a one-row occupancy map."""
    pytest.importorskip("PIL")
    from PIL import Image as PILImage

    monkeypatch.setenv("ROBOVAST_WORKSPACES_ROOT", str(tmp_path / "workspaces"))
    cdir = tmp_path / "results" / _CAMPAIGN
    maps = cdir / "cfg-map" / "_config" / "maps"
    maps.mkdir(parents=True)
    image = PILImage.new("L", (len(_MAP_PIXELS), 1))
    image.putdata(_MAP_PIXELS)
    image.save(maps / "room.pgm")
    (maps / "room.yaml").write_text(
        "image: room.pgm\nresolution: 0.05\norigin: [0.0, 0.0, 0.0]\n"
        "negate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\n")
    monkeypatch.setattr(service_access, "service_client", lambda: None)
    return str(cdir)


def test_occupancy_classifies_cells_by_occupancy_not_by_shade(map_campaign):
    """The thresholds belong to the occupancy the map server derives, not to the stored
    shade. Read against the shade, the two are swapped: a nearly-black cell counts as
    unknown and an unknown-grey one as free, while the totals still add up — so nothing
    downstream can tell the counts are wrong."""
    result = nav.nav_get_map_info(_CAMPAIGN, "cfg-map", occupancy=True)
    assert (result["occupied_cells"], result["free_cells"], result["unknown_cells"]) == (
        _EXPECTED_CELLS["occupied"], _EXPECTED_CELLS["free"], _EXPECTED_CELLS["unknown"])
    assert result["total_cells"] == len(_MAP_PIXELS)
    assert result["occupied_ratio"] == pytest.approx(2 / len(_MAP_PIXELS))


def test_negate_inverts_which_cells_are_occupied(map_campaign, tmp_path):
    """``negate: 1`` says the raster stores occupancy directly. Reporting the field while
    ignoring it answers about a map nobody has."""
    maps = tmp_path / "results" / _CAMPAIGN / "cfg-map" / "_config" / "maps"
    (maps / "room.yaml").write_text(
        "image: room.pgm\nresolution: 0.05\norigin: [0.0, 0.0, 0.0]\n"
        "negate: 1\noccupied_thresh: 0.65\nfree_thresh: 0.196\n")
    result = nav.nav_get_map_info(_CAMPAIGN, "cfg-map", occupancy=True)
    # The same pixels read as occupancy directly: 254/205/200 are occupied, 0 is free,
    # and 60 lands in the unknown band -- the mirror image of the reading above.
    assert (result["occupied_cells"], result["free_cells"], result["unknown_cells"]) == (
        3, 1, 1)


def test_a_palette_image_counts_cells_not_channels(map_campaign, tmp_path):
    """A palette or RGB raster's raw array is indices or three planes, so counting it
    unconverted reports a cell count that is not the map's."""
    from PIL import Image as PILImage
    maps = tmp_path / "results" / _CAMPAIGN / "cfg-map" / "_config" / "maps"
    rgb = PILImage.new("RGB", (len(_MAP_PIXELS), 1))
    rgb.putdata([(p, p, p) for p in _MAP_PIXELS])
    rgb.save(maps / "room.png")
    (maps / "room.yaml").write_text(
        "image: room.png\nresolution: 0.05\norigin: [0.0, 0.0, 0.0]\n"
        "negate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\n")
    result = nav.nav_get_map_info(_CAMPAIGN, "cfg-map", occupancy=True)
    assert result["total_cells"] == len(_MAP_PIXELS)
    assert result["occupied_cells"] == _EXPECTED_CELLS["occupied"]
