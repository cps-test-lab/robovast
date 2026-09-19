# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The uploader container's protocol, run as the shell it is.

A scenario pod delivers its ``/out`` once, from one container, after every other container
has written its done marker (``robovast.execution.cluster_execution.pod_upload``). What
has to hold is behavioural -- when the upload starts, how often the pipeline runs, when it
stops trying -- so these run the rendered script under a POSIX shell with a scripted
``curl`` on PATH and a scratch directory standing in for ``/ipc`` and ``/out``.
"""

import os
import shutil
import signal
import subprocess
import tarfile
import time

import pytest

from robovast.execution.cluster_execution import pod_upload
from robovast.execution.cluster_execution.pod_upload import uploader_script

#: The shell the script runs under. The sidecar image's is busybox ash, which has
#: ``pipefail``; on a host whose ``sh`` lacks it (dash) bash in POSIX mode stands in, so the
#: test is about the script and never about the host's shell. Not the host's busybox: a
#: distribution's build may run its own applets in place of what PATH names, and its
#: ``tar`` is not the GNU tar the image installs.
_SHELLS = (["sh"], ["bash", "--posix"])


def _shell() -> list:
    for candidate in _SHELLS:
        if shutil.which(candidate[0]) is None:
            continue
        probe = subprocess.run(candidate + ["-c", "set -o pipefail"], check=False,
                               capture_output=True)
        if probe.returncode == 0:
            return candidate
    pytest.skip("no POSIX shell with pipefail on this host")


#: A ``curl`` that follows a plan: one word per call, in order, and the last word for every
#: call after the plan runs out. ``ok`` succeeds; ``refused`` fails as a connection curl
#: could not make; ``http<status>`` is what ``-f`` does with that response. Every call
#: consumes its body into ``body.<n>`` so ``tar`` never sees a closed pipe.
_FAKE_CURL = r'''#!/bin/sh
set -u
n=$(cat "$CURL_RECORD/count" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "$CURL_RECORD/count"
echo "$@" > "$CURL_RECORD/args.$n"
cat > "$CURL_RECORD/body.$n"
set -- $CURL_PLAN
i=1
while [ "$i" -lt "$n" ] && [ "$#" -gt 1 ]; do shift; i=$((i + 1)); done
case "$1" in
    ok) exit 0 ;;
    refused) echo "curl: (7) Failed to connect" >&2; exit 7 ;;
    http*) echo "curl: (22) The requested URL returned error: ${1#http}" >&2; exit 22 ;;
    *) echo "fake curl: unknown plan word $1" >&2; exit 99 ;;
esac
'''


class _Pod:
    """A scratch ``/ipc`` and ``/out``, the fake curl, and the means to run the script."""

    def __init__(self, tmp_path, plan: str):
        self.ipc = tmp_path / "ipc"
        self.out = tmp_path / "out"
        self.record = tmp_path / "curl"
        bin_dir = tmp_path / "bin"
        for d in (self.ipc, self.out, self.record, bin_dir):
            d.mkdir()
        fake = bin_dir / "curl"
        fake.write_text(_FAKE_CURL)
        fake.chmod(0o755)
        (self.out / "cfg" / "0").mkdir(parents=True)
        (self.out / "cfg" / "0" / "test.xml").write_text("<testsuite/>")
        self.env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "CURL_RECORD": str(self.record),
            "CURL_PLAN": plan,
            "ROBOVAST_DATA_URL": "http://robovast.example.com/data",
            "ROBOVAST_TOKEN": "t",
            pod_upload.IPC_DIR_ENV: str(self.ipc),
            pod_upload.OUT_DIR_ENV: str(self.out),
        }

    def start(self, script: str) -> subprocess.Popen:
        return subprocess.Popen(_shell() + ["-c", script], env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def calls(self) -> int:
        count = self.record / "count"
        return int(count.read_text()) if count.exists() else 0

    def args(self, n: int) -> str:
        return (self.record / f"args.{n}").read_text()

    def members(self, n: int) -> set:
        with tarfile.open(self.record / f"body.{n}", "r:gz") as tar:
            return {m.name for m in tar.getmembers()}

    def mark(self, name: str) -> None:
        (self.ipc / f"done.{name}").touch()


def _finish(proc: subprocess.Popen, timeout: float = 20.0) -> tuple:
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
        pytest.fail(f"the uploader did not finish:\n{out}")
    return proc.returncode, out


def _wait_until(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


# -- markers ----------------------------------------------------------------------------

def test_the_markers_gate_the_upload(tmp_path):
    """Nothing is delivered until every named container has said it is finished."""
    pod = _Pod(tmp_path, "ok")
    proc = pod.start(uploader_script("c-1", ["simulation"], grace_s=60,
                                     attempts=1, backoff_s=0))
    time.sleep(1.5)
    assert pod.calls() == 0, "uploaded before any marker existed"
    pod.mark("main")
    time.sleep(1.5)
    assert pod.calls() == 0, "uploaded with the sidecar's marker still missing"
    pod.mark("simulation")
    rc, out = _finish(proc)
    assert rc == 0, out
    assert pod.calls() == 1
    assert "every container has finished writing: main simulation" in out
    assert pod.members(1) >= {"./cfg/0/test.xml"}


def test_a_missing_sidecar_marker_is_waited_for_and_then_skipped(tmp_path):
    """A sidecar that never signs off must not hold the result hostage: the grace runs
    from ``done.main``, and the upload proceeds saying which marker never came."""
    pod = _Pod(tmp_path, "ok")
    proc = pod.start(uploader_script("c-1", ["simulation", "sut"], grace_s=2,
                                     attempts=1, backoff_s=0))
    pod.mark("main")
    pod.mark("sut")
    time.sleep(1.0)
    assert pod.calls() == 0, "did not wait the grace out"
    rc, out = _finish(proc)
    assert rc == 0, out
    assert pod.calls() == 1
    assert "WARNING" in out and "without a marker from: simulation" in out
    assert "sut" not in out.split("without a marker from:")[1].splitlines()[0]


def test_main_is_always_waited_for_even_when_not_named(tmp_path):
    """The scenario container's marker starts the grace clock, so it cannot be opted out."""
    script = uploader_script("c-1", ["simulation"], grace_s=1, attempts=1, backoff_s=0)
    assert 'WAIT_FOR="main simulation"' in script
    assert uploader_script("c-1", ["main", "simulation"]).count("main") == script.count("main")


