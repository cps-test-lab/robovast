#!/usr/bin/env python3
# Copyright (C) 2025 Frederik Pasch
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

"""Shared cluster-job utilities.

Helpers used across the cluster CLI (``monitor``, ``run-cleanup``,
``upload-to-share``), the controller launcher's orphan reaping, and cluster
teardown — independent of how scenario Jobs are produced. The job-manifest
toolkit that actually builds/submits Jobs lives in
:mod:`.kubernetes_backend` (the in-cluster controller is the sole executor).
"""

import contextlib
import logging
import re
import signal as _signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from kubernetes import client

from robovast.common.config import SCENARIO_CONTAINER
from robovast.common.execution import node_label
from robovast.common.log_tail import MergedLogBuffer, tag_width

from .kube_client import pod_workload_containers
from .kubernetes_kueue import (cleanup_campaign_priority_classes, cleanup_kueue_workloads,
                               cluster_queue_held)
from .manifests import MAIN_CONTAINER_NAME

logger = logging.getLogger(__name__)


def _label_safe_campaign(campaign: str) -> str:
    """Convert campaign to a valid Kubernetes label value.

    Label values must be 63 chars or less, alphanumeric, hyphens, periods.
    """
    s = campaign.lower().replace("_", "-")
    return "".join(c for c in s if c.isalnum() or c in "-.")[:63]


def job_phase(job, pod_phases=None) -> str:
    """Classify a scenario-run Job into ``completed``/``running``/``failed``/``pending``.

    Shared by the aggregate counter and the per-job lister so the two never drift.

    A Job's ``status.active`` counts pods that are Pending *or* Running, so a Job whose
    pod is still unscheduled / pulling its image / freshly Kueue-admitted looks "active"
    while ``k9s`` shows the pod ``Pending``. When *pod_phases* is supplied (Job name →
    its pod's phase — see :func:`_pod_signals`), an active Job is classified by that pod
    instead, so ``pending`` means exactly one thing: **the pod has not started yet**.

    Both terminal pod phases are honoured, not just ``Running``, and that is what keeps
    the classification monotone. The Job's ``status.succeeded`` is only incremented once
    the job controller has observed the pod's termination and removed its finalizer,
    seconds after the pod itself reached ``Succeeded``. Reading "active, and its pod is
    not Running" as ``pending`` therefore sent every finishing job *backwards* — a
    finished run showed up as not-yet-started for as long as the controller lagged.
    Trusting the pod is sound here because the scenario Job template (see
    :data:`~.manifests.JOB_TEMPLATE`) is ``backoffLimit: 0`` with the default
    ``completions``/``parallelism`` of 1: one pod, never retried, so that pod's verdict
    *is* the Job's and the Job status is only slower to say so.
    """
    status = job.status
    if status is None:
        return "pending"
    if (status.succeeded or 0) >= 1:
        return "completed"
    if (status.active or 0) >= 1:
        if pod_phases is None:
            return "running"  # no pod truth available — Job-level view
        phase = pod_phases.get(job.metadata.name)
        if phase == "Running":
            return "running"
        if phase == "Succeeded":
            return "completed"
        if phase == "Failed":
            return "failed"
        return "pending"  # no pod yet, or the pod is still Pending
    if (status.failed or 0) >= 1:
        return "failed"
    return "pending"


# Container ``waiting`` reasons that mean a pod will **not** start on its own: the
# image cannot be pulled, or the container cannot be created. Unlike a crash (which
# counts against the Job's ``backoffLimit`` and is eventually marked ``failed``),
# these keep the pod ``Pending`` and the Job ``active`` indefinitely — so a campaign
# hangs with no progress unless the reason is surfaced. Kubernetes' own message
# (attached to each) names the offending image / registry error.
POD_BLOCKED_REASONS = frozenset({
    "ImagePullBackOff", "ErrImagePull", "InvalidImageName",
    "ErrImageNeverPull", "CreateContainerConfigError", "RegistryUnavailable",
})


#: The subset of :data:`POD_BLOCKED_REASONS` that reports on a *pull attempt*, and so the
#: only reasons whose message can name a rate limit (see :func:`image_pull_is_throttled`).
#: A name that cannot be parsed, an image the node may not pull, a container config the
#: kubelet rejects: none of those involve a registry, and none can be transient.
POD_PULL_REASONS = frozenset({"ImagePullBackOff", "ErrImagePull"})


#: Why the scheduler refused to place a pod. Unlike :data:`POD_BLOCKED_REASONS` this is a
#: pod *condition*, and it is the shape every capacity or quota mistake takes: the
#: workload is admitted, the kubelet has nowhere to put it, and the pod sits ``Pending``
#: with its Job ``active``. Unreported, such a batch logs "still running" until
#: ``activeDeadlineSeconds`` fires -- an hour by default -- and then reports
#: ``DeadlineExceeded``, telling a scenario-timeout story about an infrastructure fault.
#: Kubernetes' own message names the missing resource ("0/1 nodes are available: 1
#: Insufficient nvidia.com/gpu"), which is the whole diagnosis.
POD_UNSCHEDULABLE_REASONS = frozenset({"Unschedulable", "SchedulerError"})


#: How long a workload tolerates a pod that cannot start (see :data:`POD_BLOCKED_REASONS`
#: and :data:`POD_UNSCHEDULABLE_REASONS`) before the wait gives up and reports Kubernetes'
#: own reason. A short grace absorbs a transient registry blip while never letting a doomed
#: pull hang indefinitely.
#:
#: Lives here, beside the reasons it is the response to, because three waits need the same
#: number and it was written out separately in each -- the campaign batch loop, the service
#: rollout, and (since a blocked build Job hung ``vast image wait`` forever) the image-build
#: status read. Three copies of a tuned constant are three chances to tune only two.
BLOCKED_GRACE_SECONDS = 60.0


#: The same tolerance, for the causes that *do* recover on their own. Two of them, and
#: both appear when several campaigns run at once and never when one does:
#:
#: * a pod the scheduler refused because the resource it asks for is momentarily held by
#:   something else (see :func:`unschedulable_is_contention`) — the job fits a node, so
#:   it starts as soon as a neighbour finishes;
#: * a pull the kubelet or the registry is rate-limiting (see
#:   :func:`image_pull_is_throttled`) — the image is there and the credential works; the
#:   pull is queued behind the other pulls a batch of jobs asked for at the same instant.
#:
#: Sixty seconds is sized for a registry blip and failed such campaigns for the very
#: conditions that fix themselves; fifteen minutes outlasts a typical trial.
#:
#: Deliberately finite. A pod nobody reports on hangs the batch until
#: ``activeDeadlineSeconds`` fires and then blames a scenario timeout for what was an
#: infrastructure fault — the failure mode :data:`POD_UNSCHEDULABLE_REASONS` exists to
#: prevent. Waiting longer than this means the cluster is oversubscribed, not busy.
CONTENDED_GRACE_SECONDS = 900.0


def _pod_job_name(pod) -> "str | None":
    """The owning Job's name off a pod's label (``batch.kubernetes.io/job-name``,
    older clusters: ``job-name``)."""
    labels = (pod.metadata.labels or {}) if pod.metadata else {}
    return labels.get("batch.kubernetes.io/job-name") or labels.get("job-name")


