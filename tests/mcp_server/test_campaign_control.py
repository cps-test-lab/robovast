# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for the ``campaign_control`` MCP tools.

These exercise the plugin's orchestration (validate gate, single-flight guard,
liveness tracking, reap, stop) without Docker or real config generation: config
counting is stubbed and the launched process is replaced by a harmless sleeper
that carries the campaign id in its argv (so the registry's cmdline fingerprint
matches, exactly as the real ``--campaign-id`` child would).
"""

import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from robovast.mcp_server.plugins import campaign_control as cc

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GROWTH_SIM = _REPO_ROOT / "configs" / "examples" / "growth_sim" / "growth_sim.vast"

_real_popen = subprocess.Popen


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A fake initialized project rooted at a temp results dir, with stubs."""
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    fake = SimpleNamespace(
        config_path=str(tmp_path / "demo.vast"),
        results_dir=str(results_dir))
    monkeypatch.setattr(cc, "_load_project", lambda: fake)

    # Stub validation/counting (no scenario generation).
    campaign_config = SimpleNamespace(metadata={"name": "itest"})
    monkeypatch.setattr(cc, "_validate", lambda _p, _f: (
        campaign_config,
        {"valid": True, "configs": 2, "runs_per_config": 1,
         "total_trials": 2, "errors": []}))

    # Replace the launched command with a sleeper that still carries the argv
    # (and thus the campaign id) so liveness fingerprinting matches.
    def fake_popen(cmd, **kwargs):
        sleeper = [sys.executable, "-c", "import time,sys; time.sleep(30)"] + cmd
        return _real_popen(sleeper, **kwargs)
    monkeypatch.setattr(cc.subprocess, "Popen", fake_popen)

    # Don't touch Docker during stop.
    monkeypatch.setattr(cc.subprocess, "run", lambda *a, **k: None)

    # Reset the module-level process registry between tests.
    cc._LOCAL_PROCS.clear()
    yield fake
    for proc in list(cc._LOCAL_PROCS.values()):
        proc.terminate()
    cc._LOCAL_PROCS.clear()


@pytest.mark.skipif(not _GROWTH_SIM.exists(),
                    reason="growth_sim example not present")
def test_preview_configurations_resolves_without_running():
    """preview_configurations returns resolved per-cell parameters, no execution."""
    result = cc.preview_configurations(str(_GROWTH_SIM))
    assert "error" not in result
    assert result["configs"] == len(result["configurations"])
    assert result["total_trials"] == result["configs"] * result["runs_per_config"]
    first = result["configurations"][0]
    assert set(first) == {"name", "parameters"}
    assert isinstance(first["parameters"], dict) and first["parameters"]
    # Truncation caps the returned list but keeps the true total.
    capped = cc.preview_configurations(str(_GROWTH_SIM), max_configs=1)
    assert len(capped["configurations"]) == 1
    assert capped["truncated"] is True
    assert capped["configs"] == result["configs"]


def test_preview_configurations_bad_path_returns_error():
    assert "error" in cc.preview_configurations("/no/such/file.vast")


def test_start_status_and_single_flight(project):
    started = cc.start_campaign(backend="local")
    assert "campaign_id" in started, started
    cid = started["campaign_id"]
    assert started["log_path"].endswith(f"_control/logs/{cid}.log")

    status = cc.get_campaign_status(cid)
    assert status["backend"] == "local"
    assert status["status"] == "running"
    assert status["batch_runs_total"] == 2

    # A second local start must be refused while the first is live.
    refused = cc.start_campaign(backend="local")
    assert refused.get("error") == "campaign already running"
    assert refused["running"][0]["campaign_id"] == cid


def test_stop_transitions_to_terminal(project):
    started = cc.start_campaign(backend="local")
    cid = started["campaign_id"]
    assert cc.get_campaign_status(cid)["status"] == "running"

    stopped = cc.stop_campaign(cid)
    assert stopped["stopped"] is True
    assert stopped["status"] == "failed"

    # After stopping, a new campaign is allowed again.
    again = cc.start_campaign(backend="local")
    assert "campaign_id" in again and again["campaign_id"] != cid


