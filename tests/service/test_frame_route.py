# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A run's camera frames: ``GET /data/campaigns/{id}/frame``, ``.../frame-index`` and the
``frame`` events of the live stream.

A finished run answers from an index built over its recording on first request and kept;
a live run from the watcher tapping the recording as it grows. What is not there -- a
run without the topic, a topic with no frame yet -- is a ``404`` that says so.
"""

import base64
import io
import json
import shutil
import threading
import time

import pytest
import yaml
from fastapi.testclient import TestClient
from PIL import Image as PILImage

from robovast.service import data_app
from robovast.service.app import build_app
from tests.service.null_service import NullService
from robovast.service.data_app import build_data_app
from robovast.service.interface import Routes
from robovast.service.live import LiveCampaigns
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore
from tests.robovast_decode.conftest import NAV_CONFIG, make_campaign
from tests.robovast_decode.test_frames import (COMPRESSED, TOPIC, closed_bag, compressed,
                                               fixture_span, image_bag, jpeg)
from tests.robovast_decode.test_live import GrowingBag, cuts

from .conftest import TEST_TOKEN
from .test_live_route import _events

_CAMPAIGN = "nav-2026-01-01-000000"
_RUN = "cfg/0"


@pytest.fixture(name="root")
def _root(tmp_path):
    root = tmp_path / "results"
    root.mkdir()
    yield root
    LiveCampaigns.for_root(root).stop()


def _frames(n: int):
    """``[(stamp ns, shade)]`` of *n* frames a second apart, from the fixture's start."""
    t0, _ = fixture_span()
    return [(t0 + 10**9 * k, 30 * k) for k in range(1, n + 1)]


def _bag_data(frames):
    return image_bag({(TOPIC, COMPRESSED): [(t, compressed(t, jpeg((s, s, s))))
                                            for t, s in frames]})


def _finished_run(root, frames, campaign_id=_CAMPAIGN):
    """A finished campaign whose one run's recording carries the camera topic."""
    campaign = make_campaign(root / campaign_id)
    shutil.rmtree(campaign / "cfg" / "0" / "rosbag2")
    closed_bag(campaign / "cfg" / "0" / "rosbag2", _bag_data(frames))
    return campaign


def _open_run(root, frames, campaign_id=_CAMPAIGN):
    """A campaign whose one run has no verdict and an empty recording directory."""
    campaign = make_campaign(root / campaign_id, verdict=False)
    shutil.rmtree(campaign / "cfg" / "0" / "rosbag2")
    (campaign / "_execution").mkdir()
    (campaign / "_execution" / "tables.yaml").write_text(yaml.safe_dump(NAV_CONFIG))
    return campaign, GrowingBag(campaign / "cfg" / "0", data=_bag_data(frames))


def _shade(data: bytes) -> int:
    image = PILImage.open(io.BytesIO(data))
    image.load()
    return image.getpixel((5, 5))[0]


@pytest.fixture(name="mounted")
def _mounted(root, tmp_path):
    """``vast serve``'s one app, with the data routes mounted."""
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


def _get_frame(client, t=None, campaign_id=_CAMPAIGN, run=_RUN, topic=TOPIC):
    params = {"run": run, "topic": topic}
    if t is not None:
        params["t"] = repr(t)
    return client.get(Routes.campaign_frame(campaign_id), params=params)


# -- a finished run -----------------------------------------------------------------------

def test_a_finished_run_serves_the_nearest_frame_with_its_stamp(client, root):
    frames = _frames(5)
    _finished_run(root, frames)
    stamps = [t / 1e9 for t, _ in frames]

    response = _get_frame(client)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "image/jpeg"
    assert float(response.headers["x-frame-time"]) == stamps[-1], "no t: the newest"
    assert _shade(response.content) == pytest.approx(frames[-1][1], abs=4)

    response = _get_frame(client, t=stamps[1] + 0.4)
    assert float(response.headers["x-frame-time"]) == stamps[1], "last at or before t"
    assert _shade(response.content) == pytest.approx(frames[1][1], abs=4)

    response = _get_frame(client, t=stamps[0] - 5)
    assert float(response.headers["x-frame-time"]) == stamps[0], "before the first: the first"
    assert LiveCampaigns.for_root(root).active() == [], "a finished run starts no watcher"


def test_the_index_route_lists_every_stamp(client, root):
    frames = _frames(4)
    _finished_run(root, frames)
    response = client.get(Routes.campaign_frame_index(_CAMPAIGN),
                          params={"run": _RUN, "topic": TOPIC})
    assert response.status_code == 200, response.text
    assert response.json() == {"topic": TOPIC, "times": [t / 1e9 for t, _ in frames]}


@pytest.mark.parametrize("campaign_id, run, topic, status, said", [
    ("nav-2026-09-09-000000", _RUN, TOPIC, 404, "no campaign"),
    (_CAMPAIGN, "cfg/7", TOPIC, 404, "no run 'cfg/7'"),
    (_CAMPAIGN, _RUN, "/no/such/camera", 404, "recorded no topic /no/such/camera"),
    (_CAMPAIGN, _RUN, "/scan", 404, "not an image"),
    (_CAMPAIGN, "cfg", TOPIC, 400, "<config>/<run_id>"),
])
def test_what_is_not_there_says_so(standalone, root, campaign_id, run, topic, status, said):
    _finished_run(root, _frames(2))
    for route in (Routes.campaign_frame, Routes.campaign_frame_index):
        response = standalone.get(route(campaign_id), params={"run": run, "topic": topic})
        assert response.status_code == status, response.text
        assert said in response.json()["detail"]


