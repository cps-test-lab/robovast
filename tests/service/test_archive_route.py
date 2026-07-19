# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""GET /campaigns/{id}/archive — cluster streams it; a local service refuses.

A local ``vast serve`` has the campaign on its own filesystem, so a download is
pointless and the route returns 409 (mirroring ``upload_to_share`` being
local-unsupported). A cluster service streams the postprocessed tar.gz from the
object store with no scratch.
"""

import threading

from fastapi.testclient import TestClient

from robovast.service.app import build_app
from robovast.service.client import LocalTransport


def _local_transport() -> LocalTransport:
    lt = LocalTransport.__new__(LocalTransport)
    lt._campaigns = {}
    lt._lock = threading.Lock()
    lt.store = None
    return lt


def test_local_archive_refuses():
    with TestClient(build_app(_local_transport())) as client:
        resp = client.get("/campaigns/camp-2026-01-01-000000/archive")
    assert resp.status_code == 409
    assert "already on this host" in resp.json()["detail"]
