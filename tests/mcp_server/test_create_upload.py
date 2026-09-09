# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""``create_upload`` must hand back a usable URL even when the MCP is mounted
in-process inside the service.

The HTTP route handler (``app.py``) fills ``UploadGrant.url`` in on its way out; a
caller reaching the same implementation in-process skips that handler entirely (see
``use_in_process_service``), so without the tool resolving it itself, every upload
grant issued by an in-process deployment came back with ``url: None`` — unusable, since
the tool's whole contract is "PUT the bytes to ``url``".
"""

import pytest

from robovast.mcp_server import service_access
from robovast.mcp_server.plugins import authoring
from robovast.service.interface import UploadGrant, VersionInfo


class _FakeClient:
    """An in-process transport: ``create_upload`` behaves like ``LocalTransport``'s
    (no ``url``), and ``version()`` carries the service's declared web origin, exactly
    as it does for a real in-process deployment (``get_service_info``'s ``web_base``)."""

    def __init__(self, web_base="https://robovast.example.test"):
        self.web_base = web_base
        self.create_upload_calls = []

    def create_upload(self, request):
        self.create_upload_calls.append(request)
        return UploadGrant(token="tok-123", path=request.address, expires_in=600, url=None)

    def version(self):
        return VersionInfo(robovast_version="0.0.0-test", web_base=self.web_base)


@pytest.fixture
def in_process(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(service_access, "service_client", lambda: fake)
    return fake


def test_create_upload_resolves_a_url_when_in_process(in_process):
    out = authoring.create_upload("/sources/ws-1/worlds/box_mu1.yaml")
    assert out["url"] == "https://robovast.example.test/uploads/tok-123"
    assert out["token"] == "tok-123"


def test_create_upload_omits_url_when_no_origin_is_declared(monkeypatch):
    fake = _FakeClient(web_base="")
    monkeypatch.setattr(service_access, "service_client", lambda: fake)
    out = authoring.create_upload("/sources/ws-1/worlds/box_mu1.yaml")
    assert not out.get("url")
