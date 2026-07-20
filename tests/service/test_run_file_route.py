# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""GET /campaigns/{id}/run-files/{config}/{run}/{path} — one run artifact file.

Serves per-run binaries (e.g. the scene3d panel's ``scene/scene.json`` + its sibling
``scene.bin``) straight from the run directory. The path segment is confined to that
directory: ``..`` escapes are a 400, missing files a 404.
"""

import threading

from fastapi.testclient import TestClient

from robovast.service.app import build_app
from robovast.service.client import LocalTransport


def _local_transport(results_root) -> LocalTransport:
    lt = LocalTransport.__new__(LocalTransport)
    lt._campaigns = {}
    lt._lock = threading.Lock()
    lt.store = None
    lt._campaigns_root = lambda: results_root
    return lt


def _client(tmp_path):
    run_dir = tmp_path / "camp-1" / "nav" / "3" / "scene"
    run_dir.mkdir(parents=True)
    (run_dir / "scene.json").write_text('{"up": "z"}')
    (run_dir / "scene.bin").write_bytes(b"\x00\x01\x02")
    (tmp_path / "secret.txt").write_text("outside the run dir")
    return TestClient(build_app(_local_transport(tmp_path)))


def test_serves_run_file_with_content_type(tmp_path):
    with _client(tmp_path) as client:
        resp = client.get("/campaigns/camp-1/run-files/nav/3/scene/scene.json")
        assert resp.status_code == 200
        assert resp.json() == {"up": "z"}
        assert resp.headers["content-type"].startswith("application/json")

        # Binary sibling (what a relative scene.json -> scene.bin fetch resolves to).
        resp = client.get("/campaigns/camp-1/run-files/nav/3/scene/scene.bin")
        assert resp.status_code == 200
        assert resp.content == b"\x00\x01\x02"
        assert resp.headers["content-type"] == "application/octet-stream"


def test_missing_file_is_404(tmp_path):
    with _client(tmp_path) as client:
        resp = client.get("/campaigns/camp-1/run-files/nav/3/scene/missing.json")
    assert resp.status_code == 404


def test_path_escape_is_rejected(tmp_path):
    with _client(tmp_path) as client:
        # httpx normalizes ../ in URLs, so exercise the transport guard directly too.
        resp = client.get(
            "/campaigns/camp-1/run-files/nav/3/..%2F..%2F..%2Fsecret.txt")
        assert resp.status_code in (400, 404)
    import pytest

    lt = _local_transport(tmp_path)
    with pytest.raises(ValueError):
        lt.get_run_file("camp-1", "nav", 3, "../../../secret.txt")
