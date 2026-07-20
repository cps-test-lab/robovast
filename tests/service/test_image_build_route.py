# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""POST /image-builds + GET /image-builds/{id}/{status,log} — experiment image builds.

The routes bind to the interface's ``build_image`` / ``get_image_build_status`` /
``get_image_build_log``; exercised here with a fake transport so the HTTP wiring and
``_guard`` error mapping are covered without docker or a cluster.
"""

from fastapi.testclient import TestClient

from robovast.service.app import build_app
from robovast.service.interface import (BuildImageRequest, ImageBuildError,
                                        ImageBuildRef, ImageBuildStatus, LogChunk)


class _FakeImpl:
    """Minimal impl exposing only the image-build surface the routes touch."""

    def __init__(self):
        self.last_request = None

    def build_image(self, request: BuildImageRequest) -> ImageBuildRef:
        self.last_request = request
        if request.workspace_id == "no-build":
            raise ValueError("project has no 'build:' section")
        return ImageBuildRef(build_id="imgbuild-foo-abc123", tag="foo", cached=False)

    def get_image_build_status(self, build_id: str) -> ImageBuildStatus:
        if build_id == "missing":
            raise KeyError("unknown build 'missing'")
        return ImageBuildStatus(
            build_id=build_id, tag="foo", phase="failed", done=True,
            image_ref="build:foo",
            error=ImageBuildError(phase="apt", fixable_by="agent",
                                  entry="ros-jazzy-typo",
                                  message="apt could not locate package"))

    def get_image_build_log(self, build_id: str, offset: int = 0) -> LogChunk:
        data = b"step 1/3\nstep 2/3\n"
        return LogChunk(text=data[offset:].decode(), next_offset=len(data), eof=True)

    def shutdown(self):
        pass


def _client():
    return TestClient(build_app(_FakeImpl()))


def test_build_image_returns_ref():
    with _client() as client:
        resp = client.post("/image-builds",
                           json={"workspace_id": "ws1", "config_path": ""})
        assert resp.status_code == 200
        body = resp.json()
        assert body["build_id"] == "imgbuild-foo-abc123"
        assert body["tag"] == "foo"
        assert body["cached"] is False


def test_build_image_no_build_section_is_400():
    with _client() as client:
        resp = client.post("/image-builds", json={"workspace_id": "no-build"})
        assert resp.status_code == 400
        assert "build:" in resp.json()["detail"]


def test_status_carries_structured_error():
    with _client() as client:
        resp = client.get("/image-builds/imgbuild-foo-abc123/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["phase"] == "failed"
        assert body["done"] is True
        # image_ref stays symbolic — never a registry-qualified ref
        assert body["image_ref"] == "build:foo"
        assert body["error"]["phase"] == "apt"
        assert body["error"]["entry"] == "ros-jazzy-typo"
        assert body["error"]["fixable_by"] == "agent"


def test_status_unknown_build_is_404():
    with _client() as client:
        resp = client.get("/image-builds/missing/status")
        assert resp.status_code == 404


def test_build_log_streams():
    with _client() as client:
        resp = client.get("/image-builds/imgbuild-foo-abc123/log")
        assert resp.status_code == 200
        body = resp.json()
        assert "step 1/3" in body["text"]
        assert body["eof"] is True