def test_an_index_is_built_once_and_kept_per_run_and_topic(standalone, root, monkeypatch):
    _finished_run(root, _frames(3))
    monkeypatch.setattr(data_app, "FRAME_INDEXES", 1)
    monkeypatch.setattr(data_app, "_indexes", data_app.collections.OrderedDict())
    assert _get_frame(standalone).status_code == 200
    (key,) = list(data_app._indexes)  # pylint: disable=protected-access
    index = data_app._indexes[key]  # pylint: disable=protected-access
    assert key[1:] == (_RUN, TOPIC)
    assert _get_frame(standalone, t=0).status_code == 200
    assert data_app._indexes[key] is index, "the second request reads the kept index"  # pylint: disable=protected-access
    _finished_run(root, _frames(2), campaign_id="nav-2026-01-01-000001")
    assert _get_frame(standalone, campaign_id="nav-2026-01-01-000001").status_code == 200
    assert list(data_app._indexes) == [(str(root / "nav-2026-01-01-000001"), _RUN, TOPIC)], \
        "bounded: the least recently asked for is let go"  # pylint: disable=protected-access


# -- a live run ---------------------------------------------------------------------------

def test_a_live_run_answers_from_the_watcher_as_the_recording_grows(standalone, root):
    frames = _frames(6)
    campaign, bag = _open_run(root, frames)
    response = _get_frame(standalone)
    assert response.status_code == 404 and "yet" in response.json()["detail"]
    assert LiveCampaigns.for_root(root).active() == [_CAMPAIGN], "the watcher follows it"

    bag.grow(cuts(bag.data, 2)[0])
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        response = _get_frame(standalone)
        if response.status_code == 200:
            break
        time.sleep(0.1)
    assert response.status_code == 200, response.text
    first_seen = float(response.headers["x-frame-time"])
    assert first_seen in [t / 1e9 for t, _ in frames]

    bag.close()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        response = standalone.get(Routes.campaign_frame_index(_CAMPAIGN),
                                  params={"run": _RUN, "topic": TOPIC})
        if len(response.json()["times"]) == len(frames):
            break
        time.sleep(0.1)
    assert response.json()["times"] == [t / 1e9 for t, _ in frames]
    response = _get_frame(standalone, t=frames[2][0] / 1e9 + 0.1)
    assert float(response.headers["x-frame-time"]) == frames[2][0] / 1e9
    assert _shade(response.content) == pytest.approx(frames[2][1], abs=4)
    assert _get_frame(standalone, topic="/no/such/camera").status_code == 404

    (campaign / "cfg" / "0" / "test.xml").write_text("<testsuite/>")
    response = _get_frame(standalone)
    assert response.status_code == 200, "finished now: the index over the closed recording"
    assert float(response.headers["x-frame-time"]) == frames[-1][0] / 1e9


def test_the_live_stream_carries_the_newest_frame_as_it_changes(root, monkeypatch):
    monkeypatch.setattr(data_app, "LIVE_HEARTBEAT_S", 0.5)
    frames = _frames(8)
    campaign, bag = _open_run(root, frames)

    def grow():
        for cut in cuts(bag.data, 8):
            bag.grow(cut)
            time.sleep(0.4)
        bag.close()
        time.sleep(0.3)
        (campaign / "cfg" / "0" / "test.xml").write_text("<testsuite/>")

    grower = threading.Thread(target=grow, daemon=True)
    grower.start()
    with TestClient(build_data_app(root, TEST_TOKEN)) as client:
        with client.stream("GET", Routes.campaign_live(_CAMPAIGN),
                           params={"run": _RUN, "tables": "rosbag2_collision",
                                   "frames": TOPIC}) as response:
            assert response.status_code == 200, response.text
            seen = list(_events(response.iter_lines(), 30.0))
    grower.join(5)
    kinds = [event for event, _ in seen]
    assert kinds[-1] == "eof" and "streamerror" not in kinds, kinds
    assert "batch" in kinds, "the tables still come"
    events = [json.loads(data) for event, data in seen if event == "frame"]
    assert len(events) >= 2, "the newest frame is sent as it changes"
    assert all(e["topic"] == TOPIC for e in events)
    stamps = [e["t"] for e in events]
    assert stamps == sorted(stamps) and len(set(stamps)) == len(stamps), "only when it changed"
    assert stamps[-1] == frames[-1][0] / 1e9
    for event in events:
        shade = _shade(base64.b64decode(event["jpeg_base64"]))
        expected = next(s for t, s in frames if t / 1e9 == event["t"])
        assert shade == pytest.approx(expected, abs=4)


def test_the_frame_routes_are_registered_on_both_apps(root, tmp_path):
    """Served by the data container and by ``vast serve``'s one app, at the same paths."""
    paths = [Routes.campaign_frame("{campaign_id}"), Routes.campaign_frame_index("{campaign_id}")]
    data = build_data_app(root, TEST_TOKEN).openapi()["paths"]
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    app = build_app(NullService(store=store), mount_mcp=False).openapi()["paths"]
    for path in paths:
        assert list(data[path]) == ["get"] and list(app[path]) == ["get"]
    assert "frames" in json.dumps(app[Routes.campaign_live("{campaign_id}")]["get"]["parameters"])
