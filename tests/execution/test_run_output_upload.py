# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""What a scenario pod's containers do so that its ``/out`` can be delivered once.

No container of the pod uploads anything itself. The scenario container writes
``/ipc/done.main`` after its own cleanup, each sidecar stops its workload on seeing that and
writes ``/ipc/done.<name>``, and the pod's uploader container (``pod_upload``) delivers
``/out`` once every marker exists. These tests hold the two entrypoints to their half of
that protocol -- by reading what they render, and by running the rendered shell where the
property is one of timing rather than of text.
"""

import os
import re
import signal
import subprocess
import time

import pytest

from robovast.common import execution
from robovast.common.execution import (MAIN_CONTAINER, done_marker, render_entrypoint,
                                       render_secondary_entrypoint)

_UPLOAD_WORDS = ("mc mirror", "s3_upload", "S3_ENDPOINT", "x-amz-meta")


def _wait_for(path, timeout=10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(path):
            return True
        time.sleep(0.05)
    return os.path.exists(path)


# -- what is rendered -------------------------------------------------------------------

@pytest.mark.parametrize("cluster", [True, False], ids=["cluster", "local"])
def test_no_container_uploads_from_its_entrypoint(cluster):
    for script in (render_entrypoint(cluster=cluster),
                   render_secondary_entrypoint(cluster=cluster)):
        for word in _UPLOAD_WORDS:
            assert word not in script


def test_the_cluster_post_run_is_cleanup_then_the_marker():
    """Order is the point: the marker says the container's files are complete, so it goes
    after the cleanup that stops the resource monitor and the rosbag."""
    block = execution._CLUSTER_POST_RUN_BLOCK
    assert re.search(r'POST_COMMAND_PARAM="--post-run \$\{BUILTIN_CLEANUP_SCRIPT\} '
                     r'--post-run \$\{MARK_DONE_SCRIPT\}"', block)
    assert 'touch "${_marker}"' in block and 'done.main' in block
    assert done_marker(MAIN_CONTAINER) == "/ipc/done.main"


def test_the_local_lane_renders_no_marker_and_execs_the_runner():
    """/out is a bind mount there and nothing waits for a marker, so the runner simply
    replaces the shell."""
    local = render_entrypoint(cluster=False)
    assert "done.main" not in local
    assert "MARK_DONE_SCRIPT" not in local
    assert re.search(r"run_scenario\(\) \{\s+exec \"\$@\"", local)


def test_the_cluster_lane_needs_no_extra_tool():
    """The experiment image carries nothing that reaches storage: the transfer is the
    sidecar image's, in the pod's init and uploader containers."""
    assert execution._CLUSTER_INIT_BLOCK == 'EXTRA_REQUIRED_TOOLS=""'
    assert "mc" not in execution._CLUSTER_INIT_BLOCK.split('"')[1].split()


# -- the scenario container, run -------------------------------------------------------

def _run_cluster_post_run(tmp_path, command: str, *, term_after: float | None = None):
    """The cluster lane's post-run block as a script of its own: the lane's shell around
    the runner, with the runner replaced by *command*.

    The block is what the entrypoint substitutes verbatim, so what it does with a runner
    that crashes, or is signalled, is exactly what a pod's scenario container does.
    """
    ipc = tmp_path / "ipc"
    ipc.mkdir()
    script = "\n".join([
        "set -e",
        "log() { echo \"[entrypoint] $*\"; }",
        'POST_COMMAND=""',
        execution._CLUSTER_POST_RUN_BLOCK,
        f"run_scenario {command}",
    ])
    env = {**os.environ, "IPC_DIR": str(ipc), "TMPDIR": str(tmp_path)}
    # The block writes its helper scripts under /tmp by name; point them at the scratch
    # tree so a test never touches the host's.
    script = script.replace('"/tmp/', f'"{tmp_path}/')
    proc = subprocess.Popen(["bash", "-c", script], env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    if term_after is not None:
        time.sleep(term_after)
        proc.send_signal(signal.SIGTERM)
    try:
        out, _ = proc.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
        pytest.fail(f"the entrypoint did not finish:\n{out}")
    return proc.returncode, out, ipc / "done.main"


def test_a_runner_that_finishes_leaves_the_marker(tmp_path):
    rc, out, marker = _run_cluster_post_run(tmp_path, "true")
    assert rc == 0, out
    assert marker.exists()
    assert "Cleanup finished" in out


def test_a_runner_that_crashes_still_leaves_the_marker(tmp_path):
    """The runner's own --post-run hooks never run for a scenario that produced no result;
    the shell around it writes the marker anyway, after the cleanup, and keeps the
    runner's exit status so the container still fails."""
    rc, out, marker = _run_cluster_post_run(tmp_path, "sh -c 'exit 3'")
    assert rc == 3, out
    assert marker.exists()
    assert out.index("Cleanup finished") < out.index("Wrote")


