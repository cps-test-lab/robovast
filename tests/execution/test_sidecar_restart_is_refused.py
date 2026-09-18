# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A sidecar container that the kubelet restarts starts NOTHING the second time.

A native sidecar carries ``restartPolicy: Always`` -- the only policy the API allows it, and the
shape that starts the simulator before the scenario and ends the pod with it -- so a workload
container that dies IS restarted, and the restart itself cannot be refused. What can be refused
is the workload: a simulator brought back mid-trial starts a fresh world under a stack still
running the old one. The trial ended when the first instance died; the second says so, keeps the
evidence, and waits for the runner to end the job.

Run through a real bash on the shipped script, twice against one ``IPC_DIR`` -- the per-pod
scratch space the marker lives in -- because the guard is a few lines of shell whose whole
value is in what they do at runtime, and a test that grepped for them would pass with the
logic inverted.
"""

import os
import signal
import subprocess
import time

import pytest

from robovast.common.execution import render_secondary_entrypoint

pytestmark = pytest.mark.skipif(
    any(subprocess.run(["which", tool], capture_output=True, check=False).returncode
        for tool in ("bash", "stdbuf", "tee", "python3")),
    reason="needs the tools the entrypoint itself requires")


def _start(tmp_path, script, ipc_dir, workload):
    env = {**os.environ,
           "OUTPUT_DIR": str(tmp_path / "out"),
           "CONTAINER_NAME": "simulation",
           "IPC_DIR": str(ipc_dir),
           "ROBOVAST_CONTAINER_COMMAND": workload}
    # Its own process group, so the signal reaches bash and not this test runner.
    return subprocess.Popen(["bash", str(script)], env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, start_new_session=True)


def _wait_for(path, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


def _end(proc, timeout=10.0):
    os.killpg(proc.pid, signal.SIGTERM)
    out, _ = proc.communicate(timeout=timeout)
    return out


def test_a_second_instance_in_the_same_pod_does_not_start_the_workload(tmp_path):
    script = tmp_path / "secondary_entrypoint.sh"
    script.write_text(render_secondary_entrypoint(cluster=True))
    ipc = tmp_path / "ipc"
    ipc.mkdir()
    ran = tmp_path / "ran"

    # The first instance: starts its workload and leaves the marker behind.
    first = _start(tmp_path, script, ipc, f"touch {ran}")
    assert _wait_for(ran), "the first instance must run the workload"
    assert (ipc / ".simulation.started").exists(), "and mark that this pod has run it"
    _end(first)
    ran.unlink()

    # The kubelet's restart: same pod, same /ipc, same command. Nothing may run.
    second = _start(tmp_path, script, ipc, f"touch {ran}")
    log = tmp_path / "out" / "logs" / "system_simulation.log"
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and "restarted container" not in log.read_text():
        time.sleep(0.05)
    assert second.poll() is None, "it holds for the runner rather than exiting into another restart"
    out = _end(second)

    assert not ran.exists(), "the workload was started again"
    assert "ERROR" in out and "restarted container" in out
    assert "died with it" in out, "the log must say the trial is over, not merely that it restarted"


def test_the_marker_names_this_pod_and_not_the_job(tmp_path):
    """A fresh pod -- a fresh emptyDir -- starts clean. Locally the tmpfs is per job; both mean the
    marker cannot leak into the next trial and refuse a workload that never ran in it."""
    script = tmp_path / "secondary_entrypoint.sh"
    script.write_text(render_secondary_entrypoint(cluster=True))
    ran = tmp_path / "ran"
    for pod in ("pod-a", "pod-b"):
        ipc = tmp_path / pod
        ipc.mkdir()
        proc = _start(tmp_path, script, ipc, f"touch {ran}")
        assert _wait_for(ran), f"{pod}: a first instance must always run its workload"
        _end(proc)
        ran.unlink()