def resolve_pull_secret(cluster_config, k8s_core, namespace: str) -> str:
    """The Secret a pod needs to pull ITS OWN images, or ``""`` when none is configured.

    The opposite direction from the push credential, and easy to forget for exactly that
    reason: a Job may build or run perfectly while its pod cannot start, because the images it
    is made of -- the sidecar that mirrors a context, the campaign's own execution image --
    live in a registry the kubelet has no credential for. That failure leaves the pod
    ``ImagePullBackOff`` and the Job ``active``, so a waiter polling ``status.failed`` learns
    nothing and reports a timeout.

    Falls back to the push Secret when no pull Secret is named, because ``vast exec cluster
    setup`` writes one dockerconfigjson that serves both directions; the lookup is what proves
    it exists, since naming an absent Secret keeps the pod from starting.

    Everything optional: a deployment on a public registry needs no credential, so any failure
    to determine one yields ``""`` rather than an error.

    The run, exec and warm paths each carry this logic inline (``kubernetes_backend``,
    ``kube_exec_lane``, ``image_warm``). They predate this helper and can move onto it; nothing
    is gained by leaving a fifth copy for the next pod spec that needs one.
    """
    from .service_deploy import REGISTRY_PUSH_SECRET_NAME  # noqa: PLC0415

    try:
        named = cluster_config.get_registry_config().pull_secret_name
        if named:
            return named
    except Exception:  # noqa: BLE001 - registry config is optional
        return ""
    try:
        k8s_core.read_namespaced_secret(REGISTRY_PUSH_SECRET_NAME, namespace)
        return REGISTRY_PUSH_SECRET_NAME
    except Exception:  # noqa: BLE001 - absent, or not readable from here
        return ""


def pod_block_reason(pod) -> "tuple[str, str] | None":
    """``(reason, message)`` if *pod* cannot start on its own, else ``None``.

    Two distinct shapes, both of which leave the pod ``Pending`` and its Job
    ``active`` indefinitely: a container stuck in an unrecoverable ``waiting`` state
    (see :data:`POD_BLOCKED_REASONS`), or a pod the scheduler cannot place (see
    :data:`POD_UNSCHEDULABLE_REASONS`).

    Checks init *and* regular containers; ``message`` is Kubernetes' own text -- the
    failed image ref and registry error, or the scheduler's per-node accounting --
    possibly empty.
    """
    status = pod.status
    if status is None:
        return None
    statuses = list(getattr(status, "init_container_statuses", None) or []) + \
        list(getattr(status, "container_statuses", None) or [])
    for cs in statuses:
        state = getattr(cs, "state", None)
        waiting = getattr(state, "waiting", None) if state else None
        if waiting and getattr(waiting, "reason", None) in POD_BLOCKED_REASONS:
            return waiting.reason, (getattr(waiting, "message", None) or "").strip()
    # Checked after the containers because an unschedulable pod has no container
    # statuses at all -- there is no node on which to create them.
    for cond in (getattr(status, "conditions", None) or []):
        if (getattr(cond, "type", None) == "PodScheduled"
                and str(getattr(cond, "status", None)) == "False"
                and getattr(cond, "reason", None) in POD_UNSCHEDULABLE_REASONS):
            return cond.reason, (getattr(cond, "message", None) or "").strip()
    return None


# Pod-level termination reasons that a user would otherwise dig out of ``kubectl
# describe`` / k9s: the node evicted the pod (memory/disk pressure) or the Job's
# ``activeDeadlineSeconds`` fired. Unlike a normal non-zero scenario exit (whose
# cause is in the run's own log), these truncate the log with no in-log explanation.
POD_TERMINATED_REASONS = frozenset({"Evicted", "DeadlineExceeded"})


def pod_termination_reason(pod) -> "tuple[str, str] | None":
    """``(reason, message)`` explaining an *abnormal* end of *pod*, else ``None``.

    Surfaces only the causes that leave the scenario log unexplained — an
    ``OOMKilled`` container, or a pod-level ``Evicted`` / ``DeadlineExceeded`` — so a
    failed job says *why* without a trip to k9s. A plain non-zero exit is deliberately
    not reported here: that is an ordinary scenario failure whose reason is in the
    run's own log, not an infrastructure event.
    """
    status = pod.status
    if status is None:
        return None
    pod_reason = getattr(status, "reason", None)
    if pod_reason in POD_TERMINATED_REASONS:
        return pod_reason, (getattr(status, "message", None) or "").strip()
    statuses = list(getattr(status, "init_container_statuses", None) or []) + \
        list(getattr(status, "container_statuses", None) or [])
    for cs in statuses:
        state = getattr(cs, "state", None)
        term = getattr(state, "terminated", None) if state else None
        if term and getattr(term, "reason", None) == "OOMKilled":
            cname = getattr(cs, "name", None) or "?"
            return "OOMKilled", f"container {cname} exceeded its memory limit"
    return None


#: Exit codes in ``(128, 192)`` are ``128 + signal`` -- the convention the kubelet reports
#: verbatim. Nothing above 63 is a real signal number, so the upper bound is generous.
_SIGNAL_EXIT_BASE = 128


def _signal_from_exit(exit_code, signal) -> "tuple[int | None, str | None]":
    """``(number, name)`` of the signal a container died on, or ``(None, None)``.

    ``exit 135`` is ``128 + 7``, and reading that as ``SIGBUS`` is the difference between a
    number nobody recognises and the one word that names the failure. Doing it here rather
    than leaving it to whoever reads the record is the point: the translation was done by
    hand once, on a campaign whose pod was already gone.

    ``terminated.signal`` is filled only sometimes, so the exit code is the reliable source
    and the explicit field is the fallback rather than the other way round.
    """
    number = signal or None
    if number is None and isinstance(exit_code, int) and (
            _SIGNAL_EXIT_BASE < exit_code < _SIGNAL_EXIT_BASE + 64):
        number = exit_code - _SIGNAL_EXIT_BASE
    if not number:
        return None, None
    try:
        return number, _signal.Signals(number).name
    except ValueError:
        return number, None


def _container_role(name: str, workload_names: "set[str]") -> str:
    """What this container *is*, in the vocabulary the ``.vast`` uses.

    The pod's single regular container is named ``robovast`` and runs the ``scenario``
    role; every sidecar is named for the role that declared it (``sut``, ``simulation``, or
    an ad-hoc key). Anything outside the workload set is one-shot staging.
    """
    if name == MAIN_CONTAINER_NAME:
        return SCENARIO_CONTAINER
    return name if name in workload_names else "init"


