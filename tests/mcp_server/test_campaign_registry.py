# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the crash-safe campaign run-state registry.

These exercise the guarantees the ``campaign_control`` plugin relies on:
single-flight for local launches, stale-PID recovery, PID-reuse hardening, and
atomic writes under concurrent writers. No MCP server is involved.
"""

import multiprocessing
import os
import subprocess
import sys
import time

from robovast.mcp_server.campaign_registry import (CampaignRegistry,
                                                    LAUNCH_GRACE_SECONDS,
                                                    local_process_alive)


def _reserve_and_attach(reg, campaign_id, pid, start_time, expected_total=4):
    ok, running = reg.reserve_local(
        campaign_id=campaign_id, config_filter="", runs=1,
        expected_total=expected_total, log_path=reg.log_path_for(campaign_id))
    if ok:
        reg.attach_pid(campaign_id, pid, start_time)
    return ok, running


def _spawn_sleeper(campaign_id="marker"):
    """Start a real child process that sleeps, returning (proc, pid, start_time).

    The campaign id is passed as an argv token so the process cmdline carries it,
    mirroring the real ``vast exec local run --campaign-id <id>`` child that the
    registry's liveness check fingerprints.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time,sys; time.sleep(30)",
         "--campaign-id", campaign_id])
    # Give /proc a moment to expose the pid.
    time.sleep(0.1)
    from robovast.mcp_server.campaign_registry import _proc_start_time
    return proc, proc.pid, _proc_start_time(proc.pid)


def test_single_flight_refuses_second_local_start(tmp_path):
    reg = CampaignRegistry(str(tmp_path))
    proc, pid, start = _spawn_sleeper("camp-a")
    try:
        ok, _ = _reserve_and_attach(reg, "camp-a", pid, start)
        assert ok is True

        # A second start while the first is live must be refused.
        ok2, running = _reserve_and_attach(reg, "camp-b", pid, start)
        assert ok2 is False
        assert running and running[0]["campaign_id"] == "camp-a"
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_stale_pid_recovery_allows_new_launch(tmp_path):
    reg = CampaignRegistry(str(tmp_path))
    proc, pid, start = _spawn_sleeper("camp-a")
    ok, _ = _reserve_and_attach(reg, "camp-a", pid, start)
    assert ok is True

    # Kill the recorded process — its entry must reconcile to a terminal state.
    proc.terminate()
    proc.wait(timeout=5)

    entry = reg.reconcile_and_get("camp-a")
    assert entry["status"] in ("crashed", "finished")

    # And a fresh launch is now allowed.
    proc2, pid2, start2 = _spawn_sleeper("camp-b")
    try:
        ok2, running = _reserve_and_attach(reg, "camp-b", pid2, start2)
        assert ok2 is True, running
    finally:
        proc2.terminate()
        proc2.wait(timeout=5)


def test_pid_reuse_is_detected_via_start_time(tmp_path):
    reg = CampaignRegistry(str(tmp_path))
    proc, pid, start = _spawn_sleeper("camp-a")
    try:
        _reserve_and_attach(reg, "camp-a", pid, start)
        # Corrupt the recorded start-time to simulate the pid being reused by an
        # unrelated process: the live pid no longer matches our record.
        reg.update("camp-a", proc_start_time="1")
        entry = reg.get("camp-a")
        assert local_process_alive(entry) is False
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_dead_pid_is_not_alive(tmp_path):
    reg = CampaignRegistry(str(tmp_path))
    proc, pid, start = _spawn_sleeper()
    proc.terminate()
    proc.wait(timeout=5)
    entry = {"pid": pid, "proc_start_time": start, "campaign_id": "x"}
    assert local_process_alive(entry) is False


def test_launching_placeholder_blocks_then_expires(tmp_path, monkeypatch):
    from robovast.mcp_server import campaign_registry as cr

    fake_now = {"t": cr.datetime.now()}
    reg = CampaignRegistry(str(tmp_path), now_fn=lambda: fake_now["t"])

    # Reserve but never attach a pid → a bare "launching" placeholder.
    ok, _ = reg.reserve_local(campaign_id="camp-a", config_filter="", runs=1,
                              expected_total=4, log_path=reg.log_path_for("camp-a"))
    assert ok is True

    # Within the grace window it still blocks a second start.
    ok2, running = reg.reserve_local(campaign_id="camp-b", config_filter="", runs=1,
                                     expected_total=4, log_path=reg.log_path_for("camp-b"))
    assert ok2 is False and running[0]["campaign_id"] == "camp-a"

    # After the grace window it is considered dead and no longer blocks.
    fake_now["t"] = fake_now["t"] + __import__("datetime").timedelta(
        seconds=LAUNCH_GRACE_SECONDS + 1)
    ok3, _ = reg.reserve_local(campaign_id="camp-c", config_filter="", runs=1,
                               expected_total=4, log_path=reg.log_path_for("camp-c"))
    assert ok3 is True


def _concurrent_writer(results_dir, campaign_id, n):
    reg = CampaignRegistry(results_dir)
    for i in range(n):
        reg.reserve_cluster(campaign_id=f"{campaign_id}-{i}", config_filter="",
                            runs=1, expected_total=1,
                            log_path=reg.log_path_for(f"{campaign_id}-{i}"))


def test_atomic_writes_under_concurrent_writers(tmp_path):
    # Two processes hammering the flock-guarded registry must not corrupt it or
    # lose entries.
    results_dir = str(tmp_path)
    per_writer = 25
    procs = [
        multiprocessing.Process(target=_concurrent_writer,
                                args=(results_dir, tag, per_writer))
        for tag in ("w1", "w2")
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
        assert p.exitcode == 0

    reg = CampaignRegistry(results_dir)
    entries = reg.entries(reconcile=False)
    assert len(entries) == 2 * per_writer  # no lost updates, file stays valid JSON


def test_cluster_reserve_has_no_single_flight(tmp_path):
    # Cluster campaigns are concurrent by design: two cluster reserves both pass,
    # unlike local (which is single-flighted).
    reg = CampaignRegistry(str(tmp_path))
    ok1, _ = reg.reserve_cluster(campaign_id="c1", config_filter="", runs=1,
                                 expected_total=1, log_path=reg.log_path_for("c1"))
    ok2, _ = reg.reserve_cluster(campaign_id="c2", config_filter="", runs=1,
                                 expected_total=1, log_path=reg.log_path_for("c2"))
    assert ok1 is True and ok2 is True
    backends = {e["backend"] for e in reg.entries(reconcile=False)}
    assert backends == {"cluster"}


def test_reserved_control_dir_layout(tmp_path):
    reg = CampaignRegistry(str(tmp_path))
    reg.ensure_dirs()
    assert (tmp_path / "_control").is_dir()
    assert (tmp_path / "_control" / "logs").is_dir()
    assert str(reg.log_path_for("camp-a")).endswith("_control/logs/camp-a.log")
