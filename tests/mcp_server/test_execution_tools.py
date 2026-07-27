# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the ``campaign_control`` MCP tools.

The control tools are a **strict client** of a running ``robovast-service``: there
is no local subprocess path. These tests cover the read-only preview tool, routing
through a fake service client (including per-campaign ``backend`` selection), and
that every control tool fails loudly when no service is reachable.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from robovast.mcp_server import service_access
from robovast.mcp_server.plugins import authoring, execution, results_lifecycle

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GROWTH_SIM = _REPO_ROOT / "configs" / "examples" / "growth_sim" / "growth_sim.vast"


# -- preview (read-only, serviceless) ---------------------------------------


@pytest.mark.skipif(not _GROWTH_SIM.exists(),
                    reason="growth_sim example not present")
def test_preview_configurations_resolves_without_running():
    """preview_configurations returns resolved per-cell parameters, no execution."""
    result = authoring.preview_configurations(str(_GROWTH_SIM))
    assert "error" not in result
    assert result["configs"] == len(result["configurations"])
    assert result["total_trials"] == result["configs"] * result["runs_per_config"]
    first = result["configurations"][0]
    assert set(first) == {"name", "parameters"}
    assert isinstance(first["parameters"], dict) and first["parameters"]
    # Truncation caps the returned list but keeps the true total.
    capped = authoring.preview_configurations(str(_GROWTH_SIM), max_configs=1)
    assert len(capped["configurations"]) == 1
    assert capped["truncated"] is True
    assert capped["configs"] == result["configs"]


def test_preview_configurations_bad_path_returns_error():
    assert "error" in authoring.preview_configurations("/no/such/file.vast")


# -- client-server routing (a reachable service) ----------------------------


class _FakeClient:
    """A RobovastInterface stand-in recording calls, for the service path."""

    def __init__(self):
        self.calls = []

    def create_campaign(self, request):
        from robovast.service.interface import CampaignRef
        self.calls.append(("create_campaign", request))
        return CampaignRef(campaign_id="svc-campaign-1")

    def get_status(self, campaign_id):
        from robovast.service.interface import Status
        self.calls.append(("get_status", campaign_id))
        return Status(phase="running", campaign_id=campaign_id,
                      runs={"completed": 3, "total": 8})

    def stop(self, campaign_id):
        from robovast.service.interface import ActionResult
        self.calls.append(("stop", campaign_id))
        return ActionResult(ok=True, message="stop requested")

    def list_campaigns(self, request=None):
        from robovast.service.interface import (CampaignSummary,
                                                ListCampaignsResponse)
        self.calls.append(("list_campaigns", request))
        return ListCampaignsResponse(total=2, campaigns=[
            CampaignSummary(campaign_id="svc-running", phase="running"),
            CampaignSummary(campaign_id="svc-done", phase="finished")])

    def resource_usage(self, backend=None):
        from robovast.service.interface import ResourceUsage
        self.calls.append(("resource_usage", backend))
        return ResourceUsage(backend="docker", cpu_capacity=8, cpu_used=1,
                             memory_capacity_bytes=16, memory_used_bytes=2,
                             parallel_runs=False)

    def build_image(self, request):
        from robovast.service.interface import ImageBuildRef
        self.calls.append(("build_image", request))
        return ImageBuildRef(build_id="b1", tag="t", cached=True)


@pytest.fixture
def service(monkeypatch):
    """Route the control tools through a fake robovast-service client."""
    fake = _FakeClient()
    monkeypatch.setattr(service_access, "service_client", lambda: fake)
    return fake


def test_service_start_routes_to_client(service):
    started = execution.start_campaign(config_filter="hospital*", runs=5)
    assert started == {"campaign_id": "svc-campaign-1", "backend": "service-default"}
    name, req = service.calls[-1]
    assert name == "create_campaign"
    assert req.config_filter == "hospital*" and req.runs == 5
    assert req.backend is None  # unset -> service default (cluster when available)


def test_service_start_passes_backend(service):
    execution.start_campaign(backend="local")
    _name, req = service.calls[-1]
    assert req.backend == "local"


def test_service_start_passes_description(service):
    execution.start_campaign(description="pilot: 5 reps DWB vs MPPI on open_space")
    _name, req = service.calls[-1]
    assert req.description == "pilot: 5 reps DWB vs MPPI on open_space"


def test_start_without_description_sends_empty(service):
    execution.start_campaign()
    _name, req = service.calls[-1]
    assert req.description == ""


def test_start_rejects_overlong_description(service):
    """Refused before launch, with the limit named — not a pydantic traceback."""
    from robovast.service.interface import DESCRIPTION_MAX_LEN
    res = execution.start_campaign(description="x" * (DESCRIPTION_MAX_LEN + 1))
    assert "error" in res and str(DESCRIPTION_MAX_LEN) in res["error"]
    assert not service.calls  # never reached the service


def test_start_rejects_unknown_backend(service):
    res = execution.start_campaign(backend="gpu")
    assert "error" in res and "unknown backend" in res["error"]
    assert not service.calls  # never reached the service


def test_service_status_maps_from_status_model(service):
    st = execution.get_campaign_status("svc-campaign-1")
    assert st["backend"] == "service"
    assert st["status"] == "running"
    assert st["batch_runs_done"] == 3 and st["batch_runs_total"] == 8


