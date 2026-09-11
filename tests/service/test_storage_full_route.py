# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A full disk is a 507 with a sentence, on every surface -- not a 500, a 400 or a 409.

A write that fails for lack of space says nothing about the request that caused it. Reported
as a generic 500 it reads as a bug; reported through the class of whatever layer translated it
(an archive that "could not be extracted", a conflict) it sends the caller to fix a request
that was fine. The one thing the caller can do -- free space and retry -- is only said if the
failure is recognised as what it is.
"""

import errno

import anyio
import pytest
from fastapi.testclient import TestClient

from robovast.common.errors import STORAGE_FULL_DETAIL
from robovast.mcp_server.service_access import error_result
from robovast.service.app import build_app
from robovast.service.client import LocalTransport
from robovast.service.interface import Routes, ServiceError
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore


def _full() -> OSError:
    return OSError(errno.ENOSPC, "No space left on device", "/var/lib/somewhere/data.db")


class _FullDisk:
    """Impl whose data-status probe fails the way a write to a full disk fails."""

    def __init__(self, exc):
        self.exc = exc

    def campaign_data_status(self, campaign_id: str):
        raise self.exc

    def shutdown(self):
        pass


def _status_probe(exc):
    with TestClient(build_app(_FullDisk(exc), mount_mcp=False)) as client:
        return client.get(Routes.campaign_data_status("camp-1"))


def test_a_full_disk_behind_a_guarded_route_is_a_507_with_the_sentence():
    resp = _status_probe(_full())
    assert resp.status_code == 507
    assert resp.json()["detail"] == STORAGE_FULL_DETAIL
    # Where on the service host the write failed is nothing the caller can act on.
    assert "/var/lib" not in resp.text


def test_a_full_disk_is_not_reported_as_the_class_a_layer_translated_it_into():
    """Checked ahead of the class-based arms: this ValueError would otherwise be a 400."""
    translated = ValueError("could not extract the archive")
    translated.__cause__ = _full()      # what ``raise ... from`` records
    assert _status_probe(translated).status_code == 507


def test_a_full_index_disk_is_a_507():
    class _DiskFull(RuntimeError):
        sqlstate = "53100"

    # A RuntimeError, which would otherwise be a 409 "conflict".
    assert _status_probe(_DiskFull("could not extend file")).status_code == 507


def test_other_failures_keep_their_mapping():
    assert _status_probe(ValueError("bad")).status_code == 400
    assert _status_probe(RuntimeError("busy")).status_code == 409


def test_a_full_disk_outside_any_guard_is_a_507_too():
    """A route that writes without ``_guard`` must not answer differently from one inside."""
    app = build_app(_FullDisk(_full()), mount_mcp=False)

    def _writes_unguarded():
        raise _full()

    app.add_api_route("/test-writes-unguarded", _writes_unguarded)
    # Starlette re-raises after the handler has answered, so the traceback still reaches the
    # log; the client is told not to raise it into the test.
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/test-writes-unguarded")
    assert resp.status_code == 507
    assert resp.json()["detail"] == STORAGE_FULL_DETAIL


def test_any_other_unhandled_failure_is_still_a_plain_500():
    app = build_app(_FullDisk(_full()), mount_mcp=False)

    def _breaks():
        raise OSError(errno.EACCES, "Permission denied")

    app.add_api_route("/test-breaks", _breaks)
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/test-breaks").status_code == 500


# -- the archive upload, which writes outside ``_guard`` -----------------------------------

class _FullFile:
    """An async file whose every write fails for lack of space."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def write(self, chunk):
        raise _full()


@pytest.fixture(name="archive_client")
def _archive_client(tmp_path):
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    transport = LocalTransport(store=store)
    transport._campaigns_root = lambda: tmp_path / "results"
    with TestClient(build_app(transport, mount_mcp=False)) as client:
        yield client, tmp_path / "results"


def test_an_archive_upload_onto_a_full_disk_is_a_507_and_leaves_nothing(archive_client,
                                                                        monkeypatch):
    client, results = archive_client
    grant = client.post(Routes.CAMPAIGN_ARCHIVES)
    assert grant.status_code == 200, grant.text

    async def _open_full(path, mode):
        # The file exists once opened, as a real one would, so the cleanup has work to do.
        await anyio.Path(path).touch()
        return _FullFile()

    monkeypatch.setattr(anyio, "open_file", _open_full)
    resp = client.put(grant.json()["url"], content=b"x" * 1024)

    assert resp.status_code == 507
    assert resp.json()["detail"] == STORAGE_FULL_DETAIL
    # A truncated tarball left behind would only fail later, at import, and take space.
    assert not [p for p in results.rglob("*") if p.is_file()]


def test_an_archive_upload_that_fails_otherwise_names_no_server_path(archive_client,
                                                                     monkeypatch):
    client, _ = archive_client
    grant = client.post(Routes.CAMPAIGN_ARCHIVES).json()

    async def _open_denied(path, mode):
        raise OSError(errno.EACCES, "Permission denied", str(path))

    monkeypatch.setattr(anyio, "open_file", _open_denied)
    resp = client.put(grant["url"], content=b"x")

    assert resp.status_code == 500
    assert resp.json()["detail"] == "could not store the archive: Permission denied"


# -- the MCP surface, which is handed the raw exception when mounted in the service ---------

def test_an_mcp_tool_reports_a_full_disk_with_the_same_sentence():
    assert error_result(_full()) == {"error": STORAGE_FULL_DETAIL}


def test_an_mcp_tool_over_http_already_carries_the_sentence():
    """Remote, the tool is handed the service's 507 -- whose detail is the sentence itself."""
    refused = ServiceError(507, STORAGE_FULL_DETAIL, "http://service.example/x")
    assert error_result(refused)["error"] == STORAGE_FULL_DETAIL
