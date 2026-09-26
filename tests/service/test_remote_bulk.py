# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""One interface, two homes: a campaign on a service answers the same calls, with the same
values, as the same campaign extracted on a laptop -- tables in their types, a DataFrame
joined in, frames as pixels, clouds as fields.

The requests go through the service's real routes; only the socket is replaced, by routing
the one function that opens a request into the app's test client.
"""

import urllib.error
import urllib.parse

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from robovast.service.app import build_app
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore
from robovast_data import Campaign, RemoteCampaign
from robovast_data import remote as remote_module
from tests.service.null_service import NullService

from .test_bulk_routes import CID, CLOUD_TOPIC, RAW_TOPIC, TOPIC, campaign_with_bulk

URL = f"http://robovast.example.org/campaigns/{CID}"


class _Response:
    def __init__(self, response):
        self._content = response.content
        self.headers = dict(response.headers)

    def read(self):
        return self._content

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


@pytest.fixture(name="both")
def _both(tmp_path, monkeypatch):
    """``(local, remote, stamps)``: the campaign on disk and the same one behind the routes."""
    root = tmp_path / "results"
    root.mkdir()
    stamps = campaign_with_bulk(root)
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    transport = NullService(store=store)
    transport._campaigns_root = lambda: root   # noqa: SLF001
    client = TestClient(build_app(transport, mount_mcp=False))

    def _open(request, timeout):
        del timeout
        url = urllib.parse.urlsplit(request.full_url)
        path = f"{url.path}?{url.query}" if url.query else url.path
        headers = dict(request.header_items()) or {"Authorization": "Bearer "}
        if request.data is not None:
            resp = client.post(path, content=request.data, headers=headers)
        else:
            resp = client.get(path, headers=headers)
        if resp.status_code >= 400:
            raise urllib.error.HTTPError(request.full_url, resp.status_code, "", {},
                                         _Response(resp))
        return _Response(resp)

    monkeypatch.setattr(remote_module, "_open", _open)
    return Campaign(str(root / CID), workers=1), RemoteCampaign(URL), stamps


def test_tables_arrive_in_their_types_with_lists_as_lists(both):
    local, remote, _ = both
    here = local.table("rosbag2_scan", config="cfg", run=0)
    there = remote.table("rosbag2_scan", config="cfg", run=0)
    assert list(there.columns) == list(here.columns)
    assert there.timestamp.dtype == here.timestamp.dtype
    assert np.array_equal(there.ranges.iloc[0], here.ranges.iloc[0])
    assert here.ranges.iloc[0].dtype == np.float32


def test_a_callers_dataframe_joins_the_tables_on_the_service_as_at_home(both):
    local, remote, stamps = both
    detections = pd.DataFrame({"timestamp": [s / 1e9 for s in stamps], "seen": [0, 0, 1, 1]})
    sql = """SELECT d.timestamp, d.seen, p."position.x" AS x FROM detections d
             ASOF JOIN (SELECT * FROM poses WHERE frame = 'base_link') p
             ON p.timestamp <= d.timestamp ORDER BY 1"""
    pd.testing.assert_frame_equal(remote.sql(sql, tables={"detections": detections}),
                                  local.sql(sql, tables={"detections": detections}))


def test_frames_are_the_same_pixels_at_the_same_stamps(both):
    local, remote, stamps = both
    here = list(local.frames("cfg", 0, TOPIC, every=2.0))
    there = list(remote.frames("cfg", 0, TOPIC, every=2.0))
    assert [f.t_ns for f in there] == [f.t_ns for f in here] == stamps[0::2]
    for a, b in zip(here, there):
        assert a.encoding == b.encoding and a.frame_id == b.frame_id == "camera"
        assert np.array_equal(a.image, b.image)

    depth_here = local.frame("cfg", 0, RAW_TOPIC, t=stamps[1] / 1e9 + 0.1)
    depth_there = remote.frame("cfg", 0, RAW_TOPIC, t=stamps[1] / 1e9 + 0.1)
    assert depth_there.t == depth_here.t and depth_there.encoding == "16UC1"
    assert depth_there.image.dtype == np.uint16 and np.array_equal(depth_there.image, depth_here.image)
    assert remote.frame_times("cfg", 0, TOPIC) == [s / 1e9 for s in stamps]


def test_point_clouds_are_the_same_fields(both):
    local, remote, stamps = both
    here = local.pointcloud("cfg", 0, CLOUD_TOPIC)
    there = remote.pointcloud("cfg", 0, CLOUD_TOPIC)
    assert there.t == here.t and there.frame_id == "lidar"
    assert set(there.fields) == set(here.fields)
    for name in here.fields:
        assert np.array_equal(there.fields[name], here.fields[name], equal_nan=True)
    assert there.xyz.shape == (2, 3)
    assert [p.t_ns for p in remote.pointclouds("cfg", 0, CLOUD_TOPIC)] == [stamps[1]]
    assert not list(remote.pointclouds("cfg", 0, CLOUD_TOPIC, start=stamps[2] / 1e9))
    with pytest.raises(FileNotFoundError, match="not a point cloud"):
        remote.pointcloud("cfg", 0, TOPIC)
