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

A fourth is added by the move to a central index: ``poses`` is one table holding every
campaign, so a query scoped only by ``(config_name, run_id)`` reads whichever campaigns
happen to share those keys.

The tests that need the index are gated on ``ROBOVAST_TEST_PG_DSN``; the map-reading and
refusal ones read files only and stay ungated.

These were written as ``xfail`` against a plugin that had not been ported to the index --
as the passing behaviour rather than the behaviour of the day -- and turned green when
``robovast_nav.mcp_plugin`` was ported to it.
"""

import itertools
import math
import os
import sqlite3

import pytest

from robovast.mcp_server import service_access
from robovast_nav import mcp_plugin as nav

DSN = os.environ.get("ROBOVAST_TEST_PG_DSN")

#: Namespaced so a shared test database can hold several suites at once.
SCHEMA = "mcp_nav_test"

_CAMPAIGN = "nav-2026-07-16-120000"

#: A second campaign recording the SAME configuration and run id. Its poses sit far away
#: in space, so a row of it leaking into a trajectory is unmistakable rather than merely
#: extra.
_OTHER = "nav-2026-07-16-130000"
_OTHER_OFFSET = 1000.0

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


def _write_campaign(root, name: str, offset: float = 0.0):
    """A campaign whose ``poses.csv`` the ingest turns into an indexed ``poses`` table.

    The pose contract's two clocks are both written. ``timestamp`` is arrival:
    deliberately quantised onto a coarse grid here, the way a real /clock grid quantises
    it, so a tool that differentiates the wrong column reports a different number and the
    test can tell. ``stamp`` is the true measurement time, evenly spaced -- which is what
    the speeds below are the truth for.
    """
    cdir = root / name
    run = cdir / "cfg-a" / "0"
    run.mkdir(parents=True)
    (cdir / "_execution").mkdir(parents=True)
    header = "timestamp,stamp,frame,position.x,position.y,orientation.yaw"
    lines = [header]
    for frame in ("base_link", "odom"):
        for (t, x, y, yaw) in _POSES:
            lines.append(f"{_arrival(t)},{t},{frame},{x + offset},{y + offset},{yaw}")
    (run / "poses.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # A minimal record, so the campaign has the `runs` dimension row a reader joins to.
    db = sqlite3.connect(cdir / "campaign.db")
    db.executescript(
        "CREATE TABLE campaign (id INTEGER PRIMARY KEY, name TEXT, config_json TEXT);"
        "CREATE TABLE batch (id INTEGER PRIMARY KEY, campaign_id INTEGER, idx INTEGER);"
        "CREATE TABLE unit (id INTEGER PRIMARY KEY, batch_id INTEGER, config_name TEXT,"
        "                   paramset_id TEXT, params_json TEXT, objective REAL,"
        "                   status TEXT);"
        "CREATE TABLE run (id INTEGER PRIMARY KEY, unit_id INTEGER, run_id INTEGER,"
        "                  status TEXT, passed INTEGER, duration_s REAL, errors INTEGER,"
        "                  failures INTEGER, tests INTEGER, start_time TEXT,"
        "                  failure_message TEXT, job_id INTEGER);")
    db.execute("INSERT INTO campaign VALUES (1, ?, '{}')", (name,))
    db.execute("INSERT INTO batch VALUES (1, 1, 0)")
    db.execute("INSERT INTO unit VALUES (1, 1, 'cfg-a', 'ps-1', '{}', 0.5, 'evaluated')")
    db.execute("INSERT INTO run VALUES (1, 1, 0, 'passed', 1, 59.0, 0, 0, 1, 't', NULL, NULL)")
    db.commit()
    db.close()
    return cdir


@pytest.fixture
def campaign_tree(tmp_path, monkeypatch):
    """The campaign on disk, with nothing ingested. For the tests that read files only."""
    # The results root is derived from the workspaces store, and a CWD .robovast_project
    # would otherwise win the precedence and point the tools at the developer's own tree.
    monkeypatch.setenv("ROBOVAST_WORKSPACES_ROOT", str(tmp_path / "workspaces"))
    monkeypatch.setattr(service_access, "service_client", lambda: None)
    return _write_campaign(tmp_path / "results", _CAMPAIGN)


@pytest.fixture
def campaign(campaign_tree, tmp_path, monkeypatch):
    """That campaign's poses in the index, with a second campaign's beside them."""
    if not DSN:
        pytest.skip("ROBOVAST_TEST_PG_DSN is not set")
    psycopg = pytest.importorskip("psycopg")
    from robovast.common import index_db

    with psycopg.connect(DSN, autocommit=True) as setup:
        for statement in (f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE",
                          "DROP SCHEMA IF EXISTS campaign CASCADE",
                          f"CREATE SCHEMA {SCHEMA}"):
            setup.execute(statement)
    monkeypatch.setenv(index_db.DSN_ENV, f"{DSN} options=-csearch_path={SCHEMA}")

    _ingest(campaign_tree)
    _ingest(_write_campaign(tmp_path / "results", _OTHER, offset=_OTHER_OFFSET))
    yield str(campaign_tree)
    with psycopg.connect(DSN, autocommit=True) as teardown:
        teardown.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        teardown.execute("DROP SCHEMA IF EXISTS campaign CASCADE")


def _ingest(root) -> None:
    from robovast.results_processing import campaign_ingest, index_query, index_views

    with index_query.open_index(readonly=False) as conn:
        campaign_ingest.ingest_campaign(conn, str(root), root.name)
        index_views.create_views(conn)


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


def test_a_trajectory_holds_only_the_campaign_it_was_asked_about(campaign):
    """One ``poses`` table holds every campaign, so scoping is the query's job.

    The other campaign records the same configuration and run id, a kilometre away. Read
    unscoped, the trajectory silently doubles in length and its distance is dominated by
    the jump between the two campaigns' tracks -- a number nothing downstream can tell is
    wrong.
    """
    result = nav.nav_get_trajectory(_CAMPAIGN, "cfg-a", 0, limit=2 * len(_POSES))
    assert result["total_points"] == len(_POSES)
    assert all(abs(p["x"]) <= 2.0 for p in result["points"]), \
        "no pose of the other campaign may reach this trajectory"


def test_sqrt_is_available_to_the_distance_sum(campaign):
    """The distance sum needs SQRT, and the schema note promises it is there.

    On SQLite it had to be registered (its own is a compile-time option) and the shim
    answered NULL for an argument it could not take a root of. Postgres ships one that
    *refuses* instead -- the guarantee that survives is the one that matters: a distance
    is never silently computed from a wrong number.
    """
    import psycopg

    from robovast.results_processing.data_query import open_data_db
    conn = open_data_db(campaign)
    try:
        assert conn.execute("SELECT SQRT(9)").fetchone()["sqrt"] == 3.0
        for refused in ("SELECT SQRT(-1)", "SELECT SQRT('nope')"):
            with pytest.raises(psycopg.Error):
                conn.execute(refused).fetchone()
            conn.rollback()
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


def test_an_unknown_campaign_is_an_error_result_not_an_exception(campaign_tree):
    """Raising here reaches an MCP client as a broken server."""
    result = nav.nav_get_trajectory("no-such-2026-01-01-000000", "cfg-a", 0)
    assert "error" in result


def test_obstacles_refuse_cleanly_when_the_config_has_no_scenario_config(campaign_tree):
    result = nav.nav_get_obstacles(_CAMPAIGN, "cfg-a")
    assert "error" in result


def test_map_info_says_this_is_not_a_navigation_config(campaign_tree):
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
    """A campaign whose configuration carries a one-row occupancy map.

    No index: the map is read from the campaign's files, and pinning that it stays a file
    read is part of the point -- a raster is not a table.
    """
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