# -- retries ----------------------------------------------------------------------------

def test_a_failed_delivery_reruns_the_whole_pipeline(tmp_path):
    """A streamed body cannot be replayed, so a retry is another ``tar | curl``: two
    refused connections and a success are three complete bodies."""
    pod = _Pod(tmp_path, "refused refused ok")
    proc = pod.start(uploader_script("c-1", [], grace_s=1, attempts=5, backoff_s=0))
    pod.mark("main")
    rc, out = _finish(proc)
    assert rc == 0, out
    assert pod.calls() == 3
    for n in (1, 2, 3):
        assert pod.members(n) >= {"./cfg/0/test.xml"}, f"attempt {n} streamed no body"
    assert out.count("attempt ") == 3


def test_running_out_of_attempts_is_a_failed_container(tmp_path):
    """A lost result must be a failed Job, never a Completed one with nothing in it."""
    pod = _Pod(tmp_path, "refused")
    proc = pod.start(uploader_script("c-1", [], grace_s=1, attempts=3, backoff_s=0))
    pod.mark("main")
    rc, out = _finish(proc)
    assert rc != 0
    assert pod.calls() == 3
    assert "giving up after 3 attempts" in out


@pytest.mark.parametrize("status", ["400", "404", "507"])
def test_a_response_that_would_not_change_stops_the_retries(tmp_path, status):
    """curl's ``-f`` turns an HTTP failure into exit 22 with the status in its message; a
    4xx or a full volume is read out of it and ends the attempts at once."""
    pod = _Pod(tmp_path, f"http{status}")
    proc = pod.start(uploader_script("c-1", [], grace_s=1, attempts=5, backoff_s=0))
    pod.mark("main")
    rc, out = _finish(proc)
    assert rc != 0
    assert pod.calls() == 1, "a terminal status was retried"
    assert f"HTTP {status} would not change on a retry" in out


def test_a_service_being_rolled_is_retried(tmp_path):
    """A 503 from the front while the service restarts is the case the retries exist for."""
    pod = _Pod(tmp_path, "http503 ok")
    proc = pod.start(uploader_script("c-1", [], grace_s=1, attempts=5, backoff_s=0))
    pod.mark("main")
    rc, out = _finish(proc)
    assert rc == 0, out
    assert pod.calls() == 2