def test_a_runner_killed_hard_still_leaves_the_marker(tmp_path):
    """An OOM kill takes the runner, not the shell: SIGKILL to the child."""
    rc, out, marker = _run_cluster_post_run(tmp_path, "sh -c 'kill -9 $$'")
    assert rc == 137, out
    assert marker.exists()


def test_a_term_to_the_container_reaches_the_runner_and_leaves_the_marker(tmp_path):
    """tini forwards the kubelet's TERM to the shell only; the shell forwards it to the
    runner, waits for it to go, and then writes the marker -- a torn-down run still says
    it has finished writing."""
    started = time.monotonic()
    rc, out, marker = _run_cluster_post_run(tmp_path, "sleep 30", term_after=0.5)
    assert time.monotonic() - started < 10, "the TERM was not forwarded to the runner"
    assert rc == 143, out
    assert marker.exists()


# -- a sidecar, run ---------------------------------------------------------------------

def _start_sidecar(tmp_path, name: str, command: str) -> tuple:
    """The rendered secondary entrypoint, running *command* as its workload.

    It runs as shipped: the resource monitor it starts and the sysinfo it records are
    scripts it expects at ``/config``, which are absent here and are tolerated by the
    script itself (their absence is not the sidecar's failure).
    """
    ipc = tmp_path / "ipc"
    out = tmp_path / "out"
    ipc.mkdir()
    out.mkdir()
    script = tmp_path / "secondary_entrypoint.sh"
    script.write_text(render_secondary_entrypoint(cluster=True))
    env = {**os.environ, "IPC_DIR": str(ipc), "OUTPUT_DIR": str(out), "CONTAINER_NAME": name,
           "ROBOVAST_CONTAINER_COMMAND": command}
    env.pop("ROS_DISTRO", None)
    proc = subprocess.Popen(["bash", str(script)], env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    return proc, ipc, out


def _end(proc) -> tuple:
    try:
        out, _ = proc.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
        pytest.fail(f"the sidecar did not finish:\n{out}")
    return proc.returncode, out


def test_a_sidecar_stops_its_workload_when_the_scenario_is_done_and_signs_off(tmp_path):
    """A simulator never exits on its own; the scenario's marker is what ends it, and the
    sidecar's own marker follows once the workload is gone -- then it holds, because a
    native sidecar that exits is restarted."""
    proc, ipc, _ = _start_sidecar(tmp_path, "simulation", "sleep 60")
    time.sleep(1.5)
    assert not (ipc / "done.simulation").exists(), "signed off while the workload ran"
    (ipc / "done.main").touch()
    assert _wait_for(ipc / "done.simulation"), "did not sign off after the scenario finished"
    assert proc.poll() is None, "a native sidecar must hold rather than exit"
    proc.send_signal(signal.SIGTERM)
    rc, out = _end(proc)
    assert rc == 0, out
    assert "stopping the simulation workload" in out
    assert "has finished writing" in out


def test_a_sidecar_whose_workload_exits_early_waits_for_the_scenario(tmp_path):
    """The scenario-execution server exits when its client disconnects, before the
    scenario container has finished. The sidecar signs off only on the scenario's
    marker, so its monitor runs to the end of the trial."""
    proc, ipc, _ = _start_sidecar(tmp_path, "sut", "true")
    time.sleep(1.5)
    assert proc.poll() is None
    assert not (ipc / "done.sut").exists()
    (ipc / "done.main").touch()
    assert _wait_for(ipc / "done.sut")
    proc.send_signal(signal.SIGTERM)
    rc, out = _end(proc)
    assert rc == 0, out
    assert "holding so the kubelet does not restart it" in out


def test_a_sidecar_torn_down_before_the_scenario_ends_still_signs_off(tmp_path):
    """The kubelet's TERM ends the workload and the marker is written on the way out, so
    an uploader that outlives this container is not left waiting on it."""
    proc, ipc, _ = _start_sidecar(tmp_path, "simulation", "sleep 60")
    time.sleep(1.0)
    proc.send_signal(signal.SIGTERM)
    rc, out = _end(proc)
    # The status is the workload's, which the TERM ended; the pod is being torn down, so
    # nothing reads it as a failure. The marker is what has to be there.
    assert rc == 143, out
    assert (ipc / "done.simulation").exists()


def test_a_sidecar_whose_workload_fails_exits_without_holding(tmp_path):
    """A failure is a true signal: the kubelet's restart is what invalidates the trial."""
    proc, ipc, _ = _start_sidecar(tmp_path, "simulation", "sh -c 'exit 2'")
    rc, out = _end(proc)
    assert rc == 2, out
    assert (ipc / "done.simulation").exists()
