# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The image-build MCP tools: registration + service delegation.

Like the other campaign_control tools, these delegate to a reachable
robovast-service through the client; here the client is stubbed so the tool
orchestration is covered without a service, docker, or a cluster.
"""

from types import SimpleNamespace

from robovast.mcp_server import service_access
from robovast.mcp_server.plugins import execution as cc


def test_build_tools_registered():
    names = [f.__name__ for f in cc._TOOLS]
    assert "build_experiment_image" in names
    assert "get_image_build_status" in names
    assert "get_image_build_log" in names


def test_build_experiment_image_no_service(monkeypatch):
    monkeypatch.setattr(service_access, "service_client", lambda: None)
    out = cc.build_experiment_image(workspace_id="ws1")
    assert "error" in out
    assert "no robovast-service" in out["error"]


def _stub_build(monkeypatch, ref, captured=None):
    class _Client:
        def build_image(self, request):
            if captured is not None:
                captured["request"] = request
            return ref

    monkeypatch.setattr(service_access, "service_client", lambda: _Client())


def test_build_experiment_image_delegates(monkeypatch):
    captured = {}
    _stub_build(monkeypatch, SimpleNamespace(
        build_id="imgbuild-sut-abc", tag="sut", cached=True,
        builds={"sut": "imgbuild-sut-abc"},
        cached_builds={"sut": True}), captured)
    out = cc.build_experiment_image(workspace_id="ws1", config_path="a.vast")
    # ``builds`` carries every image the request started; ``build_id`` names only one, so
    # a campaign building two is not silently reported as having built one. ``next_step``
    # is the wait, in band -- here everything was a cache hit, which is the one case with
    # nothing to wait for and so the one where naming a wait command would be wrong.
    assert out == {"build_id": "imgbuild-sut-abc", "tag": "sut", "cached": True,
                   "builds": {"sut": "imgbuild-sut-abc"},
                   "cached_builds": {"sut": True},
                   "next_step": ("every image is built — start_campaign(...) to run it, "
                                 "or exec_in_container(...) to look inside it")}
    assert captured["request"].workspace_id == "ws1"
    assert captured["request"].config_path == "a.vast"
    assert captured["request"].container is None


def test_one_containers_cache_hit_is_not_the_requests(monkeypatch):
    """The reported bug: `cached` was whichever value the primary container happened to have.

    A scenario image already built and a `sut` image still building was reported as
    ``cached: true`` with "nothing to wait for" -- and the caller went straight on to exec in
    a `sut` image that did not exist yet.
    """
    # The service aggregates (see test_image_build_core::primary_build_ref); what this tool
    # owes the caller is a wait that names the build still running and not the cached one.
    _stub_build(monkeypatch, SimpleNamespace(
        build_id="b-scenario", tag="scenario", cached=False,
        builds={"scenario": "b-scenario", "sut": "b-sut"},
        cached_builds={"scenario": True, "sut": False}))
    out = cc.build_experiment_image(workspace_id="ws1")
    assert out["cached"] is False, "a request is a cache hit only when nothing has to build"
    assert out["cached_builds"] == {"scenario": True, "sut": False}
    assert "b-sut" in out["next_step"]
    assert "b-scenario" not in out["next_step"]


def test_a_service_without_per_container_verdicts_waits_on_everything(monkeypatch):
    """An older service sends no ``cached_builds``. Waiting on all of them is the safe read;
    trusting the single aggregate flag is what went wrong."""
    _stub_build(monkeypatch, SimpleNamespace(
        build_id="b-scenario", tag="scenario", cached=False,
        builds={"scenario": "b-scenario", "sut": "b-sut"}))
    out = cc.build_experiment_image(workspace_id="ws1")
    assert out["cached_builds"] == {}
    assert "b-scenario" in out["next_step"] and "b-sut" in out["next_step"]


def test_get_image_build_status_surfaces_structured_error(monkeypatch):
    err = SimpleNamespace(
        model_dump=lambda: {"phase": "pip", "fixable_by": "agent",
                            "entry": "shapely==99", "message": "no distribution"})
    status = SimpleNamespace(build_id="b1", tag="foo", phase="failed", done=True,
                            cached=False, image_ref="build:foo", error=err)

    class _Client:
        def get_image_build_status(self, build_id):
            return status

    monkeypatch.setattr(service_access, "service_client", lambda: _Client())
    out = cc.get_image_build_status("b1")
    assert out["phase"] == "failed"
    assert out["image_ref"] == "build:foo"
    assert out["error_detail"]["entry"] == "shapely==99"
    assert out["error_detail"]["fixable_by"] == "agent"


def test_get_image_build_log_streams(monkeypatch):
    class _Client:
        def get_image_build_log(self, build_id, offset):
            return SimpleNamespace(text="building...\n", next_offset=12, eof=True)

    monkeypatch.setattr(service_access, "service_client", lambda: _Client())
    out = cc.get_image_build_log("b1")
    assert out["text"] == "building...\n"
    assert out["eof"] is True
