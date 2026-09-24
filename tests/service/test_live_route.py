# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A run's tables as it records: ``GET /data/campaigns/{id}/live`` and the watchers behind it.

The stream is server-sent events over one campaign watcher per campaign
(:mod:`robovast.service.live`), driven by inotify in the data-plane process. The properties:
batches arrive as the recording grows and add up to a whole build, ``eof`` follows the run's
verdict, a run that is not live gets ``eof`` at once, what is not here is a ``streamerror``,
a reader that falls behind is dropped with the reason rather than buffered, and a watcher
nobody reads is stopped once its campaign is over.
"""

import json
import shutil
import threading
import time

import pytest
import yaml
from fastapi.testclient import TestClient

from robovast.service import data_app
from robovast.service import live as service_live
from robovast.service.app import build_app
from tests.service.null_service import NullService
from robovast.service.data_app import build_data_app
from robovast.service.interface import Routes
from robovast.service.live import Dropped, LiveCampaigns
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore
from tests.robovast_decode.conftest import NAV_CONFIG, make_campaign
from tests.robovast_decode.test_live import GrowingBag, cuts, reference

from .conftest import TEST_TOKEN

_CAMPAIGN = "nav-2026-01-01-000000"
_RUN = "cfg/0"
_TABLES = "poses,rosbag2_collision"


@pytest.fixture(name="root")
def _root(tmp_path):
    root = tmp_path / "results"
    root.mkdir()
    yield root
    LiveCampaigns.for_root(root).stop()


def _open_run(root, campaign_id=_CAMPAIGN):
    """A campaign whose one run has no verdict and an empty recording directory, with the
    fixture's decoder configuration so the watcher follows the tables a whole build gives."""
    campaign = make_campaign(root / campaign_id, verdict=False)
    shutil.rmtree(campaign / "cfg" / "0" / "rosbag2")
    (campaign / "_execution").mkdir()
    (campaign / "_execution" / "tables.yaml").write_text(yaml.safe_dump(NAV_CONFIG))
    return campaign, GrowingBag(campaign / "cfg" / "0")


def _events(lines, deadline_s: float):
    """``(event, data)`` of every SSE frame with data, up to ``eof`` or the deadline."""
    deadline = time.monotonic() + deadline_s
    event, data = "message", []
    for line in lines:
        line = line.rstrip("\r\n")
        if line == "":
            if data:
                yield event, "\n".join(data)
            if event == "eof" or time.monotonic() > deadline:
                return
            event, data = "message", []
        elif line.startswith("event:"):
            event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data.append(line[len("data:"):].strip())


def _stream(client, campaign_id=_CAMPAIGN, run=_RUN, tables=_TABLES, deadline_s=30.0):
    """The stream's events. The test client hands the response back once the app's
    generator has ended, so what drives the run to its end must already be under way."""
    with client.stream("GET", Routes.campaign_live(campaign_id),
                       params={"run": run, "tables": tables}) as response:
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["x-accel-buffering"] == "no"
        return list(_events(response.iter_lines(), deadline_s))


def test_batches_arrive_as_the_bag_grows_and_eof_follows_the_verdict(root, tmp_path,
                                                                     monkeypatch):
    monkeypatch.setattr(data_app, "LIVE_HEARTBEAT_S", 0.2)
    monkeypatch.setattr(data_app, "LIVE_FRAME_ROWS", 50)
    campaign, bag = _open_run(root)

    def grow():
        for cut in cuts(bag.data, 6):
            bag.grow(cut)
            time.sleep(0.3)
        bag.close()
        time.sleep(0.3)
        (campaign / "cfg" / "0" / "test.xml").write_text("<testsuite/>")

    grower = threading.Thread(target=grow, daemon=True)
    grower.start()
    with TestClient(build_data_app(root, TEST_TOKEN)) as client:
        seen = _stream(client)
    grower.join(5)
    kinds = [event for event, _ in seen]
    assert kinds[-1] == "eof", kinds
    assert "streamerror" not in kinds
    assert "heartbeat" in kinds, "a quiet stretch is a heartbeat the client can see"
    batches = [json.loads(data) for event, data in seen if event == "batch"]
    assert {b["table"] for b in batches} == {"poses", "rosbag2_collision"}
    assert max(len(b["rows"]) for b in batches) == 50, "a big batch is several frames"
    expected = reference(tmp_path, ["poses", "rosbag2_collision"])
    for table, rows in expected.items():
        streamed = sum(len(b["rows"]) for b in batches if b["table"] == table)
        assert streamed == rows.num_rows, table
    row = next(b["rows"][0] for b in batches if b["table"] == "poses")
    assert {"campaign_id", "config_name", "run_id", "frame", "position.x"} <= set(row)
    assert row["campaign_id"] == _CAMPAIGN


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