def test_service_stop_routes_to_client(service):
    res = execution.stop_campaign("svc-campaign-1")
    assert res["stopped"] is True and res["status"] == "stopping"
    assert ("stop", "svc-campaign-1") in service.calls


def test_service_list_running_filters_terminal(service):
    listing = execution.list_running_campaigns()
    ids = [e["campaign_id"] for e in listing["running"]]
    assert ids == ["svc-running"]  # 'svc-done' (finished) filtered out
    assert listing["count"] == 1


def test_resource_usage_passes_backend(service):
    execution.resource_usage(backend="cluster")
    assert ("resource_usage", "cluster") in service.calls
    execution.resource_usage()  # unset -> None (service default)
    assert ("resource_usage", None) in service.calls


def test_build_image_passes_backend(service):
    execution.build_experiment_image(backend="cluster")
    name, req = service.calls[-1]
    assert name == "build_image" and req.backend == "cluster"


# -- fail loudly when no service is reachable (no local fallback) ------------


@pytest.fixture
def no_service(monkeypatch):
    monkeypatch.setattr(service_access, "service_client", lambda: None)


def test_start_without_service_fails_loudly(no_service):
    res = execution.start_campaign()
    assert "error" in res and "no robovast-service" in res["error"]


def test_status_without_service_fails_loudly(no_service):
    assert "no robovast-service" in execution.get_campaign_status("x")["error"]


def test_stop_without_service_fails_loudly(no_service):
    assert "no robovast-service" in execution.stop_campaign("x")["error"]


def test_list_running_without_service_fails_loudly(no_service):
    assert "no robovast-service" in execution.list_running_campaigns()["error"]


def test_resource_usage_without_service_fails_loudly(no_service):
    assert "no robovast-service" in execution.resource_usage()["error"]


# -- get_campaign_download — returns a web link, never writes a file ---------


def _fake_download_client(backend, base_url="http://127.0.0.1:8800"):
    from robovast.service.interface import VersionInfo
    return SimpleNamespace(
        base_url=base_url,
        version=lambda: VersionInfo(robovast_version="test", backend=backend))


def test_get_campaign_download_cluster_returns_url(monkeypatch):
    monkeypatch.setattr(service_access, "service_client",
                        lambda: _fake_download_client("kubernetes"))
    res = results_lifecycle.get_campaign_download("camp-2026-01-01-000000")
    assert res["url"] == "http://127.0.0.1:8800/campaigns/camp-2026-01-01-000000/archive"
    assert res["path"] == "/campaigns/camp-2026-01-01-000000/archive"
    assert "web UI" in res["note"]
    assert "error" not in res


def test_get_campaign_download_local_has_no_url(monkeypatch):
    monkeypatch.setattr(service_access, "service_client",
                        lambda: _fake_download_client("docker"))
    res = results_lifecycle.get_campaign_download("camp-2026-01-01-000000")
    assert "url" not in res
    assert "filesystem" in res["note"]


def test_get_campaign_download_no_service_errors(monkeypatch):
    monkeypatch.setattr(service_access, "service_client", lambda: None)
    res = results_lifecycle.get_campaign_download("camp-2026-01-01-000000")
    assert "error" in res and "no robovast-service" in res["error"]


# -- get_campaign_log is served by the service, not by the local results dir ------


def test_get_campaign_log_is_served_by_the_service(monkeypatch):
    """The campaign log comes from the service, which knows where it lives.

    Regression: this tool read the local results dir directly, so on a cluster
    service -- where the durable log is in the object store and the live one is pod
    scratch -- it reported an empty log even though
    ``ClusterService.get_campaign_logs`` already served both.
    """
    from robovast.mcp_server import service_access
    from robovast.mcp_server.plugins import execution

    class _Chunk:
        text = "===== RUN =====\nline one\nline two\n"

    class _Fake:
        def __init__(self):
            self.asked = None

        def get_campaign_logs(self, campaign_id, offset=0):
            self.asked = (campaign_id, offset)
            return _Chunk()

    fake = _Fake()
    monkeypatch.setattr(service_access, "service_client", lambda: fake)
    # Would raise if the local disk path were taken: no such campaign anywhere.
    monkeypatch.setattr(
        execution.results_resolver, "resolve_campaign_path",
        lambda *a, **k: pytest.fail("must not read the local results dir"))

    out = execution.get_campaign_log("camp-2026-01-01-000000")
    assert fake.asked == ("camp-2026-01-01-000000", 0)
    assert "line one" in out["content"]
    assert out["total_lines"] == 3


def test_get_campaign_log_falls_back_to_disk_with_no_service(monkeypatch, tmp_path):
    """With no service, an archived results tree is still readable offline."""
    from robovast.mcp_server import service_access
    from robovast.mcp_server.plugins import execution

    campaign = tmp_path / "camp-2026-01-01-000000"
    (campaign / "_execution").mkdir(parents=True)
    (campaign / "_execution" / "controller.log").write_text("from disk\n")

    monkeypatch.setattr(service_access, "service_client", lambda: None)
    monkeypatch.setattr(execution.results_resolver, "resolve_campaign_path",
                        lambda *a, **k: campaign)
    assert "from disk" in execution.get_campaign_log("camp-2026-01-01-000000")["content"]
