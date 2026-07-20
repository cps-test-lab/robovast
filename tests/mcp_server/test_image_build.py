# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The image-build MCP tools: registration + service delegation.

Like the other campaign_control tools, these delegate to a reachable
robovast-service through the client; here the client is stubbed so the tool
orchestration is covered without a service, docker, or a cluster.
"""

from types import SimpleNamespace

from robovast.mcp_server.plugins import campaign_control as cc


def test_build_tools_registered():
    names = [f.__name__ for f in cc._TOOLS]
    assert "build_experiment_image" in names
    assert "get_image_build_status" in names
    assert "get_image_build_log" in names


def test_build_experiment_image_no_service(monkeypatch):
    monkeypatch.setattr(cc, "_service_client", lambda: None)
    out = cc.build_experiment_image(workspace_id="ws1")
    assert "error" in out
    assert "no robovast-service" in out["error"]


def test_build_experiment_image_delegates(monkeypatch):
    captured = {}

    class _Client:
        def build_image(self, request):
            captured["request"] = request
            return SimpleNamespace(build_id="imgbuild-foo-abc", tag="foo", cached=True)

    monkeypatch.setattr(cc, "_service_client", lambda: _Client())
    out = cc.build_experiment_image(workspace_id="ws1", config_path="a.vast")
    assert out == {"build_id": "imgbuild-foo-abc", "tag": "foo", "cached": True}
    assert captured["request"].workspace_id == "ws1"
    assert captured["request"].config_path == "a.vast"


def test_get_image_build_status_surfaces_structured_error(monkeypatch):
    err = SimpleNamespace(
        model_dump=lambda: {"phase": "pip", "fixable_by": "agent",
                            "entry": "shapely==99", "message": "no distribution"})
    status = SimpleNamespace(build_id="b1", tag="foo", phase="failed", done=True,
                            cached=False, image_ref="build:foo", error=err)

    class _Client:
        def get_image_build_status(self, build_id):
            return status

    monkeypatch.setattr(cc, "_service_client", lambda: _Client())
    out = cc.get_image_build_status("b1")
    assert out["phase"] == "failed"
    assert out["image_ref"] == "build:foo"
    assert out["error_detail"]["entry"] == "shapely==99"
    assert out["error_detail"]["fixable_by"] == "agent"


def test_get_image_build_log_streams(monkeypatch):
    class _Client:
        def get_image_build_log(self, build_id, offset):
            return SimpleNamespace(text="building...\n", next_offset=12, eof=True)

    monkeypatch.setattr(cc, "_service_client", lambda: _Client())
    out = cc.get_image_build_log("b1")
    assert out["text"] == "building...\n"
    assert out["eof"] is True
