# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``get_track_deviation`` -- a recorded track against a path its configuration contributed.

The track is read in full from the index, so these pin that the answer covers the whole
recording (not a reply-sized prefix of it), that a floor-plan path is compared in the plane,
that the geometry is the nearest point of any segment, and that each way of not having an
answer names what does exist.
"""

import os

import pytest
import yaml

from robovast.results_processing.track_deviation import choose_path

DSN = os.environ.get("ROBOVAST_TEST_PG_DSN")
pg = pytest.mark.skipif(not DSN, reason="ROBOVAST_TEST_PG_DSN is not set")

SCHEMA = "track_deviation_test"
CAMPAIGN = "camp-deviation-2026-09-21-120000"

#: More poses than fit in one capped SQL reply: a figure over a prefix would differ.
_POSES = 3000


def _campaign(results):
    """A path-variation configuration whose planned path is the x axis from 0 to 30 m.

    The robot drives it 0.5 m to the side for the first two thirds and 2.0 m to the side
    for the last third, at a height of 0.3 m -- so a prefix, a 3D distance to a floor-plan
    path, and the true figure are three different numbers.
    """
    root = results / CAMPAIGN
    run = root / "cfg-a" / "0"
    run.mkdir(parents=True)
    (root / "_execution").mkdir()
    (root / "_config").mkdir()
    (root / "_transient").mkdir()
    (root / "_config" / "campaign.vast").write_text(yaml.safe_dump(
        {"configuration": [{"name": "cfg", "variations": [{"PathVariationRandom": {}}]}]}))
    (root / "_transient" / "configurations.yaml").write_text(yaml.safe_dump({"configs": [{
        "name": "cfg-a", "config": {}, "_config_name": "cfg",
        "_path": [{"x": 0.0, "y": 0.0}, {"x": 30.0, "y": 0.0}]}]}))
    lines = ["timestamp,stamp,frame,position.x,position.y,position.z"]
    for i in range(_POSES):
        offset = 0.5 if i < 2 * _POSES // 3 else 2.0
        lines.append(f"{i * 0.01},{i * 0.01},base_link,{i * 30.0 / _POSES},{offset},0.3")
    (run / "poses.csv").write_text("\n".join(lines) + "\n")
    return root


@pytest.fixture(name="transport")
def _transport(monkeypatch, tmp_path):
    psycopg = pytest.importorskip("psycopg")
    pytest.importorskip("robovast_nav")
    from robovast.common import index_db
    from robovast.results_processing import campaign_ingest, index_query, index_views
    from tests.service.null_service import NullService
    with psycopg.connect(DSN, autocommit=True) as setup:
        for statement in (f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE",
                          "DROP SCHEMA IF EXISTS campaign CASCADE",
                          f"CREATE SCHEMA {SCHEMA}"):
            setup.execute(statement)
    monkeypatch.setenv(index_db.DSN_ENV, f"{DSN} options=-csearch_path={SCHEMA}")
    # The results root is derived from the workspaces store.
    monkeypatch.setenv("ROBOVAST_WORKSPACES_ROOT", str(tmp_path / "workspaces"))
    root = _campaign(tmp_path / "results")
    with index_query.open_index(readonly=False) as conn:
        campaign_ingest.ingest_campaign(conn, str(root), CAMPAIGN)
        index_views.create_views(conn)
    yield NullService()

    with psycopg.connect(DSN, autocommit=True) as teardown:
        teardown.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        teardown.execute("DROP SCHEMA IF EXISTS campaign CASCADE")


@pg
def test_the_deviation_covers_every_pose_and_ignores_height_for_a_floor_plan_path(transport):
    result = transport.get_track_deviation(CAMPAIGN, "cfg-a", 0)
    assert result.points == _POSES
    assert result.planar is True
    # Two thirds at 0.5 m, one third at 2.0 m -- in the plane, the 0.3 m height ignored.
    assert result.mean_m == pytest.approx(1.0, abs=1e-3)
    assert result.max_m == pytest.approx(2.0)
    assert result.marker_label == "planned path"
    assert result.path_length_m == pytest.approx(30.0)
    # The run drives the path's length plus the sideways move, so it is longer than the path
    # and the ratio says by how much -- the figure a reader wants beside a mean distance.
    assert result.track_length_m > result.path_length_m
    assert result.track_length_m == pytest.approx(31.5, abs=0.1)
    assert result.efficiency == pytest.approx(result.path_length_m / result.track_length_m)
    assert 0.9 < result.efficiency < 1.0


@pg
def test_an_unrecorded_frame_is_refused_with_the_recorded_ones(transport):
    with pytest.raises(KeyError, match="base_link"):
        transport.get_track_deviation(CAMPAIGN, "cfg-a", 0, frame="odom")


@pg
def test_a_table_that_is_not_a_pose_table_is_refused_with_the_ones_that_are(transport):
    with pytest.raises(ValueError, match="poses"):
        transport.get_track_deviation(CAMPAIGN, "cfg-a", 0, source="runs")


def test_one_path_is_chosen_without_a_label_and_several_are_never_guessed_between():
    a = {"kind": "path", "label": "a", "points": [[0, 0], [1, 0]]}
    b = {"kind": "path", "label": "b", "points": [[0, 0], [0, 1]]}
    goal = {"kind": "pose", "label": "goal", "pos": [1, 0]}
    assert choose_path([a, goal], None) is a
    assert choose_path([a, b], "b") is b
    with pytest.raises(ValueError, match="'a'"):
        choose_path([a, b], None)
    with pytest.raises(ValueError, match="no path marker"):
        choose_path([goal], None)
    with pytest.raises(ValueError, match="'c'"):
        choose_path([a, b], "c")


def test_the_distance_is_to_the_nearest_point_of_any_segment_in_2d_and_3d():
    import numpy as np

    from robovast.results_processing.track_deviation import _distances

    corner = np.array([[0.0, 0.0], [10.0, 0.0], [10.0, 10.0]])
    poses = np.array([[5.0, 1.0],     # beside the first segment
                      [11.0, 5.0],    # beside the second
                      [-3.0, -4.0]])  # past the start: distance to the endpoint
    assert _distances(poses, corner) == pytest.approx([1.0, 1.0, 5.0])

    rising = np.array([[0.0, 0.0, 0.0], [0.0, 0.0, 2.0]])
    assert _distances(np.array([[0.0, 1.0, 1.0]]), rising) == pytest.approx([1.0])
    # A single-point path is that point.
    assert _distances(np.array([[3.0, 4.0]]), np.array([[0.0, 0.0]])) == pytest.approx([5.0])