def pod_container_failures(pod) -> "list[dict]":
    """One record per restarted container of *pod*, newest state and all, or ``[]``.

    Everything :func:`pod_restarted_containers` used to format into a sentence and throw
    away. A restart is the only campaign signal whose evidence dies with the pod -- the
    container is gone, its logs are one API call away for a few minutes, and after that the
    whole diagnosis is whatever string got logged. So this captures rather than formats,
    and the formatting is a thin layer on top.

    **Every** restarted container, not just the first. Their order in
    ``init_container_statuses + container_statuses`` is declaration order, not causal
    order, so stopping at the first one reports whichever happened to be declared earlier
    -- and in a pod where the SUT dies and takes the simulator with it, that is a coin
    toss between the cause and the consequence.

    ``invalidating`` marks the ones a campaign should act on: a workload container that
    died non-zero. See :func:`pod_invalidating_restart` for why that is the right cut.

    Pure -- no API calls. Enriching a record with the dead container's own log costs a
    request per container and belongs at the one place that acts on it, not in a probe
    that runs every couple of seconds for every batch.
    """
    status = getattr(pod, "status", None)
    if status is None:
        return []
    workload_names = {getattr(c, "name", None) for c in pod_workload_containers(pod)}
    workload_names.discard(None)
    metadata = getattr(pod, "metadata", None)
    records = []
    statuses = list(getattr(status, "init_container_statuses", None) or []) + \
        list(getattr(status, "container_statuses", None) or [])
    for cs in statuses:
        restart_count = getattr(cs, "restart_count", 0) or 0
        if restart_count < 1:
            continue
        cname = getattr(cs, "name", None) or "?"
        term = getattr(getattr(cs, "last_state", None), "terminated", None)
        exit_code = getattr(term, "exit_code", None) if term else None
        signal_number, signal_name = _signal_from_exit(
            exit_code, getattr(term, "signal", None) if term else None)
        # A container outside the workload set cannot be restarted by this pod's policy
        # (``restartPolicy: Never``), so if one ever is, something is wrong in a way worth
        # recording -- but it is not this pod's trial that was invalidated.
        is_workload = cname in workload_names or not workload_names
        records.append({
            "pod_name": getattr(metadata, "name", None),
            # The machine, hashed: this record is campaign data and ships with it, and
            # a hostname here would reintroduce by another route exactly what the run's
            # own sysinfo stopped recording.
            "node_label": node_label(
                getattr(getattr(pod, "spec", None), "node_name", None)),
            "pod_phase": getattr(status, "phase", None),
            "container": cname,
            "role": _container_role(cname, workload_names),
            "image": getattr(cs, "image", None),
            "image_id": getattr(cs, "image_id", None),
            "restart_count": restart_count,
            "reason": getattr(term, "reason", None) if term else None,
            "exit_code": exit_code,
            "signal": signal_number,
            "signal_name": signal_name,
            "message": getattr(term, "message", None) if term else None,
            "started_at": _isoformat(getattr(term, "started_at", None) if term else None),
            "finished_at": _isoformat(getattr(term, "finished_at", None) if term else None),
            "cpu_limit": _container_limit(pod, cname, "cpu"),
            "memory_limit": _container_limit(pod, cname, "memory"),
            # ``exit_code is None`` means the kubelet has not reported what the previous
            # instance died of. Unknown is not innocent: stay on the strict side, the same
            # way an unreadable node list makes every blocked job unrecoverable.
            "invalidating": is_workload and exit_code != 0,
            "detail": _restart_detail(cname, restart_count, term, signal_name),
        })
    return records


def _isoformat(value) -> "str | None":
    """A Kubernetes timestamp as ISO 8601 text, or ``None``. Never raises on a odd type."""
    if value is None:
        return None
    try:
        return value.isoformat()
    except AttributeError:
        return str(value)


def _container_limit(pod, container_name: str, key: str) -> "str | None":
    """*container_name*'s declared limit for *key*, or ``None`` -- **meaning no limit**.

    Recorded because the absence is a finding, not a blank: a container with no memory
    limit is told by the downward API that it has the whole node, and the memory-backed
    ``/dev/shm`` it shares with its siblings is sized the same way. Reconstructing that
    after the fact means finding the ``.vast`` that ran; reading it off the pod does not.
    """
    for container in list(getattr(getattr(pod, "spec", None), "containers", None) or []) + \
            list(getattr(getattr(pod, "spec", None), "init_containers", None) or []):
        if getattr(container, "name", None) != container_name:
            continue
        limits = getattr(getattr(container, "resources", None), "limits", None) or {}
        value = limits.get(key)
        return None if value is None else str(value)
    return None


def _restart_detail(cname, restart_count, term, signal_name) -> str:
    """``container sut restarted 1x after Error (exit 135, SIGBUS)`` -- the human sentence."""
    detail = f"container {cname} restarted {restart_count}x"
    why = getattr(term, "reason", None) if term else None
    code = getattr(term, "exit_code", None) if term else None
    if why:
        detail += f" after {why}"
    if code is not None:
        detail += f" (exit {code}{f', {signal_name}' if signal_name else ''})"
    return detail


def _format_restarts(records: "list[dict]") -> "tuple[str, str] | None":
    """``(reason, message)`` over *records*, naming how many others there were."""
    if not records:
        return None
    detail = records[0]["detail"]
    if len(records) > 1:
        others = len(records) - 1
        detail += f"; and {others} other container{'s' if others > 1 else ''}"
    return "ContainerRestarted", detail


def pod_restarted_containers(pod) -> "tuple[str, str] | None":
    """``(reason, message)`` if ANY container of *pod* has been restarted, else ``None``.

    The unconditional reading of a restart, kept for the service rollout watcher
    (``service_deploy``), where the pod is a long-lived Deployment replica and a container
    that exits for any reason and comes back IS a crash-loop.

    A campaign job is the other case and wants :func:`pod_invalidating_restart`: its pod is
    one-shot (``backoffLimit: 0``, ``restartPolicy: Never``), its sidecars are native
    sidecars whose clean exit the kubelet answers with a restart, and reading that as a
    crash marked a *passing* trial failed.
    """
    return _format_restarts(pod_container_failures(pod))


def pod_invalidating_restart(pod) -> "tuple[str, str] | None":
    """``(reason, message)`` if a restart of *pod* invalidated the trial, else ``None``.

    The campaign lane's reading, and it turns on the exit code rather than on which
    container it was.

    A campaign pod runs ``restartPolicy: Never``, so its regular container and its one-shot
    ``s3-init`` are never restarted at all: the only containers that *can* restart are the
    native sidecars, which carry ``restartPolicy: Always`` so that the pod's life is tied
    to the scenario's rather than to whichever container runs longest. Filtering by name
    would therefore exclude nothing -- and would exclude the wrong thing if it tried, since
    ``plan_containers`` maps the ``simulation`` role onto ``sut`` whenever nothing declares
    a separate simulator, so "the simulator" is not a fixed container name.

    What does separate the cases is *how* the container left. A sidecar that finished its
    work exits 0 and the kubelet restarts it because that is what ``Always`` means; the
    trial is untouched and the run's own verdict stands. A sidecar that CRASHED -- non-zero,
    OOM-killed, or dead on a signal -- takes its state with it, and the scenario carries on
    against a simulator that no longer remembers the trial. That result is worthless
    whether it says failed or, worse, passed.

    Unknown counts as invalidating: a missing ``last_state`` means the kubelet has not said
    what the previous instance died of, and treating silence as a clean exit is how a
    broken trial gets believed.
    """
    return _format_restarts([r for r in pod_container_failures(pod) if r["invalidating"]])


def _after_last(lines, needle):
    """Lines after the last occurrence of *needle* in *lines*, or ``None`` if absent."""
    for i in range(len(lines) - 1, -1, -1):
        if lines[i] == needle:
            return lines[i + 1:]
    return None


#: Lines of the dead container's own output kept as evidence. The reason a container
#: crashed is at the END of its log, and 400 lines is a few tens of kB -- nothing beside a
#: rosbag, and bounded so a chatty simulator cannot make the invalidation path slow.
PREVIOUS_LOG_TAIL_LINES = 400


