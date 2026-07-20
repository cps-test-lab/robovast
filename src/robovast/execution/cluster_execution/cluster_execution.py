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
import time
from concurrent.futures import ThreadPoolExecutor

from kubernetes import client
from kubernetes import config as kube_config

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


def running_scenario_job_names(k8s_core, namespace, label_selector) -> set:
    """Set of Job names that currently own a pod in phase ``Running``.

    A Kubernetes Job's ``status.active`` cannot tell Pending from Running pods, so
    the truth comes from the pods themselves. Pods carry their owning Job's name in
    the ``batch.kubernetes.io/job-name`` label (older clusters: ``job-name``); a Job
    is "really running" once at least one of its pods reaches phase ``Running``.
    Best-effort: on any pod-list error, returns the empty set so classification
    falls back to the Job-level view rather than failing the whole listing.
    """
    try:
        pods = k8s_core.list_namespaced_pod(namespace, label_selector=label_selector)
    except Exception as exc:  # noqa: BLE001 - best-effort refinement
        logger.warning("Could not list pods to refine job status: %s", exc)
        return set()
    names = set()
    for pod in pods.items:
        if not (pod.status and pod.status.phase == "Running"):
            continue
        labels = pod.metadata.labels or {}
        name = labels.get("batch.kubernetes.io/job-name") or labels.get("job-name")
        if name:
            names.add(name)
    return names


def list_jobs_with_phase(k8s_batch, k8s_core, namespace, label_selector):
    """List scenario-run Jobs matching *label_selector*, each paired with its phase.

    The one place that turns "Jobs + pods" into an accurate phase, so every consumer
    (service :meth:`ClusterService.list_jobs`, the CLI monitor, MCP — all of which go
    through one of those) classifies identically and can never drift. The pod list is
    fetched once and threaded into :func:`job_phase` so an active-but-Pending pod is
    reported ``pending`` rather than ``running``.

    Returns a list of ``(job, phase)`` tuples in the order the API returned the Jobs.
    """
    job_list = k8s_batch.list_namespaced_job(namespace, label_selector=label_selector)
    running = running_scenario_job_names(k8s_core, namespace, label_selector)
    return [(job, job_phase(job, running)) for job in job_list.items]


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
    try:
        kube_config.load_incluster_config()
    except kube_config.ConfigException:
        kube_config.load_kube_config(context=context)
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
    kube_config.load_kube_config(context=context)
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
