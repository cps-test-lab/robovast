# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Bulk data and typed rows over the wire: a whole frame, a point cloud as Arrow, and a
query answered as an Arrow stream with the caller's own tables registered.

The properties: ``GET /data/campaigns/{id}/frame?full=1`` hands over what the camera produced
(a compressed image as recorded, a raw one as its pixels in ``.npy``), ``.../points`` one cloud
as one column per field and steps through a topic with ``after``, and
``POST /campaigns/{id}/query.arrow`` streams rows in their types -- a list column as a list --
over the campaign's tables and the tables the request carries.
"""

import base64
import io
import json
import shutil

import numpy as np
import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from robovast.service.app import build_app
from robovast.service.data_app import build_data_app
from robovast.service.interface import Routes
from robovast.service.live import LiveCampaigns
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore
from tests.robovast_data.conftest import nav_campaign
from tests.robovast_decode.test_bulk import CLOUD, CLOUD_TOPIC, _cloud
from tests.robovast_decode.test_frames import (COMPRESSED, RAW, RAW_TOPIC, TOPIC, closed_bag,
                                               compressed, fixture_span, image_bag, jpeg, raw)
from tests.service.null_service import NullService

from .conftest import TEST_TOKEN

CID = "nav-2026-01-01-000000"
RUN = "cfg/0"
DEPTH = np.linspace(0, 4000, 32 * 24, dtype="<u2").reshape(24, 32)
XYZ = np.array([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0], [np.nan, 0.0, 0.0]], dtype=np.float32)


def campaign_with_bulk(root):
    """The fixture campaign at *root*/CID, its run recording a camera, a depth camera and one
    lidar cloud at the second of four frames; the frames' stamps in ns."""
    campaign = nav_campaign(root / CID)
    t0, _ = fixture_span()
    stamps = [t0 + 10**9 * k for k in range(1, 5)]
    shutil.rmtree(campaign / "cfg" / "0" / "rosbag2")
    closed_bag(campaign / "cfg" / "0" / "rosbag2", image_bag({
        (TOPIC, COMPRESSED): [(t, compressed(t, jpeg((40 * k, 0, 0)))) for k, t in enumerate(stamps)],
        (RAW_TOPIC, RAW): [(t, raw(t, DEPTH, "16UC1")) for t in stamps],
        (CLOUD_TOPIC, CLOUD): [(stamps[1], _cloud(stamps[1], XYZ, np.array([1, 2, 3], dtype=np.uint16)))],
    }, chunked=True))
    return stamps


@pytest.fixture(name="root")
def _root(tmp_path):
    root = tmp_path / "results"
    root.mkdir()
    yield root
    LiveCampaigns.for_root(root).stop()


@pytest.fixture(name="stamps")
def _stamps(root):
    return campaign_with_bulk(root)


@pytest.fixture(name="mounted")
def _mounted(root, tmp_path):
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    lt = NullService(store=store)
    lt._campaigns_root = lambda: root  # pylint: disable=protected-access
    with TestClient(build_app(lt, mount_mcp=False)) as client:
        yield client


@pytest.fixture(name="standalone")
def _standalone(root):
    with TestClient(build_data_app(root, TEST_TOKEN)) as client:
        yield client


@pytest.fixture(params=["standalone", "mounted"])
def client(request):
    return request.getfixturevalue(request.param)


