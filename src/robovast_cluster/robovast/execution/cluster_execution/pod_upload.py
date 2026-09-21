# Copyright (C) 2026 Frederik Pasch
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

"""The container that delivers a scenario pod's ``/out`` to the data plane.

One container per pod does the upload, once, after every other container has said it is
finished writing -- as opposed to each container mirroring the shared ``/out`` when *it*
happens to finish. The protocol is three parties and one directory:

* the scenario container writes ``/ipc/done.main`` as its last post-run step
  (:data:`robovast.common.execution._CLUSTER_POST_RUN_BLOCK`);
* each sidecar stops its workload on seeing that and writes ``/ipc/done.<name>`` once its
  own monitor has stopped (``secondary_entrypoint.sh``);
* this container waits for every marker it was told to, then streams ``/out`` as one tar
  into ``PUT /campaigns/<id>/outputs`` (:func:`~.pod_access.deliver_command`).

A marker that never comes -- a sidecar killed hard, an image whose entrypoint never ran --
must not hold the result hostage: once ``done.main`` has existed for the grace period the
upload proceeds with what is there, saying which markers were missing.

The upload retries on the schedule every transfer follows
(:data:`~.pod_access.TRANSFER_ATTEMPTS`), because the service it delivers to is rolled by
``vast service upgrade`` while campaigns run, and a streamed body cannot be replayed: every
attempt re-runs the whole ``tar | curl`` pipeline. A 507 is retried too: the results volume is
full, the run's output is sound, and space freed within the retry window is space this
upload lands in -- giving up would throw away a run that already cost its compute. Only a
4xx is terminal: the request itself is wrong, and retrying it would only repeat the
answer. The container exits non-zero when the result could not be delivered, so a lost
result is a failed Job and never a quiet one.

Runs from the sidecar image, so this is POSIX ``sh`` for busybox, with ``curl`` and GNU
``tar``.
"""

from robovast.common.execution import IPC_DIR, MAIN_CONTAINER, done_marker

from .pod_access import TRANSFER_ATTEMPTS, TRANSFER_BACKOFF_S, deliver_command

#: The uploader's name in the pod: a regular container, so the Job is complete only once
#: the results are home, and failed when they could not be delivered.
UPLOADER_CONTAINER = "uploader"

#: What a ``tar | curl`` of a results tree needs: a little CPU and a bounded heap, whatever
#: the tree's size, because both stream and neither compresses.
UPLOADER_RESOURCES = {
    "requests": {"cpu": "100m", "memory": "64Mi"},
    "limits": {"memory": "256Mi"},
}

#: The env a test points at a scratch directory; a pod leaves both at their mounts.
IPC_DIR_ENV = "ROBOVAST_IPC_DIR"
OUT_DIR_ENV = "ROBOVAST_OUT_DIR"
OUT_DIR = "/out"

#: How long a sidecar may take to stop its workload and write its marker after
#: ``done.main`` exists before the upload proceeds without it. Long enough for a simulator
#: to flush a recording on TERM; short enough that a sidecar that died hard costs the
#: result minutes, not the Job's deadline.
UPLOAD_GRACE_SECONDS = 120

#: The pod's ``terminationGracePeriodSeconds`` floor: the window the uploader's TERM
#: handler has to deliver what ``/out`` holds before the kubelet kills it. A bound on a
#: best effort -- a tree larger than the window allows is lost with the pod, and a
#: longer window holds every stopped pod's resources for that much longer.
UPLOAD_TERMINATION_GRACE = 120

#: roqsim's live sample stream, packed into ``run.npz`` at close and unlinked. Excluded
#: only from the upload a TERM forces: a run whose recorder is still writing has no archive
#: to keep beside the stream, and roqsim documents the stream a hard kill leaves as
#: forensics whose signal is the archive's absence. Once every marker exists nothing is
#: still being written, so the ordinary upload excludes nothing.
IN_PROGRESS_SUFFIX = ".part"