def previous_container_log(core, namespace: str, pod_name: str, container: str,
                           tail_lines: int = PREVIOUS_LOG_TAIL_LINES) -> "tuple[str, str]":
    """``(text, status)`` -- the output of the container instance that DIED, or why there
    is none.

    The one artifact that answers "what happened", and the only one with a deadline: the
    kubelet keeps a restarted container's previous log for as long as it keeps the pod, and
    a campaign that deletes the job has minutes. Nothing in robovast has ever read it, so
    every restart so far has been diagnosed from a single formatted sentence.

    **Never raises.** It runs on the path that handles a failure, and a diagnostic that can
    fail the thing it is diagnosing is worse than no diagnostic. Every outcome is instead a
    *status*: ``captured``, ``gone`` (the kubelet no longer has it), ``empty`` (it had one
    and it was blank), or ``unavailable[: <code>]``. That distinction is the point -- "we
    looked and there was nothing" and "we never looked" are different facts, and collapsing
    them is how the previous campaign lost its evidence.
    """
    try:
        text = core.read_namespaced_pod_log(
            name=pod_name, namespace=namespace, container=container,
            previous=True, tail_lines=tail_lines, timestamps=True)
    except client.ApiException as exc:
        # 400 is what the API answers when there is no previous instance retained, 404
        # when the pod itself has gone. Both mean the same thing to a reader.
        status = "gone" if exc.status in (400, 404) else f"unavailable: {exc.status}"
        logger.debug("No previous log for %s/%s: %s", pod_name, container, exc)
        return "", status
    except Exception as exc:  # noqa: BLE001 - a diagnostic must not raise
        logger.debug("Could not read previous log for %s/%s: %s", pod_name, container, exc)
        return "", "unavailable"
    text = text or ""
    return text, ("captured" if text.strip() else "empty")


class PodLogTail:
    """Incremental, cache-backed reader for a running pod's merged container logs.

    Backs the byte-offset streaming protocol (:meth:`ClusterService.get_job_log`)
    *without* re-reading the whole pod log on every poll — the pathology that made a
    long-running job's log panel pull megabytes from the kube API every 1.5s. The
    full assembled text is kept in :attr:`buf` so a client's byte offset still maps
    straight onto it, but each :meth:`read` only pulls a small trailing window from
    the API (``since_seconds``) and appends the lines it has not seen yet.

    Append-only by construction, which is what the byte-offset protocol rests on: a pod's
    containers share the node clock and logs only grow forward, so lines fetched in a later
    poll always sort after everything already buffered. Dedup across the overlapping
    ``since_seconds`` windows is by exact last-consumed line per container, which is
    unique because kubelet stamps every line with a nanosecond timestamp.

    The tagging and appending live in :class:`robovast.common.log_tail.MergedLogBuffer`,
    shared with the local lane so the same campaign reads the same either way. What is
    kube-specific — the ``since_seconds`` window, the anchor dedup, the re-anchor — is here.
    """

    #: Slack (seconds) added to the since-window so a poll never misses lines written
    #: in the same second as the previous read — ``since_seconds`` is second-granular.
    _SINCE_SLACK = 2

    def __init__(self):
        self.merged = MergedLogBuffer()  # the stream so far; a client's offset indexes it
        self.terminal = False
        self._last_line = {}            # container -> last raw "<ts> msg" line consumed
        self._last_ts = {}              # container -> last seen ts (for continuation lines)
        self._last_wall = None          # time.time() of last successful fetch
        self.lock = threading.Lock()    # serialize concurrent reads of the same job

    def read(self, core, pod, namespace, now) -> bool:
        """Fetch the delta since the last read, append it, and return ``terminal``."""
        names = [c.name for c in pod_workload_containers(pod) if getattr(c, "name", None)]
        names = names or ["robovast"]
        multi = len(names) > 1
        width = tag_width(names) if multi else 0
        # First read pulls the whole log; later reads only the elapsed window + slack.
        since = None
        if self._last_wall is not None:
            since = int(now - self._last_wall) + self._SINCE_SLACK

        def _fetch(container, since_seconds):
            try:
                return core.read_namespaced_pod_log(
                    name=pod.metadata.name, namespace=namespace, container=container,
                    timestamps=True, since_seconds=since_seconds)
            except client.exceptions.ApiException as e:
                # 404: pod/container gone; 400: container waiting / no log yet.
                if e.status in (400, 404):
                    return None
                raise

        def _lines(raw):
            out = (raw or "").split("\n")
            if out and out[-1] == "":
                out.pop()  # trailing newline from the API is not a real line
            return out

        # Concurrently, because this runs every 0.5s per open panel and a three-container
        # job would otherwise cost three serial round-trips -- over a `kubectl
        # port-forward` (a service driving the lane from off-cluster) that is enough to
        # make the panel visibly trail the run. The merge below sorts by timestamp, so
        # completion order does not affect the output.
        if len(names) > 1:
            with ThreadPoolExecutor(max_workers=len(names)) as pool:
                fetched = list(pool.map(lambda n: _fetch(n, since), names))
        else:
            fetched = [_fetch(names[0], since)]

        new = []  # ((ts, container_order, line_order), container, message)
        for order, (name, raw) in enumerate(zip(names, fetched)):
            lines = _lines(raw)
            last = self._last_line.get(name)
            if last is not None:
                fresh = _after_last(lines, last)
                if fresh is None and since is not None:
                    # The window slid past the boundary line (a long gap between
                    # polls). Re-anchor with a full read so no lines are dropped.
                    lines = _lines(_fetch(name, None))
                    fresh = _after_last(lines, last)
                if fresh is None:
                    # Anchor gone even from the full log: kubelet rotated it out of
                    # the retained window (a very chatty job). We can't tell which
                    # fetched lines are already buffered, so skip forward to the
                    # newest line rather than risk duplicating the whole buffer — a
                    # few lines may be missed, but the stream stays consistent.
                    if lines:
                        newest = lines[-1].partition(" ")[0]
                        if "T" in newest and newest[:1].isdigit():
                            self._last_ts[name] = newest
                        self._last_line[name] = lines[-1]
                    continue
                lines = fresh
            if not lines:
                continue
            self._last_line[name] = lines[-1]
            for line_order, line in enumerate(lines):
                ts, _, message = line.partition(" ")
                if "T" in ts and ts[:1].isdigit():
                    self._last_ts[name] = ts
                else:
                    message = line  # continuation line: keep it whole, inherit last ts
                cur_ts = self._last_ts.get(name, "")
                new.append(((cur_ts, order, line_order), name, message))

        self.merged.append(new, multi=multi, width=width)
        self._last_wall = now
        self.terminal = bool(pod.status and pod.status.phase in ("Succeeded", "Failed"))
        return self.terminal


#: Pod phases ranked by how far the pod got, so a Job that somehow owns more than one
#: pod is classified deterministically rather than by list order. A live pod outranks a
#: finished one (that is the state a reader is asking about), and both outrank a pod
#: that has not started.
_POD_PHASE_RANK = {"Pending": 0, "Failed": 1, "Succeeded": 2, "Running": 3}