def test_a_finished_run_gets_eof_at_once_and_starts_no_watcher(client, root):
    make_campaign(root / _CAMPAIGN)
    assert _stream(client) == [("eof", "{}")]
    assert LiveCampaigns.for_root(root).active() == []


def test_a_run_of_a_finished_campaign_is_not_live_either(standalone, root):
    campaign, _ = _open_run(root)
    (campaign / "_execution" / "outcome.json").write_text("{}")
    assert _stream(standalone) == [("eof", "{}")]
    assert LiveCampaigns.for_root(root).active() == []


@pytest.mark.parametrize("campaign_id, run, tables, said", [
    ("nav-2026-09-09-000000", _RUN, _TABLES, "no campaign"),
    (_CAMPAIGN, "cfg/7", _TABLES, "no run 'cfg/7'"),
    (_CAMPAIGN, "cfg", _TABLES, "<config>/<run_id>"),
    (_CAMPAIGN, _RUN, "", "at least one table"),
    (_CAMPAIGN, _RUN, "poses,no such", "not table names"),
])
def test_what_is_not_here_is_a_streamerror_then_eof(standalone, root, campaign_id, run,
                                                    tables, said):
    _open_run(root)
    seen = _stream(standalone, campaign_id, run, tables)
    assert [event for event, _ in seen] == ["streamerror", "eof"]
    assert said in json.loads(seen[0][1])
    assert LiveCampaigns.for_root(root).active() == []


def test_a_subscriber_that_falls_behind_is_dropped_with_the_reason(root, monkeypatch):
    monkeypatch.setattr(service_live, "QUEUE_MAX", 1)
    _, bag = _open_run(root)
    live = LiveCampaigns.for_root(root)
    subscription = live.subscribe(_CAMPAIGN, _RUN, ["poses", "rosbag2_collision"])
    for cut in cuts(bag.data, 6):
        bag.grow(cut)
        time.sleep(0.2)
    deadline = time.monotonic() + 10
    while subscription.dropped is None and time.monotonic() < deadline:
        time.sleep(0.1)
    with pytest.raises(Dropped, match="fell 1 batches behind"):
        subscription.next(0.1)
    with pytest.raises(Dropped):
        subscription.next(0.1)
    assert live.active() == [_CAMPAIGN], "the watcher stays for the next reader"


def test_a_watcher_nobody_reads_is_stopped_once_its_campaign_is_over(root, monkeypatch):
    campaign, _ = _open_run(root)
    live = LiveCampaigns.for_root(root)
    subscription = live.subscribe(_CAMPAIGN, _RUN, ["rosbag2_collision"])
    assert live.active() == [_CAMPAIGN]
    assert any(t.name == f"live:{_CAMPAIGN}" for t in threading.enumerate())
    monkeypatch.setattr(service_live, "IDLE_S", 0.0)
    assert live.reap() == [], "a watcher with a subscriber is never reaped"
    subscription.close()
    assert live.reap() == [], "an idle watcher of a running campaign is kept warm"
    (campaign / "_execution" / "outcome.json").write_text("{}")
    assert live.reap() == [_CAMPAIGN]
    assert live.active() == []
    assert not any(t.name == f"live:{_CAMPAIGN}" for t in threading.enumerate())


def test_a_watcher_whose_campaign_directory_is_gone_is_reaped_too(root, monkeypatch):
    campaign, _ = _open_run(root)
    live = LiveCampaigns.for_root(root)
    live.subscribe(_CAMPAIGN, _RUN, ["rosbag2_collision"]).close()
    monkeypatch.setattr(service_live, "IDLE_S", 0.0)
    shutil.rmtree(campaign)
    assert live.reap() == [_CAMPAIGN]


def test_the_live_route_is_registered_on_both_apps(root, tmp_path):
    """Served by the data container and by ``vast serve``'s one app, at the same path."""
    path = Routes.campaign_live("{campaign_id}")
    assert list(build_data_app(root, TEST_TOKEN).openapi()["paths"][path]) == ["get"]
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    app = build_app(NullService(store=store), mount_mcp=False)
    assert list(app.openapi()["paths"][path]) == ["get"]