_SCRIPT = r'''#!/bin/sh
# The uploader of one scenario pod: see robovast.execution.cluster_execution.pod_upload.
set -u
set -o pipefail

IPC_DIR="${@@IPC_DIR_ENV@@:-@@IPC_DIR@@}"
OUT_DIR="${@@OUT_DIR_ENV@@:-@@OUT_DIR@@}"
WAIT_FOR="@@WAIT_FOR@@"
GRACE_S=@@GRACE_S@@
ATTEMPTS=@@ATTEMPTS@@
BACKOFF_S=@@BACKOFF_S@@

log() { echo "[uploader] $*"; }

terminating=""
delivered=""
sleeper=""
on_term() {
    terminating=1
    if [ -n "${sleeper}" ]; then kill "${sleeper}" 2>/dev/null || true; fi
    return 0
}
trap on_term TERM INT

# `sleep &` + `wait`: only `wait` is interruptible by the trap above, so a TERM during a
# bare sleep would be seen only when the sleep ended. The trap also ends the sleeper, and
# the flag is read again once the sleeper is known, so a TERM landing between the flag
# being read and `wait` is not waited out either. The sleeper is ended with the wait, or
# an interrupted one would outlive this script holding its stdout open.
pause() {
    sleep "$1" &
    sleeper=$!
    if [ -n "${terminating}" ]; then
        kill "${sleeper}" 2>/dev/null || true
        return 0
    fi
    wait "${sleeper}" 2>/dev/null || true
    kill "${sleeper}" 2>/dev/null || true
}

marker() { echo "${IPC_DIR}/done.$1"; }

# Every name in WAIT_FOR, or the grace after done.main: the scenario container's own
# marker is what starts the clock, because until the scenario has finished nothing is
# late. Returns 1 when a TERM arrived first.
wait_for_markers() {
    main_seen_at=""
    while :; do
        missing=""
        for name in ${WAIT_FOR}; do
            [ -e "$(marker "${name}")" ] || missing="${missing} ${name}"
        done
        if [ -z "${missing}" ]; then
            log "every container has finished writing: ${WAIT_FOR}"
            return 0
        fi
        if [ -e "$(marker "@@MAIN@@")" ]; then
            [ -n "${main_seen_at}" ] || main_seen_at=$(date +%s)
            if [ $(( $(date +%s) - main_seen_at )) -ge "${GRACE_S}" ]; then
                log "WARNING: proceeding ${GRACE_S}s after the scenario finished without a marker from:${missing}"
                return 0
            fi
        fi
        [ -z "${terminating}" ] || return 1
        pause 1
    done
}

# One run of the pipeline. Its status is curl's, or tar's when tar is what failed
# (pipefail); curl's stderr is kept so a 22 can be read for the HTTP status it stands for.
deliver() {
    ( cd "${OUT_DIR}" && @@DELIVER@@ ) 2>"${IPC_DIR}/.upload.err"
}

deliver_in_progress() {
    ( cd "${OUT_DIR}" && @@DELIVER_IN_PROGRESS@@ ) 2>"${IPC_DIR}/.upload.err"
}

# The HTTP status behind a curl exit 22, or nothing when the failure was not an HTTP one.
http_status() {
    sed -n 's/.*returned error: \([0-9][0-9][0-9]\).*/\1/p' "${IPC_DIR}/.upload.err" | head -n 1
}

# A response that would not change on a retry: the request is wrong (4xx). Everything else
# is worth another attempt -- a refused connection while the service rolls, a 502/503 from
# the front, a reset mid-stream, and a 507: a full results volume takes this upload once
# space is freed.
is_terminal() {
    case "$1" in
        4[0-9][0-9]) return 0 ;;
        *) return 1 ;;
    esac
}

upload_with_retries() {
    attempt=1
    while :; do
        log "delivering ${OUT_DIR} (attempt ${attempt}/${ATTEMPTS})"
        rc=0
        deliver || rc=$?
        if [ "${rc}" -eq 0 ]; then
            log "delivered"
            delivered=1
            return 0
        fi
        status=$(http_status)
        log "ERROR: delivery failed (curl exit ${rc}${status:+, HTTP ${status}}): $(tr '\n' ' ' < "${IPC_DIR}/.upload.err")"
        if [ "${status}" = "507" ]; then
            log "the service's results volume is full; retrying while space is freed"
        fi
        if [ -n "${status}" ] && is_terminal "${status}"; then
            log "ERROR: HTTP ${status} would not change on a retry; giving up"
            return 1
        fi
        [ -z "${terminating}" ] || return 1
        if [ "${attempt}" -ge "${ATTEMPTS}" ]; then
            log "ERROR: giving up after ${ATTEMPTS} attempts"
            return 1
        fi
        pause $(( attempt * BACKOFF_S ))
        [ -z "${terminating}" ] || return 1
        attempt=$(( attempt + 1 ))
    done
}

# A TERM before the result was delivered: the pod is being torn down -- a stop, a restart
# the runner acted on, a deadline -- and whatever /out holds is the evidence of why. One
# attempt, with the streams still being written left out.
upload_on_term() {
    log "WARNING: terminated before the result was delivered; uploading what ${OUT_DIR} holds"
    rc=0
    deliver_in_progress || rc=$?
    if [ "${rc}" -eq 0 ]; then
        log "delivered on termination"
        return 0
    fi
    log "ERROR: delivery on termination failed (curl exit ${rc}): $(tr '\n' ' ' < "${IPC_DIR}/.upload.err")"
    return 1
}

if wait_for_markers && upload_with_retries; then
    exit 0
fi
if [ -n "${terminating}" ] && [ -z "${delivered}" ]; then
    upload_on_term && exit 0
fi
exit 1
'''