#: A scheduler "N/M nodes are available" clause, e.g. ``1 Insufficient cpu`` or
#: ``2 node(s) had untolerated taint``. The leading number is how many nodes that clause
#: applies to; what matters here is the text after it.
_UNAVAILABLE_CLAUSE = re.compile(r"^\s*\d+\s+(?P<cause>.+?)\s*$")


def unschedulable_is_contention(message: str) -> bool:
    """Is *message* an Unschedulable reason that could clear with no intervention?

    True only when **every** cause the scheduler lists is ``Insufficient <resource>`` —
    the node has the resource and something else is holding it. Anything else (an
    untolerated taint, an unmatched node selector, a missing volume) describes a cluster
    that will look identical in an hour, and gets no extra patience.

    Kubernetes does not distinguish "busy" from "too big to ever fit": both read
    ``Insufficient cpu``. So this is only half the test — the caller must also check the
    pod against what a node actually advertises (:func:`pod_fits_any_node`).

    The message shape is ``0/1 nodes are available: 1 Insufficient cpu. no new claims to
    deallocate, preemption: 0/1 nodes are available: ...``, so the preemption report and
    anything past the first sentence are dropped before the causes are read.
    """
    head = message.split("preemption:")[0]
    _, sep, detail = head.partition(":")
    if not sep:
        return False
    detail = detail.split(". ")[0].strip().rstrip(".")
    causes = [c.strip() for c in detail.split(",") if c.strip()]
    if not causes:
        return False
    for cause in causes:
        match = _UNAVAILABLE_CLAUSE.match(cause)
        text = match.group("cause") if match else cause
        if not text.startswith("Insufficient "):
            return False
    return True


#: Kubelet / registry phrasings for "this pull is rate-limited", lowercased. Deliberately
#: an allowlist of the throttling *vocabulary* rather than a list of the errors to fail
#: on: an unrecognised message keeps the strict answer, so a new registry error nobody
#: anticipated fails fast instead of sitting for the long grace on a guess.
#:
#: ``pull qps exceeded`` is the kubelet's own limiter (``registryPullQPS``/``registryBurst``,
#: 5/10 by default) deferring a pull; ``toomanyrequests`` / ``rate limit`` are what a
#: registry says when the node asks for images faster than the account may pull them.
_PULL_THROTTLE_PHRASES = ("pull qps exceeded", "toomanyrequests", "rate limit")


def image_pull_is_throttled(message: str) -> bool:
    """Is *message* a pull the kubelet or the registry is merely rate-limiting?

    True for a queue, false for a verdict. A throttled pull has an image that exists and
    a credential that works — it is waiting its turn, and it takes its turn without
    anyone doing anything. A batch of jobs created in one instant asks one kubelet for
    every image at once, so this is the image-side twin of
    :func:`unschedulable_is_contention`: invisible on a quiet cluster, routine when two
    campaigns start a batch together, and self-clearing in both cases.

    It matters because the two live on different clocks. Failing a throttled pull on the
    sixty-second blip timer ends the campaign for the one image condition that fixes
    itself — which is what happened to a 50-batch search on its 34th batch, with two
    jobs of thirty-five affected and eight hours of finished work behind it.

    Everything else — a manifest that does not exist, a registry that refuses the
    credential, a host that does not resolve — reads identically in fifteen minutes and
    keeps the short grace.
    """
    return any(phrase in message.lower() for phrase in _PULL_THROTTLE_PHRASES)


def pod_fits_any_node(pod, nodes) -> bool:
    """Could *pod* be placed on some node in *nodes* if that node were empty?

    Compares the pod's effective requests (native sidecars included) against each node's
    ``allocatable``. Capacity a node never advertises — a GPU whose device plugin is down,
    a reservation larger than the biggest machine — is not contention and no amount of
    waiting produces it, so the caller must fail such a job rather than sit on it.
    """
    from .kubernetes_kueue import _parse_resource  # noqa: PLC0415 - avoids a cycle
    from .kube_client import pod_workload_containers  # noqa: PLC0415

    required = {}
    for container in pod_workload_containers(pod):
        requests = (container.resources.requests
                    if container.resources else None) or {}
        for name, value in requests.items():
            required[name] = required.get(name, 0.0) + _parse_resource(value)
    for node in nodes:
        allocatable = (node.status.allocatable or {}) if node.status else {}
        if all(amount <= _parse_resource(allocatable.get(name))
               for name, amount in required.items()):
            return True
    return False


def _pod_signals(k8s_core, namespace,
                 label_selector) -> "tuple[dict, dict, dict, dict, dict]":
    """One pod list → ``(pod_phases, blocked, terminated, restarted, contended)``.

    ``pod_phases``: Job name → its pod's phase — the truth a Job's ``status`` can't
    give, in both directions. ``status.active`` counts Pending pods as active, and it
    keeps counting a pod that has already terminated until the job controller catches
    up (see :func:`job_phase`). ``blocked_job_reasons``: Job name →
    ``"<reason>: <message>"`` for pods that cannot start (image pull / container-config
    errors). ``terminated_reasons``: Job name → reason string for a pod that ended
    abnormally (OOMKilled / evicted / deadline — see :func:`pod_termination_reason`), so
    a *failed* job can explain itself. ``restarted``: Job name → ``{"detail", "containers"}`` for a
    pod whose container the kubelet restarted after a CRASH (see
    :func:`pod_invalidating_restart`) -- the one signal here that condemns a job which
    still looks healthy. The records travel with the reason because the pod they came from
    is about to be deleted, and nothing else can answer what the container died of
    afterwards. ``contended``: the
    subset of ``blocked`` that is only waiting its turn and would start on its own --
    for the node it needs (:func:`unschedulable_is_contention` plus
    :func:`pod_fits_any_node`) or for the pull it asked for
    (:func:`image_pull_is_throttled`). The caller needs the distinction because it is the
    difference between a campaign that is slow and one that is broken.

    A node list that cannot be read leaves the *scheduling* half of ``contended`` empty,
    so an unreadable cluster yields the stricter answer there: a pod refused for capacity
    is treated as one that will not recover. A throttled pull needs no node list -- there
    is no node fact that could make it permanent -- so it is unaffected.

    Raises on a pod-list error rather than returning empties: a silent empty result
    is indistinguishable from "nothing is blocked", which let the run loop's grace
    timer reset and a genuinely blocked batch hang until the deadline hard-kill.
    Callers that can tolerate losing the refinement (the advisory job *listing*)
    catch this explicitly; the escalation loop treats a failed probe as "unknown",
    never as "unblocked".
    """
    pods = k8s_core.list_namespaced_pod(namespace, label_selector=label_selector).items
    phases, blocked, terminated, restarted, contended = {}, {}, {}, {}, {}
    nodes = None  # listed lazily: only an unschedulable pod needs to know node sizes
    for pod in pods:
        name = _pod_job_name(pod)
        if not name:
            continue
        phase = pod.status.phase if pod.status else None
        if phase is not None:
            known = phases.get(name)
            if known is None or _POD_PHASE_RANK.get(phase, -1) > _POD_PHASE_RANK.get(
                    known, -1):
                phases[name] = phase
        reason = pod_block_reason(pod)
        if reason:
            r, msg = reason
            blocked[name] = f"{r}: {msg}" if msg else r
            if r in POD_PULL_REASONS and msg and image_pull_is_throttled(msg):
                # No node list needed: unlike a reservation that may be larger than any
                # machine, a throttled pull has nothing that could make it permanent.
                contended[name] = blocked[name]
            if r == "Unschedulable" and msg and unschedulable_is_contention(msg):
                if nodes is None:
                    try:
                        nodes = k8s_core.list_node().items
                    except Exception as exc:  # noqa: BLE001 - stay on the strict side
                        logger.warning("Could not list nodes to tell a busy cluster from "
                                       "an impossible request: %s", exc)
                        nodes = []
                if nodes and pod_fits_any_node(pod, nodes):
                    contended[name] = blocked[name]
        term = pod_termination_reason(pod)
        if term:
            r, msg = term
            terminated[name] = f"{r}: {msg}" if msg else r
        invalidating = [r for r in pod_container_failures(pod) if r["invalidating"]]
        formatted = _format_restarts(invalidating)
        if formatted:
            r, msg = formatted
            restarted[name] = {"detail": f"{r}: {msg}" if msg else r,
                               "containers": invalidating}
    return phases, blocked, terminated, restarted, contended


