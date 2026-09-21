# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Every transfer between a pod and the data plane retries on one schedule, and a fetch
retries its whole pipeline.

A fetch is ``curl | tar`` onto a mount (``pod_access.fetch_command``). The service it reads
from is rolled by ``vast service upgrade`` while campaigns run, so a fetch meets refused
connections and streams cut short, and has to outlast them. What has to hold is
behavioural -- how often the pipeline runs, what ends up on the mount, what the container
exits with -- so these run the rendered shell under ``sh`` with a scripted ``curl`` on PATH
that serves a real archive, and GNU ``tar`` extracting it, as in the sidecar image.
"""

import io
import os
import subprocess
import tarfile

import pytest

from robovast.execution.cluster_execution import pod_access
from robovast.execution.cluster_execution.pod_upload import uploader_script

#: A ``curl`` that follows a plan: one word per call, in order, and the last word for every
#: call after the plan runs out. ``ok`` serves the archive; ``cut`` serves its first half
#: and fails as a transfer closed early; ``garbage`` serves bytes that are no archive and
#: succeeds; ``refused`` fails as a connection curl could not make; ``http<status>`` is what
#: ``-f`` does with that response.
_FAKE_CURL = r'''#!/bin/sh
set -u
n=$(cat "$CURL_RECORD/count" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "$CURL_RECORD/count"
echo "$@" > "$CURL_RECORD/args.$n"
set -- $CURL_PLAN
i=1
while [ "$i" -lt "$n" ] && [ "$#" -gt 1 ]; do shift; i=$((i + 1)); done
case "$1" in
    ok) cat "$FAKE_ARCHIVE"; exit 0 ;;
    cut) head -c "$(( $(wc -c < "$FAKE_ARCHIVE") / 2 ))" "$FAKE_ARCHIVE"
         echo "curl: (18) transfer closed with outstanding read data remaining" >&2; exit 18 ;;
    garbage) printf 'this is not a tar archive at all%01024d' 0; exit 0 ;;
    refused) echo "curl: (7) Failed to connect" >&2; exit 7 ;;
    http*) echo "curl: (22) The requested URL returned error: ${1#http}" >&2; exit 22 ;;
    *) echo "fake curl: unknown plan word $1" >&2; exit 99 ;;
esac
'''

#: Two members large enough that half the archive ends inside the second one's data, so a
#: cut stream leaves a file that is there but short.
_FILES = {"_config/c.vast": os.urandom(64 * 1024), "cfg/0/rosbag2/b.mcap": os.urandom(256 * 1024)}


class _Pod:
    """A scratch mount, the fake curl, and the means to run a fetch against them."""

    def __init__(self, tmp_path, plan: str):
        self.dest = tmp_path / "mount"
        self.record = tmp_path / "curl"
        bin_dir = tmp_path / "bin"
        for d in (self.record, bin_dir):
            d.mkdir()
        (bin_dir / "curl").write_text(_FAKE_CURL)
        (bin_dir / "curl").chmod(0o755)
        archive = tmp_path / "archive.tar"
        with tarfile.open(archive, "w") as tar:
            for name, data in _FILES.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        self.env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "CURL_RECORD": str(self.record),
            "CURL_PLAN": plan,
            "FAKE_ARCHIVE": str(archive),
            pod_access.DATA_URL_ENV: "http://robovast.example.com/data",
            pod_access.TOKEN_ENV: "t",
        }

    def fetch(self, *, attempts: int = 5, then: str = "") -> subprocess.CompletedProcess:
        script = pod_access.fetch_command("/campaigns/c-1/archive", str(self.dest), "stage=true",
                                          attempts=attempts, backoff_s=0)
        return subprocess.run(["sh", "-c", script + then], env=self.env, capture_output=True,
                              text=True, timeout=30, check=False)

    def calls(self) -> int:
        count = self.record / "count"
        return int(count.read_text()) if count.exists() else 0

    def landed_whole(self) -> bool:
        return all((self.dest / name).is_file() and (self.dest / name).read_bytes() == data
                   for name, data in _FILES.items())


def test_a_stream_cut_short_is_fetched_again_whole(tmp_path):
    """The second attempt is a fresh ``curl | tar``, not curl resending into the pipe the
    first attempt already half-filled -- which tar refuses as a header in the middle of an
    archive. It extracts over what the cut left, and every file ends up whole."""
    pod = _Pod(tmp_path, "cut ok")
    result = pod.fetch()
    assert result.returncode == 0, result.stderr
    assert pod.calls() == 2
    assert pod.landed_whole()
    assert "(18)" in result.stderr and "attempt 1/5" in result.stderr


def test_a_service_being_rolled_is_waited_for(tmp_path):
    """Cut mid-stream, then refused while the service restarts, then served."""
    pod = _Pod(tmp_path, "cut refused refused ok")
    result = pod.fetch()
    assert result.returncode == 0, result.stderr
    assert pod.calls() == 4
    assert pod.landed_whole()


def test_a_5xx_is_retried(tmp_path):
    pod = _Pod(tmp_path, "http503 ok")
    assert pod.fetch().returncode == 0
    assert pod.calls() == 2


@pytest.mark.parametrize("status", ["401", "403", "404"])
def test_a_response_that_would_not_change_stops_the_retries(tmp_path, status):
    """A 4xx says the request is wrong, and a retry would only repeat the answer."""
    pod = _Pod(tmp_path, f"http{status}")
    result = pod.fetch()
    assert result.returncode == 22
    assert pod.calls() == 1
    assert f"returned error: {status}" in result.stderr


def test_running_out_of_attempts_exits_with_curls_code(tmp_path):
    """The code names the transfer's failure, not tar's reaction to an empty stream."""
    pod = _Pod(tmp_path, "refused")
    result = pod.fetch(attempts=3)
    assert result.returncode == 7
    assert pod.calls() == 3


def test_a_whole_stream_that_will_not_extract_is_not_retried(tmp_path):
    """The transfer succeeded, so fetching it again would bring the same bytes: tar's
    failure is the node's, and the code is tar's."""
    pod = _Pod(tmp_path, "garbage")
    result = pod.fetch()
    assert result.returncode == 2
    assert pod.calls() == 1


def test_a_fetch_composes_with_what_follows_it(tmp_path):
    """Callers chain a fetch with ``&&``: what follows runs once the fetch succeeded, after
    any retries, and not at all when it failed."""
    marker = tmp_path / "next-ran"
    for sub, plan, code, ran in (("ok", "cut ok", 0, True), ("failed", "http404", 22, False)):
        (tmp_path / sub).mkdir()
        pod = _Pod(tmp_path / sub, plan)
        assert pod.fetch(then=f" && touch {marker}").returncode == code
        assert marker.exists() is ran
        marker.unlink(missing_ok=True)


def test_curl_never_retries_on_its_own(tmp_path):
    """A retry inside curl resends a stream into a pipe it cannot rewind."""
    pod = _Pod(tmp_path, "ok")
    assert pod.fetch().returncode == 0
    assert "--retry" not in (pod.record / "args.1").read_text()


def test_the_retry_window_outlasts_a_service_upgrade():
    """A service resuming its campaigns may take its whole startup budget to answer, and a
    pod that gave up sooner would fail exactly the Jobs starting or finishing meanwhile."""
    from robovast.execution.cluster_execution.service_deploy import (
        STARTUP_PROBE_FAILURE_THRESHOLD, STARTUP_PROBE_PERIOD_SECONDS)
    budget = STARTUP_PROBE_PERIOD_SECONDS * STARTUP_PROBE_FAILURE_THRESHOLD
    assert budget >= 30 * 60
    assert pod_access.transfer_retry_window_s() > budget
    # Both transfers carry the schedule the window was computed from.
    upload = uploader_script("c-1", [])
    assert f"ATTEMPTS={pod_access.TRANSFER_ATTEMPTS}\n" in upload
    assert f"BACKOFF_S={pod_access.TRANSFER_BACKOFF_S}\n" in upload
    fetch = pod_access.fetch_command("/campaigns/c-1/archive", "/campaign")
    assert f'-lt {pod_access.TRANSFER_ATTEMPTS} ]' in fetch
    assert f"attempt * {pod_access.TRANSFER_BACKOFF_S}" in fetch
