# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``pose_track_view`` -- where did each entity go, over every recorded pose?

The view spans every table that follows the pose contract, so these pin what makes that
valid: each table on its own measurement clock, a length in 3D, one campaign's track never
joined to another's, and the campaign predicate reaching the tables underneath rather than
being applied after the whole corpus has been windowed.
"""

import json
import math
import os
from pathlib import Path

import pytest

DSN = os.environ.get("ROBOVAST_TEST_PG_DSN")
pg = pytest.mark.skipif(not DSN, reason="ROBOVAST_TEST_PG_DSN is not set")

SCHEMA = "pose_track_view_test"
CAMPAIGN = "camp-track-2026-09-21-120000"
OTHER = "camp-track-2026-09-21-130000"

#: Arrival grid of the bag-derived table: 1.5 s against a 1.0 s measurement period, so
#: arrival times alternate 1/2/1/2 the way a /clock grid makes them. A speed differenced on
#: that clock is not the robot's.
_ARRIVAL_GRID = 1.5


def _straight_line(n=11, speed=1.0):
    """``(t, x)`` for a drive along x at a constant *speed*, one sample per second."""
    return [(float(t), speed * t) for t in range(n)]


def _write(root: Path, name: str, header: str, lines) -> None:
    run = root / "cfg-a" / "0"
    run.mkdir(parents=True, exist_ok=True)
    (root / "_execution").mkdir(parents=True, exist_ok=True)
    (run / name).write_text("\n".join([header, *lines]) + "\n", encoding="utf-8")


def _poses_csv(root: Path, offset: float = 0.0) -> None:
    """A bag-derived table: arrival ``timestamp``, measurement ``stamp``, no z column.

    One latched row with no ``stamp`` rides along, the way ``/tf_static`` arrives.
    """
    lines = [f"{math.floor(t / _ARRIVAL_GRID) * _ARRIVAL_GRID},{t},base_link,"
             f"{x + offset},{offset},0.0" for t, x in _straight_line()]
    lines.append(f"0.0,,base_link,{500 + offset},{500 + offset},0.0")
    _write(root, "poses.csv", "timestamp,stamp,frame,position.x,position.y,orientation.yaw",
           lines)


def _sim_poses_csv(root: Path) -> None:
    """A simulator-written table: ``timestamp`` is the measurement clock, and it has z.

    ``gripper`` only rises, so a planar length would call it stationary.
    """
    lines = [f"{t},base_link,{x},0.0,0.0" for t, x in _straight_line()]
    lines += [f"{t},gripper,0.0,0.0,{0.1 * t}" for t, _ in _straight_line()]
    # Spawned at the origin, then placed 5 m away before it drives 0.1 m per sample.
    lines += ["0.0,spawned,0.0,0.0,0.0"]
    lines += [f"{t},spawned,{5.0 + 0.1 * (t - 1)},0.0,0.0" for t in range(1, 6)]
    _write(root, "sim_poses.csv", "timestamp,frame,position.x,position.y,position.z", lines)


@pytest.fixture(name="index")
def _index(monkeypatch, tmp_path):
    """Two campaigns with the same configuration and run id, ingested with their views."""
    psycopg = pytest.importorskip("psycopg")
    from robovast.common import index_db
    from robovast.results_processing import campaign_ingest, index_query, index_views

    with psycopg.connect(DSN, autocommit=True) as setup:
        for statement in (f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE",
                          "DROP SCHEMA IF EXISTS campaign CASCADE",
                          f"CREATE SCHEMA {SCHEMA}"):
            setup.execute(statement)
    monkeypatch.setenv(index_db.DSN_ENV, f"{DSN} options=-csearch_path={SCHEMA}")

    mine = tmp_path / CAMPAIGN
    _poses_csv(mine)
    _sim_poses_csv(mine)
    # Far away, so a row of it leaking into this campaign's track is unmistakable.
    other = tmp_path / OTHER
    _poses_csv(other, offset=1000.0)
    with index_query.open_index(readonly=False) as conn:
        for root in (mine, other):
            campaign_ingest.ingest_campaign(conn, str(root), root.name)
        index_views.create_views(conn)
    yield

    with psycopg.connect(DSN, autocommit=True) as teardown:
        teardown.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        teardown.execute("DROP SCHEMA IF EXISTS campaign CASCADE")


def _tracks(campaign=CAMPAIGN) -> dict:
    from robovast.results_processing import index_query

    rows = index_query.query_index(
        f"SELECT * FROM pose_track_view WHERE campaign_id = '{campaign}'",
        campaign_id=campaign)["rows"]
    return {(r["source"], r["frame"]): r for r in rows}


@pg
def test_every_pose_contract_table_contributes_on_its_own_measurement_clock(index):
    """A bag-derived and a simulator-written table both appear, each timed correctly.

    On the arrival grid the same drive has steps of 1 m over 0 s and 1.5 s; only the
    measurement clock gives the constant 1 m/s the robot drove.
    """
    tracks = _tracks()
    assert set(tracks) == {("poses", "base_link"), ("sim_poses", "base_link"),
                           ("sim_poses", "gripper"), ("sim_poses", "spawned")}
    for source in ("poses", "sim_poses"):
        track = tracks[(source, "base_link")]
        assert track["points"] == 11
        assert track["length_m"] == pytest.approx(10.0)
        assert track["duration_s"] == pytest.approx(10.0)
        assert track["max_speed_m_s"] == pytest.approx(1.0)
        assert track["avg_speed_m_s"] == pytest.approx(1.0)
        assert (track["start_x"], track["end_x"]) == pytest.approx((0.0, 10.0))


@pg
def test_a_latched_sample_with_no_measurement_time_is_not_a_point(index):
    """The ``/tf_static`` row is 500 m away: in the track, it would dominate the length."""
    track = _tracks()[("poses", "base_link")]
    assert track["points"] == 11
    assert track["max_x"] == pytest.approx(10.0)


@pg
def test_a_vertical_motion_has_a_length(index):
    """The gripper only rises. A planar sum would report it as never having moved."""
    track = _tracks()[("sim_poses", "gripper")]
    assert track["length_m"] == pytest.approx(1.0)
    assert (track["min_z"], track["max_z"]) == pytest.approx((0.0, 1.0))


@pg
def test_a_reposition_is_travel_and_max_step_shows_it(index):
    """The view reports the jump rather than guessing a threshold to drop it."""
    track = _tracks()[("sim_poses", "spawned")]
    assert track["length_m"] == pytest.approx(5.4)
    assert track["max_step_m"] == pytest.approx(5.0)
    assert track["start_x"] == pytest.approx(0.0)


@pg
def test_a_track_holds_only_the_campaign_it_was_asked_about(index):
    """Same config, same run id, same frame: still two tracks, each of its own length."""
    mine = _tracks()[("poses", "base_link")]
    other = _tracks(OTHER)[("poses", "base_link")]
    assert mine["length_m"] == pytest.approx(10.0)
    assert other["length_m"] == pytest.approx(10.0)
    assert other["start_x"] == pytest.approx(1000.0)


def _scans(plan):
    """Every node of an ``EXPLAIN (FORMAT JSON)`` plan that reads a table."""
    if "Relation Name" in plan:
        yield plan
    for child in plan.get("Plans", []):
        yield from _scans(child)


def _conditions(node) -> str:
    """Every condition a scan applies, its index children included.

    A bitmap scan carries the predicate as ``Recheck Cond`` and on its ``Bitmap Index Scan``
    child rather than as a ``Filter``; which form the planner picks depends on the row count.
    """
    own = " ".join(str(node.get(k, "")) for k in ("Filter", "Index Cond", "Recheck Cond"))
    return " ".join([own, *(_conditions(child) for child in node.get("Plans", []))])


@pg
def test_the_campaign_predicate_reaches_every_table_underneath(index):
    """A scoped read filters each pose table, rather than windowing the corpus first.

    The index holds every campaign's poses, so a predicate applied above the window would
    make every read of one run pay for all of them.
    """
    from robovast.results_processing import index_query

    with index_query.open_index(readonly=True) as conn:
        plan = conn.execute(
            "EXPLAIN (FORMAT JSON) SELECT * FROM pose_track_view "
            f"WHERE campaign_id = '{CAMPAIGN}'").fetchone()[0]
    if isinstance(plan, str):
        plan = json.loads(plan)
    scans = [n for n in _scans(plan[0]["Plan"]) if n["Relation Name"] in ("poses", "sim_poses")]
    assert {n["Relation Name"] for n in scans} == {"poses", "sim_poses"}
    for node in scans:
        assert CAMPAIGN in _conditions(node), (node["Relation Name"], _conditions(node))


@pg
def test_a_pose_table_missing_a_column_the_view_names_is_left_out(index):
    """A branch naming a column its table lacks raises on CREATE VIEW, and the rebuild catches
    that -- so without the guard one odd table takes the view away from every campaign."""
    from robovast.results_processing import index_query, index_views

    with index_query.open_index(readonly=False) as conn:
        # A pose table in every respect the view tests for -- clock, frame, both positions,
        # run_id -- and without the context columns every branch also selects.
        conn.execute('CREATE TABLE odd_poses ("position.x" double precision, '
                     '"position.y" double precision, frame text, run_id integer, '
                     'timestamp double precision)')
        created = index_views.create_views(conn)

    assert "pose_track_view" in created, "the view survives a table it cannot read"
    assert _tracks(), "and still reports the tracks of the tables it can"
