# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A run's images and point clouds are read from its recording as arrays, through the same
``Campaign`` that answers its tables; a copy without the recording says so."""

import json
import shutil

import numpy as np
import pandas as pd
import pytest

from robovast_data import Campaign, Frame, PointCloud
from tests.robovast_decode.test_bulk import CLOUD, CLOUD_TOPIC, _cloud
from tests.robovast_decode.test_frames import (COMPRESSED, RAW, RAW_TOPIC, TOPIC, closed_bag,
                                               compressed, fixture_span, image_bag, jpeg, raw)

from .conftest import nav_campaign


def _campaign_with_bulk(tmp_path):
    """The fixture campaign, its run's recording carrying a camera, a depth camera and a
    lidar: four frames a second apart, one cloud at the second frame."""
    root = nav_campaign(tmp_path / "nav-2026-01-01-00000000")
    t0, _ = fixture_span()
    stamps = [t0 + 10**9 * k for k in range(1, 5)]
    depth = np.linspace(0, 4000, 32 * 24, dtype="<u2").reshape(24, 32)
    xyz = np.array([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0], [np.nan, 0.0, 0.0]], dtype=np.float32)
    shutil.rmtree(root / "cfg" / "0" / "rosbag2")
    closed_bag(root / "cfg" / "0" / "rosbag2", image_bag({
        (TOPIC, COMPRESSED): [(t, compressed(t, jpeg((40 * k, 0, 0)))) for k, t in enumerate(stamps)],
        (RAW_TOPIC, RAW): [(t, raw(t, depth, "16UC1")) for t in stamps],
        (CLOUD_TOPIC, CLOUD): [(stamps[1], _cloud(stamps[1], xyz, np.array([1, 2, 3], dtype=np.uint16)))],
    }, chunked=True))
    return root, stamps


def test_frames_of_a_run_are_arrays_in_stamp_order(tmp_path):
    root, stamps = _campaign_with_bulk(tmp_path)
    c = Campaign(str(root), workers=1)
    got = list(c.frames("cfg", 0, TOPIC))
    assert [f.t_ns for f in got] == stamps
    assert isinstance(got[0], Frame) and got[0].t == pytest.approx(stamps[0] / 1e9)
    assert got[0].image.shape == (24, 32, 3) and got[0].encoding == "rgb8"
    assert got[0].frame_id == "camera" and got[0].topic == TOPIC
    assert got[3].image[0, 0, 0] > got[0].image[0, 0, 0], "frames in recording order"
    assert got[0].pil().size == (32, 24)

    depth = list(c.frames("cfg", 0, RAW_TOPIC, every=2.0))
    assert [f.t_ns for f in depth] == stamps[0::2]
    assert depth[0].image.dtype == np.uint16 and depth[0].image.shape == (24, 32)
    assert depth[0].encoding == "16UC1"
    assert [f.t_ns for f in c.frames("cfg", 0, TOPIC, start=stamps[1] / 1e9,
                                     end=stamps[2] / 1e9)] == stamps[1:3]


def test_the_frame_at_a_moment_is_the_last_at_or_before_it(tmp_path):
    root, stamps = _campaign_with_bulk(tmp_path)
    c = Campaign(str(root), workers=1)
    assert c.frame("cfg", 0, TOPIC, t=stamps[1] / 1e9 + 0.5).t_ns == stamps[1]
    assert c.frame("cfg", 0, TOPIC).t_ns == stamps[-1]
    assert c.frame("cfg", 0, TOPIC, t=0.0).t_ns == stamps[0]
    with pytest.raises(KeyError, match="/nowhere"):
        c.frame("cfg", 0, "/nowhere")


def test_point_clouds_are_one_array_per_field(tmp_path):
    root, stamps = _campaign_with_bulk(tmp_path)
    c = Campaign(str(root), workers=1)
    cloud = c.pointcloud("cfg", 0, CLOUD_TOPIC, t=stamps[2] / 1e9)
    assert isinstance(cloud, PointCloud) and cloud.t_ns == stamps[1] and cloud.frame_id == "lidar"
    assert cloud.xyz.shape == (2, 3) and len(cloud) == 3
    assert np.array_equal(cloud.fields["intensity"], [1, 2, 3])
    assert c.pointcloud("cfg", 0, CLOUD_TOPIC, keep_nan=True).xyz.shape == (3, 3)
    assert [p.t_ns for p in c.pointclouds("cfg", 0, CLOUD_TOPIC)] == [stamps[1]]


def test_what_is_not_there_is_said(tmp_path):
    root, _ = _campaign_with_bulk(tmp_path)
    c = Campaign(str(root), workers=1)
    with pytest.raises(ValueError, match="not an image"):
        list(c.frames("cfg", 0, "/collision"))
    with pytest.raises(ValueError, match="not a point cloud"):
        c.pointcloud("cfg", 0, TOPIC)
    with pytest.raises(KeyError, match="cfg/7"):
        c.frame("cfg", 7, TOPIC)
    shutil.rmtree(root / "cfg" / "0" / "rosbag2")
    with pytest.raises(FileNotFoundError, match="vast campaign download"):
        c.frames("cfg", 0, TOPIC)


def test_an_export_lists_the_tables_it_carries_and_has_no_recording(tmp_path):
    root, _ = _campaign_with_bulk(tmp_path)
    poses = Campaign(str(root), workers=1).table("poses")
    export = tmp_path / "export"
    (export / "tables").mkdir(parents=True)
    poses.to_parquet(export / "tables" / "poses.parquet")
    shutil.copytree(root, export / root.name,
                    ignore=shutil.ignore_patterns("rosbag2*", ".cache", "rosout_bag"))
    (export / "export.json").write_text(json.dumps({
        "campaign_id": root.name, "export_id": "0123456789ab", "decoder": "test",
        "request": {"format": "parquet", "bags": "none", "records": True},
        "tables": {"poses": {"rows": len(poses), "file": "tables/poses.parquet"}}}))
    e = Campaign(str(export), workers=1)
    listed = e.tables.set_index("name")
    assert listed.loc["poses", "runs"] == 1 and listed.loc["poses", "built"] == 1
    assert len(e.table("poses", config="cfg", run=0)) == len(poses)
    with pytest.raises(FileNotFoundError, match="--bags mcap"):
        e.frame("cfg", 0, TOPIC)


def test_a_frame_loops_result_joins_the_tables_by_stamp(tmp_path):
    root, stamps = _campaign_with_bulk(tmp_path)
    c = Campaign(str(root), workers=1)
    detections = pd.DataFrame([(f.t, int(f.image[0, 0, 0] > 100)) for f in c.frames("cfg", 0, TOPIC)],
                              columns=["timestamp", "seen"])
    joined = c.sql("""SELECT d.timestamp, d.seen, p."position.x" AS x FROM detections d
                      ASOF JOIN (SELECT * FROM poses WHERE frame = 'base_link') p
                      ON p.timestamp <= d.timestamp""", tables={"detections": detections})
    # The first frame precedes every pose, so it has no match; the rest join.
    assert len(joined) == len(stamps) - 1 and joined.x.notna().all()
    assert joined.seen.tolist() == [0, 0, 1]