def running_scenario_job_names(k8s_core, namespace, label_selector) -> set:
    """Set of Job names that currently own a pod in phase ``Running`` (see
    :func:`_pod_signals`)."""
    phases = _pod_signals(k8s_core, namespace, label_selector)[0]
    return {name for name, phase in phases.items() if phase == "Running"}


def blocked_job_reasons(k8s_core, namespace, label_selector) -> dict:
    """Job name → ``"<reason>: <message>"`` for Jobs whose pod cannot start **right
    now** -- an image pull / container-config error, or a pod the scheduler cannot
    place; see :func:`_pod_signals`.

    A truthy result means "these jobs are not starting", NOT "these jobs will never
    start": the mapping includes the ones merely waiting their turn for a busy node or
    a throttled pull, which start by themselves. A caller that must tell those apart --
    to choose a grace window, or to decide what to show a reader -- wants
    :func:`blocked_and_contended_reasons` and its second element."""
    return _pod_signals(k8s_core, namespace, label_selector)[1]


def blocked_and_contended_reasons(k8s_core, namespace,
                                  label_selector) -> "tuple[dict, dict]":
    """``(blocked, contended)`` from a single pod list — :func:`blocked_job_reasons` plus
    the subset of it that is merely waiting its turn, for a node or for a pull.

    ``contended`` is always a subset of ``blocked``; ``blocked - contended`` is what will
    not recover on its own. One call because the escalation loop needs both every couple
    of seconds and two calls would double the pod listing.
    """
    _, blocked, _, _, contended = _pod_signals(k8s_core, namespace, label_selector)
    return blocked, contended


def restarted_job_forensics(k8s_core, namespace, label_selector,
                            job_names=None) -> dict:
    """Job name → ``{"detail", "containers"}`` for Jobs whose pod had a container CRASH and
    be restarted (see :func:`pod_invalidating_restart`). Empty when nothing did.

    Separate from :func:`blocked_job_reasons` because it needs the opposite response.
    Blocked means "cannot start yet", so it is given a grace period. A restart has
    *already happened* and cannot be undone: the trial ran on against a simulator that had
    lost its state, so no amount of waiting makes its result mean anything. What the caller
    does about it is invalidate that ONE trial -- not the batch, and not the campaign.

    *job_names* scopes the answer, and a batch caller must pass it. The label selector
    available at the call site is ``jobgroup=scenario-runs,campaign-id=<campaign>``, which
    is campaign-wide, and Jobs linger for ``ttlSecondsAfterFinished`` after they finish --
    so without this an already-recorded restart from an earlier batch is re-reported to
    every batch that follows, and acted on again.
    """
    restarted = _pod_signals(k8s_core, namespace, label_selector)[3]
    if job_names is None:
        return restarted
    wanted = set(job_names)
    return {name: entry for name, entry in restarted.items() if name in wanted}


def restarted_job_reasons(k8s_core, namespace, label_selector, job_names=None) -> dict:
    """Job name → ``"<reason>: <message>"``; :func:`restarted_job_forensics` without the
    evidence, for the display paths that only want the sentence."""
    return {name: entry["detail"] for name, entry
            in restarted_job_forensics(k8s_core, namespace, label_selector,
                                       job_names).items()}


def _suspended_job_reasons(job_list, namespace) -> dict:
    """Job name → Kueue's wait message, for each Job still ``spec.suspend`` true.

    Kept advisory (an unreadable Workload just yields a generic message) because this
    only enriches a listing; the campaign-level fail decision belongs to the run loop's
    admission re-check, not to a display path.
    """
    suspended = [job.metadata.name for job in job_list.items
                 if getattr(getattr(job, "spec", None), "suspend", False)]
    if not suspended:
        return {}
    from .kubernetes_kueue import workload_wait_reasons
    reasons = workload_wait_reasons(namespace, job_names=suspended)
    return {name: reasons.get(name, "waiting for Kueue admission")
            for name in suspended}


