# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""An export: the campaign's tables as files, its records and its bags, built on request.

``POST /campaigns/{id}/exports`` starts one, ``GET .../exports/{export_id}`` says how far it
is, and the data plane serves the file once it is done. The properties: the tables are the
ones the catalog has, ``runs`` always among them; a name the catalog lacks is refused before
anything is built; a bag ships as recorded or rewritten to sqlite3 with every message; a
channel no definition covers is refused by name; a scoped token fetches the file; the export's
own record answers a status read across a restart; and the table cache keeps what an export
is reading.
"""

import io
import json
import tarfile

import pytest
import yaml
from fastapi.testclient import TestClient

from robovast.service import auth
from robovast.service.app import build_app
from robovast.service.exports import ERROR_FILE, EXPORT_FILE, REQUEST_FILE, export_dir
from robovast.service.interface import Routes
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore
from tests.service.null_service import NullService
from tests.robovast_data.conftest import nav_campaign
from tests.robovast_decode.conftest import NAV_CONFIG

from .conftest import TEST_TOKEN

_CAMPAIGN = "nav-2026-01-01-000000"
_BAG = "cfg/0/rosbag2"
_ROSOUT = "_jobs/job-0/logs/rosout_bag"


def _transport(tmp_path) -> NullService:
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    lt = NullService(store=store)
    lt._campaigns_root = lambda: tmp_path / "results"  # pylint: disable=protected-access
    return lt


@pytest.fixture(name="campaign")
def _campaign(tmp_path):
    """The decoder's fixture recording as one finished run, with its store and its decoder
    configuration, so the catalog has tables to give."""
    root = nav_campaign(tmp_path / "results" / _CAMPAIGN)
    (root / "_execution").mkdir(exist_ok=True)
    (root / "_execution" / "tables.yaml").write_text(yaml.safe_dump(NAV_CONFIG))
    (root / "_config").mkdir()
    (root / "_config" / "campaign.vast").write_text("configuration:\n  name: x\n")
    (root / "metadata.yaml").write_text("title: x\n")
    return root


@pytest.fixture(name="transport")
def _transport_fixture(tmp_path, campaign):  # pylint: disable=unused-argument
    return _transport(tmp_path)


@pytest.fixture(name="client")
def _client(transport):
    with TestClient(build_app(transport, mount_mcp=False)) as client:
        yield client


def _create(client, transport, body, campaign_id=_CAMPAIGN):
    """Start an export and wait for it; ``(export_id, status)``."""
    resp = client.post(Routes.campaign_exports(campaign_id), json=body)
    assert resp.status_code == 200, resp.text
    ref = resp.json()
    assert ref["url"] == Routes.campaign_export_download(campaign_id, ref["export_id"])
    transport._exports.wait(campaign_id, ref["export_id"], timeout=120)  # pylint: disable=protected-access
    status = client.get(Routes.campaign_export(campaign_id, ref["export_id"])).json()
    assert status["done"], status
    return ref["export_id"], status


def _download(client, export_id, campaign_id=_CAMPAIGN):
    resp = client.get(Routes.campaign_export_download(campaign_id, export_id))
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-disposition"] == \
        f'attachment; filename="{campaign_id}-export-{export_id}.tar.gz"'
    return resp.content


def _members(payload: bytes) -> dict:
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
        return {m.name: m for m in tar.getmembers()}


def _read(payload: bytes, name: str) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
        return tar.extractfile(name).read()


def _extract(payload: bytes, into):
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
        tar.extractall(into, filter="data")


# -- the tables ---------------------------------------------------------------------------------


def test_a_parquet_export_writes_every_table_and_the_records(client, transport):
    import pyarrow.parquet as pq

    export_id, status = _create(client, transport, {})
    assert status["error"] == ""
    assert status["tables"]["runs"] == 1
    assert status["tables"]["poses"] > 0
    assert status["bytes"] > 0

    payload = _download(client, export_id)
    assert len(payload) == status["bytes"]
    members = _members(payload)
    manifest = json.loads(_read(payload, "export.json"))
    assert manifest["campaign_id"] == _CAMPAIGN
    assert manifest["request"]["format"] == "parquet"
    assert manifest["tables"]["poses"]["file"] == "tables/poses.parquet"
    assert manifest["decoder"]

    poses = pq.read_table(io.BytesIO(_read(payload, "tables/poses.parquet")))
    assert poses.num_rows == status["tables"]["poses"]
    assert {"campaign_id", "config_name", "run_id"} <= set(poses.column_names)

    # The records, under the campaign's own name: the store, the frozen config, the run's
    # verdict -- and neither the recordings nor the table cache.
    assert f"{_CAMPAIGN}/campaign.db" in members
    assert f"{_CAMPAIGN}/_config/campaign.vast" in members
    assert f"{_CAMPAIGN}/cfg/0/test.xml" in members
    assert f"{_CAMPAIGN}/metadata.yaml" in members
    assert not [n for n in members if "/.cache" in n]
    assert not [n for n in members if n.startswith(f"{_CAMPAIGN}/") and "bag" in n]


def test_a_csv_export_is_the_same_rows_as_text(client, transport):
    import csv

    export_id, status = _create(client, transport, {"format": "csv", "tables": ["poses"]})
    payload = _download(client, export_id)
    assert set(status["tables"]) == {"runs", "poses"}
    rows = list(csv.DictReader(io.StringIO(_read(payload, "tables/runs.csv").decode())))
    assert [r["config_name"] for r in rows] == ["cfg"]
    poses = list(csv.reader(io.StringIO(_read(payload, "tables/poses.csv").decode())))
    assert len(poses) - 1 == status["tables"]["poses"]
    assert "tables/collision.csv" not in _members(payload)


def test_runs_is_always_written(client, transport):
    _export_id, status = _create(client, transport, {"tables": []})
    assert list(status["tables"]) == ["runs"]


def test_a_table_the_catalog_lacks_is_refused_before_anything_is_built(client, campaign):
    resp = client.post(Routes.campaign_exports(_CAMPAIGN), json={"tables": ["poses", "nope"]})
    assert resp.status_code == 400, resp.text
    assert "'nope'" in resp.json()["detail"]
    assert not (campaign / ".cache" / "tables").exists()
    assert not (campaign / ".cache" / "exports").exists()


def test_records_can_be_left_out(client, transport):
    export_id, _status = _create(client, transport, {"records": False})
    members = _members(_download(client, export_id))
    assert "tables/runs.parquet" in members
    assert not [n for n in members if n.startswith(f"{_CAMPAIGN}/")]


def test_an_unknown_campaign_or_export_is_a_404(client):
    assert client.post(Routes.campaign_exports("nope-2026-01-01-000000"),
                       json={}).status_code == 404
    assert client.get(Routes.campaign_export(_CAMPAIGN, "0123456789ab")).status_code == 404
    assert client.get(Routes.campaign_export_download(_CAMPAIGN, "0123456789ab")).status_code == 404
    # An id of a shape no export has is never looked up as a path.
    assert client.get(Routes.campaign_export(_CAMPAIGN, "not-an-id")).status_code == 404
    assert client.get(Routes.campaign_export_download(
        _CAMPAIGN, "not-an-id")).status_code == 404


def test_a_running_campaign_is_not_exported(client, transport, monkeypatch):
    monkeypatch.setattr(type(transport), "campaign_is_live", lambda self, cid: True)
    resp = client.post(Routes.campaign_exports(_CAMPAIGN), json={})
    assert resp.status_code == 409
    assert "still running" in resp.json()["detail"]


# -- the bags -----------------------------------------------------------------------------------


def test_mcap_bags_are_copied_as_recorded(client, transport, campaign):
    export_id, _status = _create(client, transport, {"bags": "mcap", "records": False})
    payload = _download(client, export_id)
    members = _members(payload)
    assert f"{_CAMPAIGN}/{_BAG}/rosbag2_0.mcap" in members
    assert f"{_CAMPAIGN}/{_BAG}/metadata.yaml" in members
    assert f"{_CAMPAIGN}/{_ROSOUT}/rosout_bag_0.mcap" in members
    assert _read(payload, f"{_CAMPAIGN}/{_BAG}/rosbag2_0.mcap") == \
        (campaign / _BAG / "rosbag2_0.mcap").read_bytes()


def test_sqlite3_bags_carry_every_message_and_topic(client, transport, campaign, tmp_path):
    from rosbags.rosbag2 import Reader

    from robovast_decode.framing import McapTail, Message

    export_id, status = _create(client, transport, {"bags": "sqlite3", "records": False})
    assert status["error"] == ""
    payload = _download(client, export_id)
    members = _members(payload)
    assert f"{_CAMPAIGN}/{_BAG}/rosbag2.db3" in members
    assert f"{_CAMPAIGN}/{_BAG}/metadata.yaml" in members
    assert f"{_CAMPAIGN}/{_ROSOUT}/rosout_bag.db3" in members
    assert not [n for n in members if n.endswith(".mcap")]

    out = tmp_path / "out"
    _extract(payload, out)
    tail = McapTail(campaign / _BAG / "rosbag2_0.mcap")
    recorded = [r for r in tail.read() if isinstance(r, Message)]
    with Reader(out / _CAMPAIGN / _BAG) as reader:
        assert reader.message_count == len(recorded)
        assert {c.topic for c in reader.connections} == \
            {c.topic for c in tail.channels.values()}
        assert sum(1 for _ in reader.messages()) == len(recorded)
        # The raw bytes, untouched: what a reader deserialises is what was recorded.
        _connection, timestamp, data = next(iter(reader.messages()))
        assert timestamp == recorded[0].log_time
        assert bytes(data) == recorded[0].data


def test_a_channel_without_a_definition_is_refused_by_topic_and_type(client, transport,
                                                                       campaign):
    from mcap.writer import Writer

    bag = campaign / "cfg" / "0" / "rosbag2"
    for file in bag.iterdir():
        file.unlink()
    with open(bag / "rosbag2_0.mcap", "wb") as fh:
        writer = Writer(fh)
        writer.start(profile="ros2", library="test")
        schema = writer.register_schema("custom_msgs/msg/Thing", "ros2msg", b"")
        channel = writer.register_channel("/thing", "cdr", schema)
        writer.add_message(channel, 1, b"\x00\x01\x00\x00", 1)
        writer.finish()

    export_id, status = _create(client, transport, {"bags": "sqlite3", "records": False})
    assert "/thing" in status["error"] and "custom_msgs/msg/Thing" in status["error"]
    assert "no message definition" in status["error"]
    resp = client.get(Routes.campaign_export_download(_CAMPAIGN, export_id))
    assert resp.status_code == 409
    assert "/thing" in resp.json()["detail"]
    assert (export_dir(campaign, export_id) / ERROR_FILE).is_file()


# -- who may fetch it ---------------------------------------------------------------------------


def test_a_token_scoped_to_the_campaign_downloads_its_export_and_nothing_else(
        client, transport):
    export_id, _status = _create(client, transport, {"tables": []})
    scoped = {"Authorization": f"Bearer {auth.scoped_token(TEST_TOKEN, auth.scope_for_campaign(_CAMPAIGN))}"}
    resp = client.get(Routes.campaign_export_download(_CAMPAIGN, export_id), headers=scoped)
    assert resp.status_code == 200, resp.text
    assert "tables/runs.parquet" in _members(resp.content)
    # The status is a control route, outside the scope.
    assert client.get(Routes.campaign_export(_CAMPAIGN, export_id),
                      headers=scoped).status_code == 403
    other = {"Authorization": f"Bearer {auth.scoped_token(TEST_TOKEN, auth.scope_for_campaign('x-2026-01-01-000000'))}"}
    assert client.get(Routes.campaign_export_download(_CAMPAIGN, export_id),
                      headers=other).status_code == 403


# -- across a restart ---------------------------------------------------------------------------


def test_a_finished_export_answers_its_status_from_disk(client, transport, tmp_path, campaign):
    export_id, status = _create(client, transport, {"tables": []})
    assert (export_dir(campaign, export_id) / EXPORT_FILE).is_file()

    fresh = _transport(tmp_path)          # a new process: nothing in memory
    again = fresh.get_export_status(_CAMPAIGN, export_id)
    assert again.done and not again.error
    assert again.tables == status["tables"]
    assert again.bytes == status["bytes"]
    assert again.finished_at == status["finished_at"]


def test_an_export_lost_to_a_restart_reads_as_failed(tmp_path, campaign):
    lost = export_dir(campaign, "0123456789ab")
    lost.mkdir(parents=True)
    (lost / REQUEST_FILE).write_text(json.dumps({"started_at": "2026-01-01T00:00:00+00:00"}))
    status = _transport(tmp_path).get_export_status(_CAMPAIGN, "0123456789ab")
    assert status.done and "service stopped" in status.error
    assert status.started_at == "2026-01-01T00:00:00+00:00"
    with TestClient(build_app(_transport(tmp_path), mount_mcp=False)) as client:
        # Not done, as far as the tree can tell: the file is not there.
        assert client.get(Routes.campaign_export_download(
            _CAMPAIGN, "0123456789ab")).status_code == 404


# -- the table cache ----------------------------------------------------------------------------


def test_the_cache_keeps_the_tables_an_export_is_reading(client, transport, campaign,
                                                         monkeypatch):
    from robovast.service.service_base import TABLE_CACHE

    _create(client, transport, {"tables": ["poses"]})
    monkeypatch.setattr(transport._exports, "running",  # pylint: disable=protected-access
                        lambda cid: cid == _CAMPAIGN)
    report = transport.clear_service_cache()
    assert (campaign / ".cache" / "tables").is_dir()
    assert [(k.cache, k.name, k.reason) for k in report.kept] == \
        [(TABLE_CACHE, _CAMPAIGN, "an export is reading them right now")]
    assert client.delete(Routes.campaign_tables(_CAMPAIGN)).status_code == 409


def test_a_clear_removes_the_exports_with_the_tables(client, transport, campaign):
    export_id, _status = _create(client, transport, {"tables": []})
    assert export_dir(campaign, export_id).is_dir()
    report = transport.clear_service_cache()
    assert report.freed_bytes > 0
    assert not (campaign / ".cache" / "exports").exists()
    assert client.get(Routes.campaign_export(_CAMPAIGN, export_id)).status_code == 404