def test_a_whole_frame_is_the_recorded_bytes_or_the_pixels(client, stamps):
    response = client.get(Routes.campaign_frame(CID),
                          params={"run": RUN, "topic": TOPIC, "full": "1", "t": repr(stamps[2] / 1e9)})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "image/jpeg"
    assert float(response.headers["x-frame-time"]) == stamps[2] / 1e9
    assert response.headers["x-frame-encoding"] == "jpeg"
    assert response.headers["x-frame-id"] == "camera"
    assert response.content == compressed_payload(stamps[2], 80)

    response = client.get(Routes.campaign_frame(CID),
                          params={"run": RUN, "topic": RAW_TOPIC, "full": "1"})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/x-npy")
    assert response.headers["x-frame-encoding"] == "16UC1"
    pixels = np.load(io.BytesIO(response.content), allow_pickle=False)
    assert pixels.dtype == np.uint16 and np.array_equal(pixels, DEPTH)

    preview = client.get(Routes.campaign_frame(CID), params={"run": RUN, "topic": RAW_TOPIC})
    assert preview.headers["content-type"] == "image/jpeg", "without full: the preview"


def compressed_payload(t_ns: int, shade: int) -> bytes:
    return jpeg((shade, 0, 0))


def test_a_point_cloud_is_one_column_per_field_and_steps_with_after(client, stamps):
    def points(**params):
        return client.get(Routes.campaign_points(CID), params={"run": RUN, "topic": CLOUD_TOPIC,
                                                               **params})

    response = points(t=repr(stamps[3] / 1e9))
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/vnd.apache.arrow.stream"
    assert float(response.headers["x-frame-time"]) == stamps[1] / 1e9
    assert response.headers["x-frame-id"] == "lidar"
    table = pa.ipc.open_stream(pa.py_buffer(response.content)).read_all()
    assert table.column_names == ["x", "y", "z", "intensity"]
    assert table.column("intensity").to_pylist() == [1, 2, 3]
    assert table.column("x").type == pa.float32()

    assert float(points(after="1").headers["x-frame-time"]) == stamps[1] / 1e9, "the first"
    stepped = points(after="1", t=repr(stamps[1] / 1e9))
    assert stepped.status_code == 404 and "after" in stepped.json()["detail"]
    assert points(topic=TOPIC).status_code == 404
    assert "not a point cloud" in points(topic=TOPIC).json()["detail"]
    assert points(run="cfg/9").status_code == 404


def _arrow(response) -> pa.Table:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/vnd.apache.arrow.stream"
    return pa.ipc.open_stream(pa.py_buffer(response.content)).read_all()


def test_the_arrow_query_streams_typed_rows_with_the_callers_tables(mounted, stamps):
    scan = _arrow(mounted.post(Routes.campaign_query_arrow(CID),
                               json={"sql": "SELECT timestamp, ranges FROM rosbag2_scan"}))
    ranges = scan.schema.field("ranges").type
    assert pa.types.is_list(ranges) and ranges.value_type == pa.float32()
    assert scan.num_rows > 0 and len(scan.column("ranges")[0].as_py()) == 16
    assert json.loads(scan.schema.metadata[b"problems"]) == []

    detections = pa.table({"timestamp": pa.array([s / 1e9 for s in stamps], pa.float64()),
                           "seen": pa.array([0, 0, 1, 1], pa.int64())})
    out = io.BytesIO()
    with pa.ipc.new_stream(out, detections.schema) as writer:
        writer.write_table(detections)
    joined = _arrow(mounted.post(Routes.campaign_query_arrow(CID), json={
        "sql": """SELECT d.timestamp, d.seen, p."position.x" AS x FROM detections d
                  ASOF JOIN (SELECT * FROM poses WHERE frame = 'base_link') p
                  ON p.timestamp <= d.timestamp""",
        "tables": {"detections": base64.b64encode(out.getvalue()).decode("ascii")}}))
    # The first frame precedes every pose and has no match; the rest join.
    assert joined.num_rows == 3 and joined.column("seen").to_pylist() == [0, 1, 1]

    refused = mounted.post(Routes.campaign_query_arrow(CID), json={"sql": "DROP TABLE poses"})
    assert refused.status_code == 400
    garbage = mounted.post(Routes.campaign_query_arrow(CID),
                           json={"sql": "SELECT 1", "tables": {"x": "bm90IGFycm93"}})
    assert garbage.status_code == 400 and "Arrow" in garbage.json()["detail"]
