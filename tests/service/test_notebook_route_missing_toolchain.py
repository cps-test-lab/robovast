# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""GET /campaigns/{id}/notebook without the notebook toolchain installed.

The toolchain (``robovast[notebooks]``) is an optional extra, so a service can be
installed without it and serve everything else. Browsing results in such a service used
to hit a bare ``ModuleNotFoundError: No module named 'nbclient'`` inside the handler —
an ASGI traceback in the service log, a 500 with no ``detail`` in the browser, and
nothing anywhere naming what to install. What is defended here is the reverse: one
status the Explorer can render and a message that says what is missing and how to get
it.
"""

import sys

import pytest
from fastapi.testclient import TestClient

from robovast.service.app import build_app
from robovast.service.client import LocalTransport
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore

_CAMPAIGN = "camp-2026-01-01-000000"


@pytest.fixture(name="client")
def _client(monkeypatch, tmp_path):
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    transport = LocalTransport(store=store)
    # ``None`` in sys.modules is how the import system is told a module is absent.
    monkeypatch.setitem(sys.modules, "nbclient.exceptions", None)
    with TestClient(build_app(transport), raise_server_exceptions=False) as test_client:
        yield test_client


def test_missing_toolchain_is_a_503_naming_the_extra(client):
    response = client.get(f"/campaigns/{_CAMPAIGN}/notebook",
                          params={"workload": "Analysis", "level": "campaign"})
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "nbclient" in detail
    assert "robovast[notebooks]" in detail