def list_jobs_with_phase(k8s_batch, k8s_core, namespace, label_selector):
    """List scenario-run Jobs matching *label_selector*, each with its phase + detail.

    The one place that turns "Jobs + pods" into an accurate phase, so every consumer
    (service :meth:`ClusterService.list_jobs`, the CLI monitor, MCP — all of which go
    through one of those) classifies identically and can never drift. The pod list is
    fetched once (:func:`_pod_signals`) so an active-but-Pending pod is reported
    ``pending`` rather than ``running`` — and an active Job whose pod has already
    finished is reported ``completed``/``failed`` rather than falling back to
    ``pending`` while the job controller catches up (see :func:`job_phase`). A pod that
    cannot start is reported with a human ``detail`` (Kubernetes' own message) instead
    of sitting silently ``pending`` forever.

    ``blocked`` means what :data:`POD_BLOCKED_REASONS` and
    :func:`unschedulable_is_contention` together decide it means: an impediment that
    will *not* clear on its own. A pod merely waiting its turn -- for a node another
    campaign is holding, or for a pull the kubelet is rate-limiting -- is reported
    ``pending``, which is the literal truth about it (the pod exists and has not
    started), and keeps the scheduler's or kubelet's message as its ``detail``. It was
    reported ``blocked`` once, and a healthy campaign on a busy cluster therefore
    showed a red row and a ``Blocked:`` count that asked a reader to intervene in
    something that fixes itself in seconds -- the same mistake, and the same fix, as
    the ``waiting`` phase below. How long the run loop tolerates either lives there,
    not here (:data:`CONTENDED_GRACE_SECONDS` vs :data:`BLOCKED_GRACE_SECONDS`).

    A Kueue-**suspended** Job is its own phase, ``waiting``, carrying Kueue's wait
    message as ``detail``. It has no pod at all, so the pod-level probe cannot see it
    and it would otherwise report ``pending`` — indistinguishable from a job about to
    start, which is how a batch that could never be admitted looked like a slow one.
    It is deliberately *not* ``blocked``: waiting for quota is Kueue's normal operating
    state (every cluster batch starts there), so calling it blocked made the healthy
    case indistinguishable from the broken one and trained readers to ignore both.

    ``blocked`` is its own status, distinct from ``failed``: Kubernetes still counts
    the Job active (it keeps retrying the pull), so it has neither completed nor been
    marked failed — it simply cannot make progress. The campaign-level escalation
    (fail the batch after a grace window) lives in the run loop, not here.

    Returns a list of ``(job, phase, detail)`` tuples in the order the API returned
    the Jobs; ``detail`` is ``None`` unless the Job is blocked (image pull / config
    error), pending for a stated reason (contended: unschedulable or a throttled pull),
    waiting for Kueue admission, or failed for an infrastructure reason (OOMKilled /
    evicted / deadline), in which case it carries Kubernetes' or Kueue's own
    explanation.
    """
    job_list = k8s_batch.list_namespaced_job(namespace, label_selector=label_selector)
    try:
        phases, blocked, terminated, restarted, contended = _pod_signals(
            k8s_core, namespace, label_selector)
    except Exception as exc:  # noqa: BLE001 - advisory listing degrades explicitly
        # A transient pod-list hiccup: report Job-level phases for this listing only
        # (it self-corrects on the next poll). The safety-critical blocked-job
        # escalation does NOT come through here — it calls blocked_job_reasons and
        # handles a failed probe itself, so it is not weakened by this fallback.
        #
        # `None`, not an empty mapping: to job_phase an empty mapping is pod truth
        # saying "no job has a pod", which reports every active job pending — so one
        # failed pod list painted the whole batch as not-yet-started. `None` is the
        # documented "no pod truth" signal that actually falls back to Job level.
        logger.warning("Pod-level refinement unavailable (%s); reporting Job-level "
                       "phases for this listing.", exc)
        phases, blocked, terminated, restarted, contended = None, {}, {}, {}, {}
    suspended = _suspended_job_reasons(job_list, namespace)
    out = []
    for job in job_list.items:
        name = job.metadata.name
        detail = blocked.get(name)
        if detail and name not in contended:
            phase = "blocked"
        # The `not detail` guard matters only because the branch above no longer
        # swallows every job that has one. This branch assigns `detail`
        # unconditionally, so without it a contended job that also appeared in
        # `suspended` would have the scheduler's message replaced by Kueue's. A
        # suspended Job has no pod and so cannot be contended, which makes that
        # unreachable rather than a live bug -- the guard is what keeps the branch
        # from silently starting to lie if that ever stops being true.
        elif not detail and name in suspended:
            phase, detail = "waiting", suspended[name]
        else:
            # No impediment, or one that clears by itself: the pod's own phase is the
            # truth, and a contended job keeps the scheduler's message as its reason.
            phase = job_phase(job, phases)
        # A failed job whose pod was OOM-killed / evicted / deadline-exceeded would
        # otherwise show no cause (its scenario log is truncated) — surface it.
        if phase == "failed" and not detail:
            detail = terminated.get(name)
        # A restart is reported whatever the phase: the pod may still be Running, and
        # that is exactly the case worth surfacing -- a job on its way to a plausible
        # result its simulator can no longer justify.
        if not detail:
            detail = (restarted.get(name) or {}).get("detail")
        out.append((job, phase, detail))
    return out


def cleanup_cluster_campaign(namespace="default", campaign=None, context=None):
    """Clean up scenario run jobs, pods, and Kueue workloads from the cluster.

    Holds the ClusterQueue for the duration **only when cleaning the whole cluster**
    (*campaign* is ``None``), where pausing all admissions is the intent.

    A single campaign's cleanup deliberately does not touch it. ``stopPolicy`` lives on
    one cluster-scoped ClusterQueue shared by every campaign, so holding it to delete
    one campaign's jobs stops *every* other campaign's jobs being admitted for the
    length of the cleanup — and left behind by a failed cleanup, forever. Per-campaign
    quota safety does not need it: the deletions are label-scoped and already ordered
    Workloads-before-Jobs, which is what lets Kueue release that campaign's quota
    cleanly. See :func:`~...kubernetes_kueue.cluster_queue_held`.

    Args:
        namespace: Kubernetes namespace.
        campaign: If given, clean only this run's jobs/pods/workloads.
        context: Kubernetes context name to use. ``None`` uses the active context.
    """
    with contextlib.ExitStack() as stack:
        if campaign is None:
            stack.enter_context(cluster_queue_held(kube_context=context))
        _cleanup_cluster_campaign_resources(namespace=namespace, campaign=campaign,
                                            context=context)