def test_reap_marks_finished_when_child_exits(project, monkeypatch):
    # A child that exits 0 should reconcile to "finished" via the reaper.
    def quick_popen(cmd, **kwargs):
        return _real_popen([sys.executable, "-c", "pass"] + cmd, **kwargs)
    monkeypatch.setattr(cc.subprocess, "Popen", quick_popen)

    started = cc.start_campaign(backend="local")
    cid = started["campaign_id"]
    # Wait for the trivial child to exit, then a status call reaps it.
    for _ in range(50):
        if cc.get_campaign_status(cid)["status"] != "running":
            break
        time.sleep(0.1)
    assert cc.get_campaign_status(cid)["status"] == "finished"


def test_cluster_start_is_transparent_and_not_single_flighted(project, monkeypatch):
    # Cluster start spawns a detached `cluster run --wait-and-download` child,
    # tracked by pid like local, and is NOT single-flighted.
    started = cc.start_campaign(backend="cluster")
    assert started["backend"] == "cluster", started
    cid = started["campaign_id"]

    status = cc.get_campaign_status(cid)
    assert status["backend"] == "cluster"
    assert status["status"] == "running"

    # A second cluster start is allowed (unlike local).
    other = cc.start_campaign(backend="cluster")
    assert "campaign_id" in other and other["campaign_id"] != cid

    listing = cc.list_running_campaigns()
    ids = {e["campaign_id"] for e in listing["running"]}
    assert {cid, other["campaign_id"]} <= ids


def test_cluster_stop_kills_waiter_without_reaching_cluster(project, monkeypatch):
    # The cooperative controller stop is best-effort; if the cluster is
    # unreachable, stop still terminates the local waiter and reports a note.
    monkeypatch.setattr(cc, "_cluster_cooperative_stop",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no cluster")))
    started = cc.start_campaign(backend="cluster")
    cid = started["campaign_id"]
    stopped = cc.stop_campaign(cid)
    assert stopped["status"] == "failed"
    assert "note" in stopped and "waiter" in stopped["note"]


def test_local_and_cluster_can_coexist(project):
    # A live cluster campaign must not block a local start (single-flight is
    # local-only), and vice versa.
    cluster = cc.start_campaign(backend="cluster")
    local = cc.start_campaign(backend="local")
    assert "campaign_id" in cluster and "campaign_id" in local
    # But a second LOCAL start is refused while the first local is live.
    refused = cc.start_campaign(backend="local")
    assert refused.get("error") == "campaign already running"


def test_unknown_campaign_returns_error(project):
    assert "error" in cc.get_campaign_status("does-not-exist")
    assert "error" in cc.stop_campaign("does-not-exist")


def test_list_running_reports_live_local(project):
    started = cc.start_campaign(backend="local")
    listing = cc.list_running_campaigns()
    ids = [e["campaign_id"] for e in listing["running"]]
    assert started["campaign_id"] in ids


# -- client-server routing (ROBOVAST_SERVICE_URL set) -----------------------


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


@pytest.fixture
def service(monkeypatch):
    """Route the control tools through a fake robovast-service client."""
    fake = _FakeClient()
    monkeypatch.setattr(cc, "_service_client", lambda: fake)
    return fake


def test_service_start_routes_to_client(service):
    started = cc.start_campaign(config_filter="hospital*", runs=5)
    assert started == {"campaign_id": "svc-campaign-1", "backend": "service"}
    name, req = service.calls[-1]
    assert name == "create_campaign"
    assert req.config_filter == "hospital*" and req.runs == 5


def test_service_status_maps_from_status_model(service):
    st = cc.get_campaign_status("svc-campaign-1")
    assert st["backend"] == "service"
    assert st["status"] == "running"
    assert st["batch_runs_done"] == 3 and st["batch_runs_total"] == 8


def test_service_stop_routes_to_client(service):
    res = cc.stop_campaign("svc-campaign-1")
    assert res["stopped"] is True and res["status"] == "stopping"
    assert ("stop", "svc-campaign-1") in service.calls


def test_service_list_running_filters_terminal(service):
    listing = cc.list_running_campaigns()
    ids = [e["campaign_id"] for e in listing["running"]]
    assert ids == ["svc-running"]  # 'svc-done' (finished) filtered out
    assert listing["count"] == 1


def test_no_service_url_uses_subprocess_path(project):
    # With no service configured, the local subprocess path is used unchanged.
    assert cc._service_client() is None
    started = cc.start_campaign(backend="local")
    assert started["log_path"].endswith(f"_control/logs/{started['campaign_id']}.log")