def uploader_script(campaign_id: str, wait_for: "list[str]", grace_s: int = UPLOAD_GRACE_SECONDS,
                    *, attempts: int = TRANSFER_ATTEMPTS,
                    backoff_s: int = TRANSFER_BACKOFF_S) -> str:
    """The uploader container's command, as one ``sh`` script.

    *wait_for* names the sidecars whose ``done.<name>`` markers gate the upload; the
    scenario container's ``done.main`` is always waited for, and is the marker that starts
    the *grace_s* clock after which the upload proceeds without the rest. *attempts* and
    *backoff_s* are the retry schedule and exist so a test can run it in seconds; a pod
    takes the defaults, which
    :func:`~.pod_access.transfer_retry_window_s` sizes.
    """
    if not campaign_id:
        raise ValueError("an uploader needs the campaign it delivers to")
    names = [MAIN_CONTAINER] + [n for n in wait_for if n != MAIN_CONTAINER]
    for name in names:
        if not name or any(c.isspace() for c in name) or "/" in name:
            raise ValueError(f"not a container name: {name!r}")
    route = f"/campaigns/{campaign_id}/outputs"
    return (_SCRIPT
            .replace("@@IPC_DIR_ENV@@", IPC_DIR_ENV)
            .replace("@@OUT_DIR_ENV@@", OUT_DIR_ENV)
            .replace("@@IPC_DIR@@", IPC_DIR)
            .replace("@@OUT_DIR@@", OUT_DIR)
            .replace("@@MAIN@@", MAIN_CONTAINER)
            .replace("@@WAIT_FOR@@", " ".join(names))
            .replace("@@GRACE_S@@", str(int(grace_s)))
            .replace("@@ATTEMPTS@@", str(int(attempts)))
            .replace("@@BACKOFF_S@@", str(int(backoff_s)))
            .replace("@@DELIVER@@", deliver_command(".", route))
            .replace("@@DELIVER_IN_PROGRESS@@",
                     deliver_command(".", route, exclude=(f"*{IN_PROGRESS_SUFFIX}",))))


def uploader_command(campaign_id: str, wait_for: "list[str]",
                     grace_s: int = UPLOAD_GRACE_SECONDS) -> list:
    """The ``command`` of the uploader container: the script, handed to ``sh``."""
    return ["sh", "-c", uploader_script(campaign_id, wait_for, grace_s)]


__all__ = ["IN_PROGRESS_SUFFIX", "IPC_DIR_ENV", "OUT_DIR", "OUT_DIR_ENV", "UPLOADER_CONTAINER",
           "UPLOADER_RESOURCES", "UPLOAD_GRACE_SECONDS", "UPLOAD_TERMINATION_GRACE", "done_marker",
           "uploader_command", "uploader_script"]
