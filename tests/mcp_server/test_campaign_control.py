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
from types import SimpleNamespace

import pytest

from robovast.mcp_server.plugins import campaign_control as cc

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


def test_start_status_and_single_flight(project):
    started = cc.start_campaign(backend="local")
    assert "campaign_id" in started, started
    cid = started["campaign_id"]
    assert started["log_path"].endswith(f"_control/logs/{cid}.log")

    status = cc.get_campaign_status(cid)
    assert status["backend"] == "local"
    assert status["status"] == "running"
    assert status["runs_total"] == 2

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
