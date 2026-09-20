# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""What waits on a disk or a database does not wait on the event loop.

The service answers every request -- ``/healthz`` included -- from one event loop. A write
made on it stalls all of them for as long as the disk takes, and a disk that is filling makes
every write slow at once: the liveness probe then fails, and a service that was only short of
disk is restarted, dropping every open stream. Each test here puts a blocking call where it
can observe which side of that line it ran on.
"""

import asyncio
import time

import pytest
from starlette.testclient import TestClient

from robovast.service import event_log
from robovast.service.app import build_app
from robovast.service.local_transport import LocalTransport
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore
from tests.service.conftest import AUTH_HEADERS


def _on_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


@pytest.fixture(name="record_sites")
def _record_sites(monkeypatch):
    """Where each event-log write ran: ``True`` for the event loop."""
    sites = []
    real = event_log.EventLog.append

    def append(self, *args, **kwargs):
        sites.append(_on_event_loop())
        return real(self, *args, **kwargs)

    monkeypatch.setattr(event_log.EventLog, "append", append)
    return sites


def _app(tmp_path):
    store = WorkspaceStore(registry=WorkspaceRegistry(root=str(tmp_path / "ws")))
    return build_app(LocalTransport(store=store), mount_mcp=False, auth_token="")


def test_a_refusal_is_recorded_off_the_event_loop(tmp_path, record_sites):
    client = TestClient(_app(tmp_path))
    assert client.post("/campaigns/does-not-exist/retrigger").status_code >= 400
    assert record_sites == [False]


def test_a_request_that_never_parsed_is_recorded_off_the_event_loop(tmp_path, record_sites):
    client = TestClient(_app(tmp_path))
    assert client.get("/admin/events", params={"limit": "not-a-number"}).status_code == 422
    assert record_sites == [False]


def test_a_caller_turned_away_at_the_gate_is_recorded_off_the_event_loop(tmp_path,
                                                                        record_sites):
    """The gate runs in front of every request, so a stall there stalls all of them."""
    client = TestClient(_app(tmp_path), headers={"Authorization": "Bearer not-the-token"})
    assert client.get("/campaigns").status_code == 401
    assert record_sites == [False]


def test_an_upload_is_written_off_the_event_loop(tmp_path, monkeypatch):
    store = WorkspaceStore(registry=WorkspaceRegistry(root=str(tmp_path / "ws")))
    ws = store.registry.create("demo")["workspace_id"]
    store.registry.project_dir(ws).mkdir(parents=True, exist_ok=True)
    client = TestClient(build_app(LocalTransport(store=store), mount_mcp=False, auth_token=""),
                        headers=AUTH_HEADERS)
    grant = client.post("/uploads", json={"address": f"/sources/{ws}/a.bin"}).json()

    sites = []
    real = WorkspaceStore.write_upload

    def write_upload(self, token, data):
        sites.append(_on_event_loop())
        return real(self, token, data)

    monkeypatch.setattr(WorkspaceStore, "write_upload", write_upload)
    assert client.put(f"/uploads/{grant['token']}", content=b"x").status_code == 200
    assert sites == [False]


def test_a_slow_refusal_record_does_not_hold_other_requests(tmp_path, monkeypatch):
    """The property the tests above stand for, measured end to end.

    A refusal whose record takes a second must not delay ``/healthz`` asked for meanwhile:
    that probe is what restarts the pod.
    """
    import threading

    started = threading.Event()
    real = event_log.EventLog.append

    def slow_append(self, *args, **kwargs):
        started.set()
        time.sleep(1.0)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(event_log.EventLog, "append", slow_append)
    client = TestClient(_app(tmp_path))
    with client:
        refusing = threading.Thread(
            target=lambda: client.post("/campaigns/does-not-exist/retrigger"))
        refusing.start()
        assert started.wait(5)
        began = time.monotonic()
        assert client.get("/healthz").status_code == 200
        waited = time.monotonic() - began
        refusing.join()
    assert waited < 0.5, f"/healthz waited {waited:.2f}s behind a refusal being recorded"