def _cleanup_cluster_campaign_resources(namespace="default", campaign=None, context=None):
    """Delete scenario run jobs, pods, and Kueue workloads (steps 2-7 and 9).

    Called by :func:`cleanup_cluster_campaign`, which owns holding and resuming the
    ClusterQueue around it.

    Cleanup order is designed to avoid confusing Kueue's quota tracking:
    2. Delete Workloads first so Kueue releases quota before Jobs disappear.
    3. Force-clear finalizers on stuck Workloads.
    4. Delete Jobs (Foreground propagation so pods are reaped by the Job controller).
    5. Force-clear finalizers on stuck Jobs.
    6. Delete Pods.
    7. Force-clear finalizers on stuck Pods.

    If campaign is given, removes only resources for that run (label
    ``jobgroup=scenario-runs,campaign-id=<campaign>``) plus that campaign's
    controller pod. Otherwise removes all resources with label
    ``jobgroup=scenario-runs`` and every controller pod.

    Args:
        namespace: Kubernetes namespace.
        campaign: If given, clean only this run's jobs/pods/workloads.
        context: Kubernetes context name to use. ``None`` uses the active context.
    """
    # In-cluster first (the service drives campaigns in-pod), else the host context.
    from .kube_client import load_kube_config
    load_kube_config(context=context)
    k8s_client = client.CoreV1Api()
    k8s_batch_client = client.BatchV1Api()

    label_selector = "jobgroup=scenario-runs"
    if campaign is not None:
        label_safe = _label_safe_campaign(campaign)
        label_selector = f"jobgroup=scenario-runs,campaign-id={label_safe}"

    # Step 2+3: Delete Workloads FIRST so Kueue can release quota cleanly
    # before the underlying Jobs disappear. Hard finalizer cleanup is handled
    # inside cleanup_kueue_workloads.
    logger.info("Deleting Kueue workloads before jobs (quota-safe order)")
    cleanup_kueue_workloads(
        namespace=namespace,
        label_selector=label_selector,
        campaign_id=campaign,
        k8s_batch_client=k8s_batch_client,
    )

    # Step 4: Delete Jobs with Foreground propagation so the Job controller
    # reaps pods before the Job object itself is removed.
    try:
        logger.info("Deleting jobs with label selector '%s'", label_selector)
        k8s_batch_client.delete_collection_namespaced_job(
            namespace=namespace,
            label_selector=label_selector,
            body=client.V1DeleteOptions(
                grace_period_seconds=0, propagation_policy="Foreground"
            ),
        )
        logger.info("Successfully deleted scenario-runs jobs")
    except client.rest.ApiException as e:
        logger.error("Error deleting jobs: %s", e)
        raise

    # Step 5: Force-clear finalizers on any Jobs still stuck in Terminating.
    # Retry in a loop: after patching finalizers, Kubernetes may surface more
    # stuck jobs that were waiting behind the ones just cleared.
    for _attempt in range(30):
        try:
            remaining_jobs = k8s_batch_client.list_namespaced_job(
                namespace=namespace,
                label_selector=label_selector,
            )
        except client.rest.ApiException as e:
            logger.warning("Error listing remaining jobs: %s", e)
            break
        stuck_jobs = [
            job for job in remaining_jobs.items
            if job.metadata.deletion_timestamp is not None or job.metadata.finalizers
        ]
        if not stuck_jobs:
            break

        def _clear_job_finalizers(job):
            try:
                k8s_batch_client.patch_namespaced_job(
                    name=job.metadata.name,
                    namespace=namespace,
                    body={"metadata": {"finalizers": None}},
                )
                logger.info("Cleared finalizers on job '%s'", job.metadata.name)
            except client.rest.ApiException as e:
                if e.status == 404:
                    logger.debug("Job '%s' already gone (404), skipping", job.metadata.name)
                else:
                    logger.warning("Error clearing finalizers from job '%s': %s", job.metadata.name, e)

        logger.warning(
            "%d job(s) stuck (Terminating or has finalizers); clearing finalizers in batch",
            len(stuck_jobs),
        )
        with ThreadPoolExecutor(max_workers=min(len(stuck_jobs), 16)) as pool:
            list(pool.map(_clear_job_finalizers, stuck_jobs))
        time.sleep(1)

    # Step 6: Delete Pods.
    try:
        logger.info("Deleting pods with label selector '%s'", label_selector)
        k8s_client.delete_collection_namespaced_pod(
            namespace=namespace,
            label_selector=label_selector,
            body=client.V1DeleteOptions(
                grace_period_seconds=0, propagation_policy="Background"
            ),
        )
        logger.info("Successfully deleted scenario-runs pods")
    except client.rest.ApiException as e:
        logger.error("Error deleting pods: %s", e)
        raise

    # Step 7: Force-clear finalizers on any Pods still stuck in Terminating.
    for _attempt in range(30):
        try:
            remaining_pods = k8s_client.list_namespaced_pod(
                namespace=namespace,
                label_selector=label_selector,
            )
        except client.rest.ApiException as e:
            logger.warning("Error listing remaining pods: %s", e)
            break
        stuck_pods = [
            pod for pod in remaining_pods.items
            if pod.metadata.deletion_timestamp is not None or pod.metadata.finalizers
        ]
        if not stuck_pods:
            break

        def _clear_pod_finalizers(pod):
            try:
                k8s_client.patch_namespaced_pod(
                    name=pod.metadata.name,
                    namespace=namespace,
                    body={"metadata": {"finalizers": None}},
                )
                logger.info("Cleared finalizers on pod '%s'", pod.metadata.name)
            except client.rest.ApiException as e:
                if e.status == 404:
                    logger.debug("Pod '%s' already gone (404), skipping", pod.metadata.name)
                else:
                    logger.warning("Error clearing finalizers from pod '%s': %s", pod.metadata.name, e)

        logger.warning(
            "%d pod(s) stuck (Terminating or has finalizers); clearing finalizers in batch",
            len(stuck_pods),
        )
        with ThreadPoolExecutor(max_workers=min(len(stuck_pods), 16)) as pool:
            list(pool.map(_clear_pod_finalizers, stuck_pods))
        time.sleep(1)

    # Step 9: Also reap any auxiliary-container pod(s). There are no controller
    # pods any more (the service drives campaigns in-process), but a campaign whose
    # variations need a helper image has an aux pod. On a full cleanup (campaign is
    # None) reap every aux pod; for a single campaign reap only its pod (label
    # ``campaign-id=<campaign>``) so concurrent campaigns are left untouched.
    try:
        from .container_runner import cleanup_aux_pods  # pylint: disable=import-outside-toplevel
        cleanup_aux_pods(namespace=namespace, kube_context=context, campaign=campaign)
    except Exception as exc:  # pragma: no cover - best-effort
        logger.warning("Failed to clean up aux pods: %s", exc)

    # Step 10: The campaign's Kueue priority class, scoped by the same
    # jobgroup/campaign-id labels as everything above. LAST, after the Workloads and Jobs
    # are gone: deleting it cannot disturb work Kueue has already queued (the resolved
    # value is copied onto each Workload at creation), but a Job created against a
    # missing class is rejected, so removing it any earlier could break a campaign that
    # is still submitting.
    cleanup_campaign_priority_classes(campaign=campaign, kube_context=context)


#: One counter per phase :func:`list_jobs_with_phase` can report.
#:
#: Spelled out because the loop below used to do ``per_run[campaign][phase] += 1`` against
#: a dict seeded with four names -- which quietly made the classifier's vocabulary this
#: function's schema. Both phases added there since (``blocked``, then ``waiting``) were
#: therefore a ``KeyError`` here, and the monitor's caller catches everything and prints
#: "(unreachable)", so a healthy cluster reported as an unreachable one.
JOB_PHASE_COUNTERS = ("completed", "failed", "running", "pending", "blocked", "waiting")


def get_cluster_job_counts_per_campaign(namespace="default", context=None):
    """Get status counts per campaign for scenario run jobs.

    Returns a dict mapping campaign (or "<legacy>" for jobs without campaign-id label) to
    a counts dict with one key per :data:`JOB_PHASE_COUNTERS`, plus ``total_job_num``.

    ``blocked`` and ``waiting`` are reported separately rather than folded into
    ``pending``: they are the two ways a job can sit unstarted for a reason of its own --
    an image it cannot pull, or quota it has not been granted -- and a consumer that adds
    them to ``pending`` loses exactly the distinction worth having. A consumer asking "is
    this batch still going?" must count them as unfinished, though; see the monitor.

    Args:
        namespace: Kubernetes namespace.
        context: Kubernetes context name to use. ``None`` uses the active context.
    """
    from .kube_client import load_kube_config
    load_kube_config(context=context)
    try:
        # Phase reflects true pod state (an active-but-Pending pod counts as pending).
        jobs = list_jobs_with_phase(
            client.BatchV1Api(), client.CoreV1Api(), namespace, "jobgroup=scenario-runs")
    except client.rest.ApiException as e:
        logger.error(f"Error listing jobs with label selector: {e}")
        raise

    per_run = {}

    # ``detail`` (the third element) is per-job prose -- why a job is blocked, what Kueue
    # is waiting for -- which a per-campaign count has nowhere to put. Unpacking two from
    # a three-tuple is what raised ValueError on every call.
    for job, phase, _detail in jobs:
        campaign = "<legacy>"
        if job.metadata.labels and "campaign-id" in job.metadata.labels:
            campaign = job.metadata.labels["campaign-id"]

        if campaign not in per_run:
            per_run[campaign] = dict.fromkeys(JOB_PHASE_COUNTERS, 0)
            per_run[campaign]["total_job_num"] = None

        # Read total-job-num annotation from the first job that has it
        if per_run[campaign]["total_job_num"] is None and job.metadata.annotations:
            raw = job.metadata.annotations.get("total-job-num")
            if raw is not None:
                try:
                    per_run[campaign]["total_job_num"] = int(raw)
                except (ValueError, TypeError):
                    pass

        if phase in JOB_PHASE_COUNTERS:
            per_run[campaign][phase] += 1
        else:
            # A phase the classifier grew and this function has not been taught. Counted
            # as unfinished, which is the direction that cannot become a premature "all
            # jobs finished", and said out loud so the next one does not go unnoticed.
            logger.warning("unknown scenario-job phase %r counted as pending", phase)
            per_run[campaign]["pending"] += 1

    return per_run