def test_the_retry_window_outlasts_a_service_upgrade():
    """A service resuming its campaigns may take its whole startup budget to answer, and an
    uploader that gave up sooner would fail exactly the Jobs that finished meanwhile."""
    from robovast.execution.cluster_execution.service_deploy import (
        STARTUP_PROBE_FAILURE_THRESHOLD, STARTUP_PROBE_PERIOD_SECONDS)
    budget = STARTUP_PROBE_PERIOD_SECONDS * STARTUP_PROBE_FAILURE_THRESHOLD
    assert budget >= 30 * 60
    assert pod_upload.upload_retry_window_s() > budget
    # The schedule the script carries is the one the window was computed from.
    script = uploader_script("c-1", [])
    assert f"ATTEMPTS={pod_upload.UPLOAD_ATTEMPTS}\n" in script
    assert f"BACKOFF_S={pod_upload.UPLOAD_BACKOFF_S}\n" in script


# -- termination ------------------------------------------------------------------------

def test_a_term_before_the_markers_uploads_what_is_there(tmp_path):
    """A pod torn down mid-run still lands its evidence: one upload, and the streams still
    being written left out of it."""
    pod = _Pod(tmp_path, "ok")
    (pod.out / "cfg" / "0" / "run.npz.part").write_bytes(b"\0" * 16)
    proc = pod.start(uploader_script("c-1", ["simulation"], grace_s=60,
                                     attempts=1, backoff_s=0))
    time.sleep(0.5)
    proc.send_signal(signal.SIGTERM)
    rc, out = _finish(proc)
    assert rc == 0, out
    assert pod.calls() == 1
    assert "terminated before the result was delivered" in out
    members = pod.members(1)
    assert "./cfg/0/test.xml" in members
    assert not any(m.endswith(pod_upload.IN_PROGRESS_SUFFIX) for m in members)
    assert f"--exclude='*{pod_upload.IN_PROGRESS_SUFFIX}'" in pod.args(1) or \
        pod_upload.IN_PROGRESS_SUFFIX not in pod.args(1)  # the exclude is tar's, not curl's


def test_the_ordinary_upload_excludes_nothing(tmp_path):
    """Once every marker exists nothing is still being written, so a leftover stream is
    forensics and travels with the rest."""
    pod = _Pod(tmp_path, "ok")
    (pod.out / "cfg" / "0" / "run.npz.part").write_bytes(b"\0" * 16)
    proc = pod.start(uploader_script("c-1", [], grace_s=1, attempts=1, backoff_s=0))
    pod.mark("main")
    rc, out = _finish(proc)
    assert rc == 0, out
    assert "./cfg/0/run.npz.part" in pod.members(1)


def test_a_term_during_the_retry_sleep_ends_the_attempts(tmp_path):
    """The backoff sleeps are interruptible: a TERM does not wait a sleep out, and what
    follows is the one upload on termination rather than the rest of the schedule."""
    pod = _Pod(tmp_path, "refused ok")
    proc = pod.start(uploader_script("c-1", [], grace_s=1, attempts=5, backoff_s=30))
    pod.mark("main")
    assert _wait_until(lambda: pod.calls() == 1)
    time.sleep(0.5)
    proc.send_signal(signal.SIGTERM)
    rc, out = _finish(proc, timeout=10.0)
    assert rc == 0, out
    assert pod.calls() == 2
    assert "delivered on termination" in out


# -- the command itself -----------------------------------------------------------------

def test_the_script_is_what_the_pod_runs_and_names_its_route():
    campaign = "camp-2026-01-01-abc"
    command = pod_upload.uploader_command(campaign, ["simulation", "sut"], 120)
    assert command[:2] == ["sh", "-c"]
    script = command[2]
    assert f'"$ROBOVAST_DATA_URL/campaigns/{campaign}/outputs"' in script
    assert "Authorization: Bearer $ROBOVAST_TOKEN" in script
    assert "GRACE_S=120\n" in script
    assert "mc " not in script


@pytest.mark.parametrize("bad", [[""], ["a b"], ["a/b"]])
def test_a_container_name_that_cannot_be_a_marker_is_refused(bad):
    with pytest.raises(ValueError):
        uploader_script("c-1", bad)


def test_an_uploader_without_a_campaign_is_refused():
    with pytest.raises(ValueError):
        uploader_script("", [])
