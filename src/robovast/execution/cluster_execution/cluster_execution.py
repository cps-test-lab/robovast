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

    # Step 9: Also reap the controller pod(s). On a full cleanup (campaign is
    # None) reap every controller pod; for a single campaign reap only its pod
    # (label ``campaign-id=<campaign>``) so concurrent runs are left untouched.
    try:
        from .controller_launcher import \
            cleanup_controller_pods  # pylint: disable=import-outside-toplevel
        cleanup_controller_pods(namespace=namespace, kube_context=context, campaign=campaign)
    except Exception as exc:  # pragma: no cover - best-effort
        logger.warning("Failed to clean up controller pods: %s", exc)


def get_cluster_job_counts_per_campaign(namespace="default", context=None):
    """Get status counts per campaign for scenario run jobs.

    Returns a dict mapping campaign (or "<legacy>" for jobs without campaign-id label)
    to counts dict with keys completed, failed, running, pending.

    Args:
        namespace: Kubernetes namespace.
        context: Kubernetes context name to use. ``None`` uses the active context.
    """
    kube_config.load_kube_config(context=context)
    k8s_batch_client = client.BatchV1Api()

    try:
        job_list = k8s_batch_client.list_namespaced_job(
            namespace=namespace,
            label_selector="jobgroup=scenario-runs",
        )
    except client.rest.ApiException as e:
        logger.error(f"Error listing jobs with label selector: {e}")
        raise

    per_run = {}

    for job in job_list.items:
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

        status = job.status
        if status is None:
            per_run[campaign]["pending"] += 1
            continue

        succeeded = status.succeeded or 0
        failed = status.failed or 0
        active = status.active or 0

        if succeeded >= 1:
            per_run[campaign]["completed"] += 1
        elif active >= 1:
            per_run[campaign]["running"] += 1
        elif failed >= 1:
            per_run[campaign]["failed"] += 1
        else:
            per_run[campaign]["pending"] += 1

    return per_run
