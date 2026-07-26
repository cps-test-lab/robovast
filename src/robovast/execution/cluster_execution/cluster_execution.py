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

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from kubernetes import client

from .kubernetes_kueue import (cleanup_kueue_workloads,
                               set_cluster_queue_stop_policy)

logger = logging.getLogger(__name__)


def _label_safe_campaign(campaign: str) -> str:
    """Convert campaign to a valid Kubernetes label value.

    Label values must be 63 chars or less, alphanumeric, hyphens, periods.
    """
    s = campaign.lower().replace("_", "-")
    return "".join(c for c in s if c.isalnum() or c in "-.")[:63]


def job_phase(job, running_job_names=None) -> str:
    """Classify a scenario-run Job into ``completed``/``running``/``failed``/``pending``.

    Shared by the aggregate counter and the per-job lister so the two never drift.

    A Job's ``status.active`` counts pods that are Pending *or* Running, so a Job
    whose pod is still unscheduled / pulling its image / freshly Kueue-admitted looks
    "active" while ``k9s`` shows the pod ``Pending``. When *running_job_names* is
    supplied (the set of Job names that own an actually-``Running`` pod — see
    :func:`running_scenario_job_names`), an active Job is only reported ``running``
    if it is in that set; otherwise it is still ``pending``.
    """
    status = job.status
    if status is None:
        return "pending"
    if (status.succeeded or 0) >= 1:
        return "completed"
    if (status.active or 0) >= 1:
        if running_job_names is not None:
            return "running" if job.metadata.name in running_job_names else "pending"
        return "running"
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


def _pod_job_name(pod) -> "str | None":
    """The owning Job's name off a pod's label (``batch.kubernetes.io/job-name``,
    older clusters: ``job-name``)."""
    labels = (pod.metadata.labels or {}) if pod.metadata else {}
    return labels.get("batch.kubernetes.io/job-name") or labels.get("job-name")


def pod_block_reason(pod) -> "tuple[str, str] | None":
    """``(reason, message)`` if a container of *pod* is stuck in an unrecoverable
    ``waiting`` state (see :data:`POD_BLOCKED_REASONS`), else ``None``.

    Checks init *and* regular containers; ``message`` is Kubernetes' own text (the
    failed image ref + registry error), possibly empty.
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


def _loggable_container_names(pod) -> "list[str]":
    """Names of *pod*'s regular containers (the main ``robovast`` plus any secondary
    sim/SUT servers), in spec order. Init containers (e.g. ``s3-init``) are startup
    noise and excluded."""
    spec = getattr(pod, "spec", None)
    containers = getattr(spec, "containers", None) or [] if spec else []
    return [c.name for c in containers if getattr(c, "name", None)]


def read_pod_logs_merged(core, pod, namespace) -> str:
    """Merge every regular container's log of *pod* into one readable stream.

    A scenario-run pod has a main ``robovast`` container plus zero or more named
    secondary containers (sim / SUT servers). This returns them as a single log:

    * one container → its plain log, unchanged (byte-identical to the old behaviour);
    * many containers → each line tagged ``[<container>] `` and all lines
      merge-sorted by the kubelet's per-line RFC3339 timestamp (fetched with
      ``timestamps=True``). Because every container shares the node clock and logs
      only ever grow forward in time, the merged text is append-only across repeated
      reads — which keeps the byte-offset streaming protocol correct.

    A container that has not started yet (``Waiting``) or whose log is otherwise
    unavailable is skipped rather than failing the whole read.
    """
    names = _loggable_container_names(pod)
    pod_name = pod.metadata.name

    def _read(container, timestamps):
        try:
            return core.read_namespaced_pod_log(
                name=pod_name, namespace=namespace, container=container,
                timestamps=timestamps)
        except client.exceptions.ApiException as e:
            # 404: pod/container gone; 400: container waiting / no log yet.
            if e.status in (400, 404):
                return None
            raise

    if len(names) <= 1:
        container = names[0] if names else "robovast"
        return _read(container, timestamps=False) or ""

    width = max(len(n) for n in names)
    entries = []  # (timestamp, container_order, line_order, rendered_line)
    for order, name in enumerate(names):
        text = _read(name, timestamps=True)
        if not text:
            continue
        prefix = f"[{name}]".ljust(width + 2)
        last_ts = ""
        for line_order, line in enumerate(text.split("\n")):
            if not line:
                continue
            ts, _, message = line.partition(" ")
            # kubelet timestamps are RFC3339 and sort lexicographically; a line
            # without one (rare continuation) inherits its container's last ts so
            # it stays next to its neighbours instead of jumping to the top.
            if "T" in ts and (ts[:1].isdigit()):
                last_ts = ts
            else:
                message = line
            entries.append((last_ts, order, line_order, f"{prefix} {message}"))
    entries.sort(key=lambda e: (e[0], e[1], e[2]))
    merged = "\n".join(e[3] for e in entries)
    return merged + "\n" if merged else ""


def _after_last(lines, needle):
    """Lines after the last occurrence of *needle* in *lines*, or ``None`` if absent."""
    for i in range(len(lines) - 1, -1, -1):
        if lines[i] == needle:
            return lines[i + 1:]
    return None


class PodLogTail:
    """Incremental, cache-backed reader for a running pod's merged container logs.

    Backs the byte-offset streaming protocol (:meth:`ClusterService.get_job_log`)
    *without* re-reading the whole pod log on every poll — the pathology that made a
    long-running job's log panel pull megabytes from the kube API every 1.5s. The
    full assembled text is kept in :attr:`buf` so a client's byte offset still maps
    straight onto it, but each :meth:`read` only pulls a small trailing window from
    the API (``since_seconds``) and appends the lines it has not seen yet.

    Same append-only assumption as :func:`read_pod_logs_merged`: a pod's containers
    share the node clock and logs only grow forward, so lines fetched in a later poll
    always sort after everything already buffered. Dedup across the overlapping
    ``since_seconds`` windows is by exact last-consumed line per container, which is
    unique because kubelet stamps every line with a nanosecond timestamp.
    """

    #: Slack (seconds) added to the since-window so a poll never misses lines written
    #: in the same second as the previous read — ``since_seconds`` is second-granular.
    _SINCE_SLACK = 2

    def __init__(self):
        self.buf = bytearray()          # full merged log so far (offset maps onto it)
        self.terminal = False
        self._last_line = {}            # container -> last raw "<ts> msg" line consumed
        self._last_ts = {}              # container -> last seen ts (for continuation lines)
        self._last_wall = None          # time.time() of last successful fetch
        self.lock = threading.Lock()    # serialize concurrent reads of the same job

    def read(self, core, pod, namespace, now) -> bool:
        """Fetch the delta since the last read, append it, and return ``terminal``."""
        names = _loggable_container_names(pod) or ["robovast"]
        multi = len(names) > 1
        width = max(len(n) for n in names) if multi else 0
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

        new = []  # (ts, container_order, line_order, rendered_line)
        for order, name in enumerate(names):
            lines = _lines(_fetch(name, since))
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
                if multi:
                    prefix = f"[{name}]".ljust(width + 2)
                    new.append((cur_ts, order, line_order, f"{prefix} {message}"))
                else:
                    new.append((cur_ts, order, line_order, message))

        new.sort(key=lambda e: (e[0], e[1], e[2]))
        text = "\n".join(e[3] for e in new)
        if text:
            if self.buf and not self.buf.endswith(b"\n"):
                self.buf += b"\n"
            self.buf += text.encode("utf-8", "replace") + b"\n"
        self._last_wall = now
        self.terminal = bool(pod.status and pod.status.phase in ("Succeeded", "Failed"))
        return self.terminal


def _pod_signals(k8s_core, namespace, label_selector) -> "tuple[set, dict, dict]":
    """One pod list → ``(running_job_names, blocked_job_reasons, terminated_reasons)``.

    ``running_job_names``: Jobs owning a pod in phase ``Running`` (the truth a Job's
    ``status.active`` can't give — it counts Pending pods too). ``blocked_job_reasons``:
    Job name → ``"<reason>: <message>"`` for pods that cannot start (image pull /
    container-config errors). ``terminated_reasons``: Job name → reason string for a
    pod that ended abnormally (OOMKilled / evicted / deadline — see
    :func:`pod_termination_reason`), so a *failed* job can explain itself.

    Raises on a pod-list error rather than returning empties: a silent empty result
    is indistinguishable from "nothing is blocked", which let the run loop's grace
    timer reset and a genuinely blocked batch hang until the deadline hard-kill.
    Callers that can tolerate losing the refinement (the advisory job *listing*)
    catch this explicitly; the escalation loop treats a failed probe as "unknown",
    never as "unblocked".
    """
    pods = k8s_core.list_namespaced_pod(namespace, label_selector=label_selector).items
    running, blocked, terminated = set(), {}, {}
    for pod in pods:
        name = _pod_job_name(pod)
        if not name:
            continue
        if pod.status and pod.status.phase == "Running":
            running.add(name)
        reason = pod_block_reason(pod)
        if reason:
            r, msg = reason
            blocked[name] = f"{r}: {msg}" if msg else r
        term = pod_termination_reason(pod)
        if term:
            r, msg = term
            terminated[name] = f"{r}: {msg}" if msg else r
    return running, blocked, terminated


def running_scenario_job_names(k8s_core, namespace, label_selector) -> set:
    """Set of Job names that currently own a pod in phase ``Running`` (see
    :func:`_pod_signals`)."""
    return _pod_signals(k8s_core, namespace, label_selector)[0]


def blocked_job_reasons(k8s_core, namespace, label_selector) -> dict:
    """Job name → ``"<reason>: <message>"`` for Jobs whose pod cannot start (image
    pull / container-config errors); see :func:`_pod_signals`. Empty when nothing is
    blocked, so a truthy result means "these jobs will never start on their own"."""
    return _pod_signals(k8s_core, namespace, label_selector)[1]


def list_jobs_with_phase(k8s_batch, k8s_core, namespace, label_selector):
    """List scenario-run Jobs matching *label_selector*, each with its phase + detail.

    The one place that turns "Jobs + pods" into an accurate phase, so every consumer
    (service :meth:`ClusterService.list_jobs`, the CLI monitor, MCP — all of which go
    through one of those) classifies identically and can never drift. The pod list is
    fetched once (:func:`_pod_signals`) so an active-but-Pending pod is reported
    ``pending`` rather than ``running``, and a pod that cannot start (image pull /
    config error) is reported ``blocked`` with a human ``detail`` (Kubernetes' own
    message) instead of sitting ``pending`` forever.

    ``blocked`` is its own status, distinct from ``failed``: Kubernetes still counts
    the Job active (it keeps retrying the pull), so it has neither completed nor been
    marked failed — it simply cannot make progress. The campaign-level escalation
    (fail the batch after a grace window) lives in the run loop, not here.

    Returns a list of ``(job, phase, detail)`` tuples in the order the API returned
    the Jobs; ``detail`` is ``None`` unless the Job is blocked (image pull / config
    error) or failed for an infrastructure reason (OOMKilled / evicted / deadline),
    in which case it carries Kubernetes' own explanation.
    """
    job_list = k8s_batch.list_namespaced_job(namespace, label_selector=label_selector)
    try:
        running, blocked, terminated = _pod_signals(k8s_core, namespace, label_selector)
    except Exception as exc:  # noqa: BLE001 - advisory listing degrades explicitly
        # A transient pod-list hiccup: report Job-level phases for this listing only
        # (it self-corrects on the next poll). The safety-critical blocked-job
        # escalation does NOT come through here — it calls blocked_job_reasons and
        # handles a failed probe itself, so it is not weakened by this fallback.
        logger.warning("Pod-level refinement unavailable (%s); reporting Job-level "
                       "phases for this listing.", exc)
        running, blocked, terminated = set(), {}, {}
    out = []
    for job in job_list.items:
        detail = blocked.get(job.metadata.name)
        phase = "blocked" if detail else job_phase(job, running)
        # A failed job whose pod was OOM-killed / evicted / deadline-exceeded would
        # otherwise show no cause (its scenario log is truncated) — surface it.
        if phase == "failed" and not detail:
            detail = terminated.get(job.metadata.name)
        out.append((job, phase, detail))
    return out


def cleanup_cluster_campaign(namespace="default", campaign=None, context=None):
    """Clean up scenario run jobs, pods, and Kueue workloads from the cluster.

    Cleanup order is designed to avoid confusing Kueue's quota tracking:
    1. Hold the ClusterQueue to prevent new admissions during cleanup (does NOT
       preempt running workloads — use Hold, not HoldAndDrain).
    2. Delete Workloads first so Kueue releases quota before Jobs disappear.
    3. Force-clear finalizers on stuck Workloads.
    4. Delete Jobs (Foreground propagation so pods are reaped by the Job controller).
    5. Force-clear finalizers on stuck Jobs.
    6. Delete Pods.
    7. Force-clear finalizers on stuck Pods.
    8. Resume the ClusterQueue (stopPolicy -> None) so new runs can be admitted.

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
    from robovast.common.kube import load_kube_config
    load_kube_config(context=context)
    k8s_client = client.CoreV1Api()
    k8s_batch_client = client.BatchV1Api()

    label_selector = "jobgroup=scenario-runs"
    if campaign is not None:
        label_safe = _label_safe_campaign(campaign)
        label_selector = f"jobgroup=scenario-runs,campaign-id={label_safe}"

    # Step 1: Pause the ClusterQueue so Kueue does not admit new jobs during
    # cleanup. Use "Hold" (not "HoldAndDrain") to avoid preempting workloads
    # that belong to other campaigns or that the user did not intend to kill.
    logger.info("Setting ClusterQueue stopPolicy to Hold before cleanup")
    set_cluster_queue_stop_policy("Hold", kube_context=context)

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

    # Step 8: Resume the ClusterQueue so future runs can be admitted.
    logger.info("Restoring ClusterQueue stopPolicy to None after cleanup")
    set_cluster_queue_stop_policy(None, kube_context=context)

    # Step 9: Also reap any auxiliary-container pod(s). There are no controller
    # pods any more (the service drives campaigns in-process), but a campaign whose
    # variations need a helper image has an aux pod. On a full cleanup (campaign is
    # None) reap every aux pod; for a single campaign reap only its pod (label
    # ``campaign-id=<campaign>``) so concurrent campaigns are left untouched.
    try:
        from .container_runner import \
            cleanup_aux_pods  # pylint: disable=import-outside-toplevel
        cleanup_aux_pods(namespace=namespace, kube_context=context, campaign=campaign)
    except Exception as exc:  # pragma: no cover - best-effort
        logger.warning("Failed to clean up aux pods: %s", exc)


def get_cluster_job_counts_per_campaign(namespace="default", context=None):
    """Get status counts per campaign for scenario run jobs.

    Returns a dict mapping campaign (or "<legacy>" for jobs without campaign-id label)
    to counts dict with keys completed, failed, running, pending.

    Args:
        namespace: Kubernetes namespace.
        context: Kubernetes context name to use. ``None`` uses the active context.
    """
    from robovast.common.kube import load_kube_config
    load_kube_config(context=context)
    try:
        # Phase reflects true pod state (an active-but-Pending pod counts as pending).
        jobs = list_jobs_with_phase(
            client.BatchV1Api(), client.CoreV1Api(), namespace, "jobgroup=scenario-runs")
    except client.rest.ApiException as e:
        logger.error(f"Error listing jobs with label selector: {e}")
        raise

    per_run = {}

    for job, phase in jobs:
        campaign = "<legacy>"
        if job.metadata.labels and "campaign-id" in job.metadata.labels:
            campaign = job.metadata.labels["campaign-id"]

        if campaign not in per_run:
            per_run[campaign] = {"completed": 0, "failed": 0, "running": 0, "pending": 0,
                                 "total_job_num": None}

        # Read total-job-num annotation from the first job that has it
        if per_run[campaign]["total_job_num"] is None and job.metadata.annotations:
            raw = job.metadata.annotations.get("total-job-num")
            if raw is not None:
                try:
                    per_run[campaign]["total_job_num"] = int(raw)
                except (ValueError, TypeError):
                    pass

        per_run[campaign][phase] += 1

    return per_run
