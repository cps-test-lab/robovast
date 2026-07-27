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

"""Kueue installation, queue setup, and workload cleanup for cluster execution."""

import contextlib
import logging
import os
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from kubernetes import client
from kubernetes.utils.quantity import parse_quantity

logger = logging.getLogger(__name__)

KUEUE_NAMESPACE = "kueue-system"
KUEUE_HELM_RELEASE = "kueue"
KUEUE_HELM_REPO = "oci://registry.k8s.io/kueue/charts/kueue"
KUEUE_HELM_VERSION = "0.16.1"
KUEUE_QUEUE_NAME = "robovast"
CLUSTER_QUEUE_NAME = "robovast-cluster-queue"

# Workload CRD for cleanup (v1beta2 used by Kueue 0.16+)
KUEUE_WORKLOAD_GROUP = "kueue.x-k8s.io"
KUEUE_WORKLOAD_VERSION = "v1beta2"
KUEUE_WORKLOAD_PLURAL = "workloads"
KUEUE_RESOURCE_FLAVOR_NAME = "default-flavor"

# Admission webhook configurations installed by the Kueue Helm chart. These are
# backed by the kueue-controller-manager pods; once those pods are gone the API
# server cannot reach the webhooks and rejects any mutation of Kueue objects
# (including finalizer removal). They must be deleted before teardown patches.
KUEUE_VALIDATING_WEBHOOK_CONFIG = "kueue-validating-webhook-configuration"
KUEUE_MUTATING_WEBHOOK_CONFIG = "kueue-mutating-webhook-configuration"

# values.yaml applied on every Kueue Helm install/upgrade
KUEUE_HELM_VALUES = """
controllerManager:
  manager:
    resources:
      limits:
        cpu: "6"
        memory: "24Gi"
      requests:
        cpu: "4000m"
        memory: "16Gi"
    configuration:
      clientConnection:
        qps: 1000      # High QPS to clear the 10,000 event backlog
        burst: 2000
      controller:
        groupKindConcurrency:
          Job.batch: 100               # Process finished jobs faster
          Workload.kueue.x-k8s.io: 100  # Admit new jobs faster
      # IMPORTANT: Native Kueue cleanup
      workloadRetentionPolicy:
        afterFinished: 5s    # Clean up the "Workload" 5s after the Job is done
"""

# ResourceFlavor + ClusterQueue + LocalQueue (execution namespace set at runtime)
# {cpu_quota} and {memory_quota} are filled from cluster allocatable resources
# {node_labels_spec} is an optional "spec:\n  nodeLabels:\n    key: value\n" block
KUEUE_QUEUES_YAML = """
apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata:
  name: default-flavor
spec:
  tolerations:
    - key: "dedicated"
      value: "batch"
      effect: "NoSchedule"   
{node_labels_spec}---
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata:
  name: {cluster_queue}
spec:
  namespaceSelector: {{}}
  resourceGroups:
  - coveredResources: ["cpu", "memory"]
    flavors:
    - name: default-flavor
      resources:
      - name: cpu
        nominalQuota: {cpu_quota}
      - name: memory
        nominalQuota: {memory_quota}
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: LocalQueue
metadata:
  namespace: {namespace}
  name: {queue_name}
spec:
  clusterQueue: {cluster_queue}
"""


def _parse_resource(val):
    """Parse Kubernetes resource quantity to numeric value. Returns 0 for None/missing."""
    if val is None:
        return 0
    try:
        return float(parse_quantity(val))
    except (ValueError, TypeError):
        return 0


def _format_node_labels_spec(node_labels):
    """Return a YAML 'spec.nodeLabels' block for a ResourceFlavor, or empty string."""
    if not node_labels:
        return ""
    lines = ["spec:", "  nodeLabels:"]
    for k, v in node_labels.items():
        lines.append(f"    {k}: {v}")
    return "\n".join(lines) + "\n"


def set_cluster_queue_stop_policy(stop_policy, kube_context=None):
    """Set the stopPolicy on the robovast ClusterQueue.

    Useful before bulk job deletion so Kueue does not admit new jobs during cleanup.
    Common values: ``"Hold"`` (pause new admissions), ``"HoldAndDrain"`` (pause and
    preempt running workloads), ``"None"`` or empty string to resume.

    Args:
        stop_policy: Policy string, e.g. ``"Hold"``.
        kube_context: Kubernetes context to use. ``None`` uses the active context.
    """
    from robovast.common.kube import load_kube_config
    load_kube_config(context=kube_context)
    custom_api = client.CustomObjectsApi()
    body = {"spec": {"stopPolicy": stop_policy if stop_policy else "None"}}
    try:
        custom_api.patch_cluster_custom_object(
            group=KUEUE_WORKLOAD_GROUP,
            version=KUEUE_WORKLOAD_VERSION,
            plural="clusterqueues",
            name=CLUSTER_QUEUE_NAME,
            body=body,
        )
        logger.debug(
            "ClusterQueue '%s' stopPolicy set to '%s'", CLUSTER_QUEUE_NAME, stop_policy
        )
    except client.rest.ApiException as e:
        if e.status == 404:
            logger.debug(
                "ClusterQueue '%s' not found, skipping stopPolicy patch", CLUSTER_QUEUE_NAME
            )
        else:
            logger.warning("Could not set ClusterQueue '%s' stopPolicy: %s", CLUSTER_QUEUE_NAME, e)


#: Raised by :func:`verify_kueue_admission_ready` when the admission path cannot be
#: *checked* (RBAC), as opposed to being broken. Callers warn and carry on: refusing to
#: run because we lack a read permission would be worse than the hang we are preventing.
class KueueCheckUnavailable(Exception):
    """The Kueue queue objects could not be read, so nothing can be concluded."""


def _queue_object(custom_api, plural, name, namespace=None):
    """Read one Kueue custom object, or ``None`` when it does not exist.

    Raises :class:`KueueCheckUnavailable` on a 403 so "we are not allowed to look"
    never reaches the caller as "it is not there" — the two demand opposite responses.
    """
    try:
        if namespace is None:
            return custom_api.get_cluster_custom_object(
                group=KUEUE_WORKLOAD_GROUP, version=KUEUE_WORKLOAD_VERSION,
                plural=plural, name=name)
        return custom_api.get_namespaced_custom_object(
            group=KUEUE_WORKLOAD_GROUP, version=KUEUE_WORKLOAD_VERSION,
            plural=plural, namespace=namespace, name=name)
    except client.rest.ApiException as e:
        if e.status == 404:
            return None
        if e.status == 403:
            raise KueueCheckUnavailable(
                f"not permitted to read {plural}/{name}"
                f"{f' in namespace {namespace}' if namespace else ''}: {e.reason}") from e
        raise


def verify_kueue_admission_ready(namespace="default", kube_context=None,
                                 settle_timeout=0.0):
    """Check that a scenario Job labelled into the robovast queue can be admitted.

    Every scenario and postprocess Job carries ``kueue.x-k8s.io/queue-name``, so Kueue
    creates it **suspended** and starts it only once LocalQueue ``robovast`` admits it
    through ClusterQueue ``robovast-cluster-queue``. When any link in that chain is
    missing the Job is not rejected — it simply stays suspended, with no pod, forever.
    Nothing about that state resembles a failure: the Job is "active", the campaign log
    says "still running", and ``activeDeadlineSeconds`` cannot save it because the timer
    does not run while a Job is suspended. So it must be checked up front.

    Raises :class:`~robovast.common.errors.CampaignConfigError` naming the broken link
    and the remedy. Raises :class:`KueueCheckUnavailable` when the objects cannot be
    read at all (RBAC) — callers downgrade that to a warning. Raises
    :class:`~robovast.common.errors.ClusterUnreachableError` when the API server does
    not answer, so an off cluster reads as one sentence rather than a urllib3 traceback.

    Deliberately does **not** look at quota. A queue whose capacity is currently used up
    is healthy and the correct response is to wait; only a structurally broken admission
    path is an error.

    Args:
        namespace: Namespace the jobs run in — the LocalQueue must live there too.
        kube_context: Kubernetes context to use. ``None`` uses the active context.
        settle_timeout: Seconds to keep retrying a failing check before raising. For a
            check run straight after ``apply_kueue_queues``, where Kueue may not have
            reconciled the ClusterQueue against its ResourceFlavor yet; leave at 0 for a
            queue that has been up for a while, so a real breakage fails immediately.
    """
    from robovast.common.errors import CampaignConfigError
    from robovast.common.kube import api_transport_errors, load_kube_config
    load_kube_config(context=kube_context)
    deadline = time.monotonic() + settle_timeout
    while True:
        try:
            with api_transport_errors(
                    f"checking the Kueue admission path in namespace '{namespace}'"):
                return _check_kueue_admission(namespace)
        except CampaignConfigError:
            if time.monotonic() >= deadline:
                raise
            logger.debug("Kueue admission path not ready yet; retrying until it settles")
            time.sleep(2)


def _check_kueue_admission(namespace):
    """One pass of :func:`verify_kueue_admission_ready` (no retry, config already loaded)."""
    from robovast.common.errors import CampaignConfigError
    custom_api = client.CustomObjectsApi()
    remedy = ("Run 'vast execution cluster setup' to (re)create the Kueue queues, "
              "or point the campaign at the namespace that has them.")

    local_queue = _queue_object(custom_api, "localqueues", KUEUE_QUEUE_NAME,
                                namespace=namespace)
    if local_queue is None:
        raise CampaignConfigError(
            f"Kueue LocalQueue '{KUEUE_QUEUE_NAME}' does not exist in namespace "
            f"'{namespace}', but every job is labelled into it "
            f"(kueue.x-k8s.io/queue-name={KUEUE_QUEUE_NAME}). Kueue would suspend the "
            f"jobs and never admit them.\n{remedy}")

    # The LocalQueue names its ClusterQueue; read it from the object rather than
    # assuming CLUSTER_QUEUE_NAME, so a hand-edited queue is diagnosed truthfully.
    cq_name = (local_queue.get("spec") or {}).get("clusterQueue") or CLUSTER_QUEUE_NAME
    cluster_queue = _queue_object(custom_api, "clusterqueues", cq_name)
    if cluster_queue is None:
        raise CampaignConfigError(
            f"Kueue ClusterQueue '{cq_name}' does not exist, but LocalQueue "
            f"'{KUEUE_QUEUE_NAME}' in namespace '{namespace}' points at it. Every job "
            f"would stay suspended with 'ClusterQueue {cq_name} doesn't exist'.\n"
            f"{remedy}")

    spec = cluster_queue.get("spec") or {}
    stop_policy = spec.get("stopPolicy")
    if stop_policy and stop_policy != "None":
        raise CampaignConfigError(
            f"Kueue ClusterQueue '{cq_name}' is stopped (stopPolicy={stop_policy}), so "
            f"no job will be admitted. Campaign cleanup holds the queue while it "
            f"deletes and resumes it afterwards; a cleanup that died in between leaves "
            f"it held.\nResume it with: kubectl patch clusterqueue {cq_name} "
            f"--type=merge -p '{{\"spec\":{{\"stopPolicy\":\"None\"}}}}'")

    # Kueue reports an unusable queue (most often a ResourceFlavor that does not exist)
    # as Active=False rather than by deleting anything, so the objects all look fine.
    active = next((c for c in (cluster_queue.get("status") or {}).get("conditions") or []
                   if c.get("type") == "Active"), None)
    if active is not None and active.get("status") != "True":
        raise CampaignConfigError(
            f"Kueue ClusterQueue '{cq_name}' is not active "
            f"({active.get('reason') or 'unknown reason'}: "
            f"{active.get('message') or 'no message'}), so no job will be admitted.\n"
            f"{remedy}")

    logger.debug("Kueue admission path ready: LocalQueue '%s' in '%s' -> ClusterQueue "
                 "'%s'", KUEUE_QUEUE_NAME, namespace, cq_name)


def workload_wait_reasons(namespace, job_names=None, k8s_custom=None):
    """Job name → Kueue's own explanation for each not-yet-admitted Workload.

    Kueue mirrors every suspended Job into a Workload and records why it is waiting in
    the ``QuotaReserved`` condition. That message ("insufficient unused quota for cpu",
    "ClusterQueue ... doesn't exist") is the only place the reason exists — the Job
    itself just says ``suspend: true``.

    Workloads do **not** inherit the jobs' ``jobgroup`` / ``campaign-id`` labels (see
    :func:`cleanup_kueue_workloads`), so they are listed by the queue label and mapped
    back to their owning Job; pass *job_names* to keep only one campaign's.

    Best-effort: returns ``{}`` rather than raising, because this only enriches a log
    line. The fail-loudly decision is :func:`verify_kueue_admission_ready`'s.
    """
    custom_api = k8s_custom or client.CustomObjectsApi()
    try:
        workloads = custom_api.list_namespaced_custom_object(
            group=KUEUE_WORKLOAD_GROUP, version=KUEUE_WORKLOAD_VERSION,
            plural=KUEUE_WORKLOAD_PLURAL, namespace=namespace,
            label_selector=f"kueue.x-k8s.io/queue-name={KUEUE_QUEUE_NAME}",
        ).get("items") or []
    except Exception as e:  # noqa: BLE001 - advisory only
        logger.debug("Could not list Kueue workloads in %s: %s", namespace, e)
        return {}
    wanted = set(job_names) if job_names is not None else None
    reasons = {}
    for wl in workloads:
        owners = (wl.get("metadata") or {}).get("ownerReferences") or []
        job_name = next((o.get("name") for o in owners if o.get("kind") == "Job"), None)
        if not job_name or (wanted is not None and job_name not in wanted):
            continue
        for cond in (wl.get("status") or {}).get("conditions") or []:
            if cond.get("type") == "QuotaReserved" and cond.get("status") != "True":
                reasons[job_name] = (cond.get("message")
                                     or cond.get("reason") or "not admitted")
                break
    return reasons


@contextlib.contextmanager
def cluster_queue_held(kube_context=None):
    """Hold the ClusterQueue for the duration, then restore what it was before.

    Only appropriate for a **cluster-wide** operation. The ClusterQueue is a single
    cluster-scoped object shared by every campaign, and ``stopPolicy: Hold`` stops all
    admissions in it — it is not scoped to one campaign's jobs (``Hold`` vs
    ``HoldAndDrain`` decides only whether *running* workloads are preempted, not whose
    workloads are affected). Holding it for a per-campaign operation stalls every other
    campaign's pending jobs, which is the same forever-suspended state this module's
    preflight exists to prevent.

    Restores the **previous** policy rather than forcing ``None``, so a hold that was
    already in place (a concurrent teardown, a deliberate manual hold) survives, and so
    an error inside the block can never leave the queue stopped for good.
    """
    previous = None
    try:
        cq = _queue_object(client.CustomObjectsApi(), "clusterqueues", CLUSTER_QUEUE_NAME)
        previous = (cq.get("spec") or {}).get("stopPolicy") if cq else None
    except Exception as e:  # noqa: BLE001 - fall back to the normal state
        logger.debug("Could not read the current ClusterQueue stopPolicy: %s", e)
    logger.info("Setting ClusterQueue stopPolicy to Hold")
    set_cluster_queue_stop_policy("Hold", kube_context=kube_context)
    try:
        yield
    finally:
        logger.info("Restoring ClusterQueue stopPolicy to '%s'", previous or "None")
        set_cluster_queue_stop_policy(previous, kube_context=kube_context)


def cleanup_kueue_workloads(
    namespace="default",
    label_selector=None,
    campaign_id=None,
    k8s_batch_client=None,
):
    """Delete Kueue Workload objects for scenario run jobs.

    Workloads don't inherit job labels (jobgroup, campaign-id). They use
    kueue.x-k8s.io/queue-name=robovast. When campaign_id is given, only workloads
    owned by jobs of that run are deleted (matched via ownerReferences and job
    UIDs). Without campaign_id, all workloads in the robovast queue are deleted.
    If Kueue is not installed (Workload CRD missing), logs and returns without
    failing.

    Args:
        namespace: Kubernetes namespace
        label_selector: Label selector used to list jobs for campaign_id scoping
        campaign_id: If given, only delete workloads for this campaign's jobs
        k8s_batch_client: BatchV1Api client; required when campaign_id is given
    """
    try:
        custom_api = client.CustomObjectsApi()
        delete_opts = client.V1DeleteOptions(
            grace_period_seconds=0, propagation_policy="Background"
        )
        queue_selector = "kueue.x-k8s.io/queue-name=robovast"

        if campaign_id is not None and k8s_batch_client is not None:
            # Collect UIDs of jobs belonging to this run so we only delete
            # the workloads that are owned by those jobs.
            job_uid_selector = label_selector or f"jobgroup=scenario-runs"
            try:
                job_list = k8s_batch_client.list_namespaced_job(
                    namespace=namespace, label_selector=job_uid_selector
                )
                campaign_job_uids = {job.metadata.uid for job in job_list.items}
            except client.rest.ApiException as e:
                logger.warning(f"Could not list jobs for run-scoped workload cleanup: {e}")
                campaign_job_uids = set()

            if not campaign_job_uids:
                logger.debug("No jobs found for campaign '%s', skipping workload cleanup", campaign_id)
                return

            # List all workloads in the queue and delete only those owned by
            # jobs of the target run.
            logger.debug(
                "Deleting Kueue workloads owned by %d job(s) for campaign '%s'",
                len(campaign_job_uids), campaign_id,
            )
            workloads = custom_api.list_namespaced_custom_object(
                group=KUEUE_WORKLOAD_GROUP,
                version=KUEUE_WORKLOAD_VERSION,
                namespace=namespace,
                plural=KUEUE_WORKLOAD_PLURAL,
                label_selector=queue_selector,
            )
            target_wls = [
                wl["metadata"]["name"]
                for wl in workloads.get("items", [])
                if {
                    ref["uid"]
                    for ref in (wl.get("metadata", {}).get("ownerReferences") or [])
                } & campaign_job_uids
            ]

            def _delete_workload(wl_name):
                try:
                    custom_api.delete_namespaced_custom_object(
                        group=KUEUE_WORKLOAD_GROUP,
                        version=KUEUE_WORKLOAD_VERSION,
                        namespace=namespace,
                        plural=KUEUE_WORKLOAD_PLURAL,
                        name=wl_name,
                        body=delete_opts,
                    )
                    return True
                except client.rest.ApiException as e:
                    if e.status == 404:
                        return True  # already gone
                    logger.warning(f"Could not delete workload '{wl_name}': {e}")
                    return False

            deleted = 0
            with ThreadPoolExecutor(max_workers=min(len(target_wls) or 1, 16)) as pool:
                futures = {pool.submit(_delete_workload, n): n for n in target_wls}
                for fut in as_completed(futures):
                    if fut.result():
                        deleted += 1
            logger.info(
                "Successfully deleted %d scenario-runs Kueue workload(s) for campaign '%s'",
                deleted, campaign_id,
            )
        else:
            # No campaign_id scoping: delete all robovast queue workloads at once
            logger.debug(f"Deleting all Kueue workloads with selector '{queue_selector}'")
            custom_api.delete_collection_namespaced_custom_object(
                group=KUEUE_WORKLOAD_GROUP,
                version=KUEUE_WORKLOAD_VERSION,
                namespace=namespace,
                plural=KUEUE_WORKLOAD_PLURAL,
                label_selector=queue_selector,
                body=delete_opts,
            )
            logger.info("Successfully deleted scenario-runs Kueue workloads")

        # Hard cleanup: force-remove finalizers from any workloads that are stuck in
        # Terminating (their own finalizers block deletion after the soft delete above).
        try:
            remaining = custom_api.list_namespaced_custom_object(
                group=KUEUE_WORKLOAD_GROUP,
                version=KUEUE_WORKLOAD_VERSION,
                namespace=namespace,
                plural=KUEUE_WORKLOAD_PLURAL,
                label_selector=queue_selector,
            )
            for wl in remaining.get("items", []):
                meta = wl.get("metadata", {})
                if meta.get("deletionTimestamp") or meta.get("finalizers"):
                    wl_name = meta["name"]
                    try:
                        custom_api.patch_namespaced_custom_object(
                            group=KUEUE_WORKLOAD_GROUP,
                            version=KUEUE_WORKLOAD_VERSION,
                            namespace=namespace,
                            plural=KUEUE_WORKLOAD_PLURAL,
                            name=wl_name,
                            body={"metadata": {"finalizers": None}},
                        )
                        logger.info(
                            "Removed finalizers from stuck Kueue workload '%s'", wl_name
                        )
                    except client.rest.ApiException as patch_err:
                        if patch_err.status != 404:
                            logger.warning(
                                "Could not patch workload '%s' finalizers: %s", wl_name, patch_err
                            )
        except client.rest.ApiException as list_err:
            logger.warning("Could not list workloads for finalizer hard-cleanup: %s", list_err)

    except client.rest.ApiException as e:
        if e.status == 404:
            logger.debug(
                "Kueue Workload CRD not found (Kueue may not be installed), skipping workload cleanup"
            )
        else:
            logger.error(f"Error deleting Kueue workloads: {e}")
            raise


def delete_kueue_webhook_configs(kube_context=None):
    """Delete Kueue's admission webhook configurations.

    The validating/mutating webhooks are served by the kueue-controller-manager
    pods. During teardown those pods may already be gone (scaled down, evicted,
    or removed by an earlier ``helm uninstall``), in which case the API server
    rejects *any* mutation of Kueue objects with ``no endpoints available for
    service "kueue-webhook-service"``. That blocks finalizer removal and leaves
    the ResourceFlavor/ClusterQueue stuck in Terminating. Deleting the webhook
    configurations first makes the subsequent patches unconditionally succeed.

    Args:
        kube_context: Kubernetes context to use. ``None`` uses the active context.
    """
    from robovast.common.kube import load_kube_config
    load_kube_config(context=kube_context)
    admission_api = client.AdmissionregistrationV1Api()
    for name, deleter in [
        (KUEUE_VALIDATING_WEBHOOK_CONFIG,
         admission_api.delete_validating_webhook_configuration),
        (KUEUE_MUTATING_WEBHOOK_CONFIG,
         admission_api.delete_mutating_webhook_configuration),
    ]:
        try:
            deleter(name=name)
            logger.info("Deleted Kueue webhook configuration '%s'", name)
        except client.rest.ApiException as e:
            if e.status == 404:
                logger.debug("Webhook configuration '%s' not found, skipping", name)
            else:
                logger.warning(
                    "Could not delete webhook configuration '%s': %s", name, e
                )


def cleanup_kueue_cluster_resources(kube_context=None):
    """Force-remove finalizers from ClusterQueue and ResourceFlavor.

    Called during cluster teardown to unblock deletion of Kueue cluster-scoped
    resources that may be stuck with finalizers preventing ``helm uninstall`` from
    completing cleanly.

    Args:
        kube_context: Kubernetes context to use. ``None`` uses the active context.
    """
    from robovast.common.kube import load_kube_config
    load_kube_config(context=kube_context)
    # Remove the admission webhooks first: if the kueue-controller-manager pods
    # are already gone, the webhook calls fail and block the finalizer patches
    # below, leaving the resources stuck Terminating.
    delete_kueue_webhook_configs(kube_context=kube_context)
    custom_api = client.CustomObjectsApi()
    patch_body = {"metadata": {"finalizers": None}}
    for plural, name, label in [
        ("clusterqueues", CLUSTER_QUEUE_NAME, "ClusterQueue"),
        ("resourceflavors", KUEUE_RESOURCE_FLAVOR_NAME, "ResourceFlavor"),
    ]:
        try:
            custom_api.patch_cluster_custom_object(
                group=KUEUE_WORKLOAD_GROUP,
                version=KUEUE_WORKLOAD_VERSION,
                plural=plural,
                name=name,
                body=patch_body,
            )
            logger.info("Removed finalizers from %s '%s'", label, name)
        except client.rest.ApiException as e:
            if e.status == 404:
                logger.debug("%s '%s' not found, skipping finalizer patch", label, name)
            else:
                logger.warning(
                    "Could not remove finalizers from %s '%s': %s", label, name, e
                )


def get_cluster_allocatable_resources(kube_context=None, cluster_config=None):
    """Return total allocatable CPU and memory for Kueue quota.

    Resolution order:

    1. If *cluster_config* is provided, delegate to
       ``cluster_config.get_cluster_allocatable_resources(kube_context)``.
       A provider-specific override (e.g. GCP) can query the autoscaler for
       the true *maximum* capacity, which is correct for autoscaling clusters.
    2. Otherwise query the Kubernetes node API: sums the **total allocatable**
       resources across all current nodes (no subtracting of current pod
       requests — Kueue manages quota itself).

    Fails loudly if capacity cannot be determined. There is deliberately no
    hard-coded default: silently provisioning a tiny quota (the previous 8 CPU /
    32Gi) would throttle every future campaign's admission to a fraction of a
    large cluster with only a log line. A cluster whose capacity cannot be read
    is a setup error to fix, not to paper over.

    Args:
        kube_context: Kubernetes context to use. None uses the active context.
        cluster_config: Optional :class:`BaseConfig` instance whose
            ``get_cluster_allocatable_resources`` override is tried first.

    Returns:
        tuple: (cpu_quota: int, memory_quota: str) e.g. (64, "256Gi").

    Raises:
        RuntimeError: if allocatable capacity cannot be determined.
    """
    # 1. Provider-specific override (e.g. GKE autoscaler max capacity)
    if cluster_config is not None:
        try:
            provider_cpu, provider_mem = cluster_config.get_cluster_allocatable_resources(
                kube_context=kube_context
            )
            if provider_cpu is not None and provider_mem is not None:
                return provider_cpu, provider_mem
        except Exception as exc:
            logger.warning(
                "cluster_config.get_cluster_allocatable_resources failed: %s. "
                "Falling back to K8s node query.",
                exc,
            )

    # 2. Generic K8s node query — total allocatable (autoscaling-safe)
    try:
        from robovast.common.kube import load_kube_config
        load_kube_config(context=kube_context)

        v1 = client.CoreV1Api()
        total_allocatable_cpu = 0.0
        total_allocatable_mem = 0  # bytes

        nodes = v1.list_node()
        for node in nodes.items:
            alloc = node.status.allocatable or {}
            total_allocatable_cpu += _parse_resource(alloc.get("cpu"))
            total_allocatable_mem += int(_parse_resource(alloc.get("memory")))

        if total_allocatable_cpu <= 0:
            raise RuntimeError(
                f"No allocatable CPU found across {len(nodes.items)} cluster "
                "node(s); cannot size the Kueue quota."
            )

        cpu_quota = max(1, int(total_allocatable_cpu))
        memory_gi = max(1, total_allocatable_mem // (1024**3))
        memory_quota = f"{memory_gi}Gi"

        logger.info(
            "Cluster total allocatable: %d CPU(s), %s (from %d node(s))",
            cpu_quota,
            memory_quota,
            len(nodes.items),
        )
        return cpu_quota, memory_quota

    except Exception as e:
        raise RuntimeError(
            f"Failed to query cluster resources for the Kueue quota: {e}"
        ) from e


# CRDs that must be established before we can create queue resources
_KUEUE_CRDS = [
    "clusterqueues.kueue.x-k8s.io",
    "resourceflavors.kueue.x-k8s.io",
    "localqueues.kueue.x-k8s.io",
]


def _wait_for_kueue_crds(ctx_kubectl, timeout=120):
    """Wait until all critical Kueue CRDs are established (and not terminating).

    After ``helm uninstall`` the CRDs enter a Terminating state; after a fresh
    ``helm install`` they are re-created.  ``kubectl wait --for=condition=established``
    blocks until the CRD is fully ready, which covers both cases.

    Args:
        ctx_kubectl: list of kubectl context flags, e.g. ``["--context", "my-ctx"]``.
        timeout: seconds to wait per CRD.

    Returns:
        list[str]: the CRDs that did **not** become established within *timeout*
        (missing, or still Terminating).  An empty list means all are ready.
    """
    not_ready = []
    for crd in _KUEUE_CRDS:
        result = subprocess.run(
            ["kubectl"] + ctx_kubectl + [
                "wait",
                "--for=condition=established",
                f"crd/{crd}",
                f"--timeout={timeout}s",
            ],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            logger.debug(
                "kubectl wait for CRD '%s' returned non-zero (may not exist yet): %s",
                crd, result.stderr,
            )
            not_ready.append(crd)
    return not_ready


def _force_apply_kueue_crds(ctx_helm, ctx_kubectl):
    """Server-side apply the chart's CRDs from the installed release manifest.

    Works around occasional partial installs where ``helm install`` reports
    success but one CRD (in practice the large ``clusterqueues`` CRD) never
    lands — e.g. because a stale copy from a prior teardown was still
    Terminating when Helm tried to create it.  Server-side apply re-creates any
    missing CRD without disturbing the running controller.

    Returns:
        bool: True if the apply was attempted and succeeded.
    """
    manifest = subprocess.run(
        ["helm"] + ctx_helm + [
            "get", "manifest", KUEUE_HELM_RELEASE, "-n", KUEUE_NAMESPACE,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if manifest.returncode != 0 or not manifest.stdout.strip():
        logger.warning(
            "Could not fetch Kueue chart manifest for CRD recovery: %s",
            manifest.stderr,
        )
        return False
    crd_docs = [
        doc for doc in manifest.stdout.split("\n---\n")
        if "kind: CustomResourceDefinition" in doc
    ]
    if not crd_docs:
        logger.warning("No CRDs found in Kueue chart manifest; cannot recover.")
        return False
    applied = subprocess.run(
        ["kubectl"] + ctx_kubectl + [
            "apply", "--server-side", "--force-conflicts", "-f", "-",
        ],
        input="\n---\n".join(crd_docs),
        capture_output=True,
        text=True,
        check=False,
    )
    if applied.returncode != 0:
        logger.warning(
            "Force server-side apply of Kueue CRDs failed: %s", applied.stderr
        )
        return False
    logger.info("Force-applied %d Kueue CRD(s) from chart manifest", len(crd_docs))
    return True


def _ensure_kueue_crds(ctx_helm, ctx_kubectl, timeout=120):
    """Verify all critical Kueue CRDs are established, self-healing if not.

    Turns a silent partial install into a deterministic outcome: wait for the
    CRDs, force-apply them from the chart manifest if any are missing, and raise
    a clear, actionable error if they still cannot be established.  Without this,
    a missing ``clusterqueues`` CRD lets ``setup`` finish "successfully" while
    the ClusterQueue never gets created — so every job runs unmanaged with no
    admission control.
    """
    not_ready = _wait_for_kueue_crds(ctx_kubectl, timeout=timeout)
    if not_ready:
        logger.warning(
            "Kueue CRD(s) not established after Helm operation: %s. "
            "Force-applying CRDs from the chart manifest...",
            not_ready,
        )
        _force_apply_kueue_crds(ctx_helm, ctx_kubectl)
        not_ready = _wait_for_kueue_crds(ctx_kubectl, timeout=60)
    if not_ready:
        raise RuntimeError(
            "Kueue installation incomplete: CRD(s) missing or not established "
            f"even after recovery: {not_ready}. The ClusterQueue cannot be "
            "created, so jobs would run unmanaged (no admission control). "
            "Run `vast execution cluster cleanup` and then `setup` again."
        )


def _run_helm(args, check=True):
    """Run helm command. Returns (success, stderr)."""
    cmd = ["helm"] + args
    logger.debug("Running: %s", " ".join(cmd))
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if result.returncode != 0:
        logger.warning("Helm command failed: %s", result.stderr)
        if check:
            raise RuntimeError(
                f"Helm command failed: {result.stderr or result.stdout}"
            )
        return False, result.stderr or ""
    return True, ""


def _run_kubectl_apply(yaml_content, check=True, kube_context=None):
    """Apply YAML via kubectl. Returns success."""
    ctx_args = ["--context", kube_context] if kube_context else []
    cmd = ["kubectl"] + ctx_args + ["apply", "-f", "-"]
    logger.debug("Applying Kueue queue manifests via kubectl")
    result = subprocess.run(
        cmd,
        input=yaml_content,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        logger.warning("kubectl apply failed: %s", result.stderr)
        if check:
            raise RuntimeError(
                f"kubectl apply failed: {result.stderr or result.stdout}"
            )
        return False
    return True


def install_kueue_helm(kube_context=None):
    """Install Kueue via Helm in kueue-system namespace.

    Requires Helm to be installed and in PATH.
    If Kueue is already installed, upgrades to the specified version.

    Args:
        kube_context: Kubernetes context to use. None uses the active context.
    """
    ctx_helm = [f"--kube-context={kube_context}"] if kube_context else []
    ctx_kubectl = ["--context", kube_context] if kube_context else []
    result = subprocess.run(
        ["helm", "list", "-n", KUEUE_NAMESPACE, "-q", "-f", KUEUE_HELM_RELEASE] + ctx_helm,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        logger.info(
            "Kueue Helm release already exists, upgrading to version %s",
            KUEUE_HELM_VERSION,
        )
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", prefix="kueue_values_", delete=False
        ) as vf:
            vf.write(KUEUE_HELM_VALUES)
            values_path = vf.name
        try:
            _run_helm(
                [
                    "upgrade",
                    KUEUE_HELM_RELEASE,
                    KUEUE_HELM_REPO,
                    f"--version={KUEUE_HELM_VERSION}",
                    f"--namespace={KUEUE_NAMESPACE}",
                    f"--values={values_path}",
                ] + ctx_helm
            )
        finally:
            os.unlink(values_path)
        # Wait for CRDs after upgrade (upgrade may update CRDs).
        # Verify ALL critical Kueue CRDs and self-heal / fail loudly if any are
        # missing, rather than proceeding with a broken queue.
        _ensure_kueue_crds(ctx_helm, ctx_kubectl, timeout=60)
        return

    logger.info("Installing Kueue via Helm in namespace %s...", KUEUE_NAMESPACE)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", prefix="kueue_values_", delete=False
    ) as vf:
        vf.write(KUEUE_HELM_VALUES)
        values_path = vf.name
    try:
        _run_helm(
            [
                "install",
                KUEUE_HELM_RELEASE,
                KUEUE_HELM_REPO,
                f"--version={KUEUE_HELM_VERSION}",
                "--create-namespace",
                f"--namespace={KUEUE_NAMESPACE}",
                f"--values={values_path}",
            ] + ctx_helm
        )
    finally:
        os.unlink(values_path)
    logger.info("Kueue installed successfully. Waiting for controller and CRDs...")
    # Verify ALL critical Kueue CRDs are established; self-heal a partial install
    # (e.g. a missing clusterqueues CRD) by force-applying the chart CRDs, and
    # fail loudly if that still cannot establish them.  This also covers the case
    # where a previous uninstall left CRDs Terminating when helm tried to create.
    _ensure_kueue_crds(ctx_helm, ctx_kubectl, timeout=120)
    # Wait for deployment to be ready
    subprocess.run(
        ["kubectl"] + ctx_kubectl + [
            "rollout",
            "status",
            "deployment/kueue-controller-manager",
            "-n",
            KUEUE_NAMESPACE,
            "--timeout=120s",
        ],
        capture_output=True,
        check=False,
    )


def uninstall_kueue_helm(kube_context=None):
    """Uninstall Kueue Helm release from kueue-system namespace.

    Args:
        kube_context: Kubernetes context to use. None uses the active context.
    """
    logger.info("Uninstalling Kueue Helm release...")
    # Force-clear finalizers from cluster-scoped Kueue resources first so that helm
    # uninstall does not hang waiting for them to be garbage-collected.
    cleanup_kueue_cluster_resources(kube_context=kube_context)
    ctx_helm = [f"--kube-context={kube_context}"] if kube_context else []
    ok, err = _run_helm(
        ["uninstall", KUEUE_HELM_RELEASE, f"--namespace={KUEUE_NAMESPACE}"] + ctx_helm,
        check=False,
    )
    if not ok:
        if "release: not found" in (err or "").lower():
            logger.info("Kueue Helm release not found, skipping uninstall")
        else:
            raise RuntimeError(f"Failed to uninstall Kueue: {err}")


def apply_kueue_queues(namespace="default", kube_context=None, node_labels=None,
                       cluster_config=None):
    """Create ResourceFlavor, ClusterQueue, and LocalQueue for RoboVAST.

    Quotas are set from cluster allocatable CPU and memory.

    Args:
        namespace: Kubernetes namespace for the LocalQueue (execution namespace)
        kube_context: Kubernetes context to use. None uses the active context.
        node_labels: Optional dict of node labels to add to the ResourceFlavor spec
            (e.g. {"node-pool": "primary"}). Jobs will only run on matching nodes.
        cluster_config: Optional :class:`BaseConfig` instance.  When provided,
            its ``get_cluster_allocatable_resources`` override is used first so
            that autoscaling clusters report their maximum possible capacity.
    """
    cpu_quota, memory_quota = get_cluster_allocatable_resources(
        kube_context=kube_context, cluster_config=cluster_config
    )
    yaml_content = KUEUE_QUEUES_YAML.format(
        namespace=namespace,
        queue_name=KUEUE_QUEUE_NAME,
        cluster_queue=CLUSTER_QUEUE_NAME,
        cpu_quota=cpu_quota,
        memory_quota=memory_quota,
        node_labels_spec=_format_node_labels_spec(node_labels),
    ).strip()

    # Retry to handle the race where a CRD from a previous uninstall is still
    # in Terminating state when we try to create resources.  Each attempt
    # re-waits for the CRDs to be fully established before applying.
    ctx_kubectl = ["--context", kube_context] if kube_context else []
    max_attempts = 6
    retry_delay = 10  # seconds between retries
    for attempt in range(1, max_attempts + 1):
        result = subprocess.run(
            ["kubectl"] + ctx_kubectl + ["apply", "-f", "-"],
            input=yaml_content,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if result.returncode == 0:
            break
        stderr = result.stderr or result.stdout or ""
        if "custom resource definition is terminating" in stderr and attempt < max_attempts:
            logger.warning(
                "Kueue CRD still terminating; waiting %ds before retry (attempt %d/%d)...",
                retry_delay, attempt, max_attempts,
            )
            _wait_for_kueue_crds(ctx_kubectl, timeout=retry_delay * max_attempts)
            time.sleep(retry_delay)
        else:
            logger.warning("kubectl apply failed: %s", stderr)
            raise RuntimeError(f"kubectl apply failed: {stderr}")

    logger.info(
        "Kueue queues configured: LocalQueue '%s' in namespace '%s'",
        KUEUE_QUEUE_NAME,
        namespace,
    )


def prepare_kueue_setup(output_dir, namespace="default", kube_context=None, node_labels=None,
                        cluster_config=None):
    """Write Kueue queue manifests and README to output_dir.

    Quotas are set from cluster allocatable CPU and memory; raises if the cluster
    capacity cannot be read (no silent default quota).

    Args:
        output_dir: Directory to write files
        namespace: Kubernetes namespace for LocalQueue
        kube_context: Kubernetes context to use. None uses the active context.
        node_labels: Optional dict of node labels to add to the ResourceFlavor spec
            (e.g. {"node-pool": "primary"}). Jobs will only run on matching nodes.
        cluster_config: Optional :class:`BaseConfig` instance passed through to
            ``get_cluster_allocatable_resources``.
    """
    cpu_quota, memory_quota = get_cluster_allocatable_resources(
        kube_context=kube_context, cluster_config=cluster_config
    )
    yaml_content = KUEUE_QUEUES_YAML.format(
        namespace=namespace,
        queue_name=KUEUE_QUEUE_NAME,
        cluster_queue=CLUSTER_QUEUE_NAME,
        cpu_quota=cpu_quota,
        memory_quota=memory_quota,
        node_labels_spec=_format_node_labels_spec(node_labels),
    ).strip()
    kueue_file = f"{output_dir}/kueue-queue-setup.yaml"
    with open(kueue_file, "w") as f:
        f.write(yaml_content)

    readme = f"""# Kueue Setup Instructions

Kueue is installed for job queueing when you run `vast execution cluster setup`.

## 1. Install Kueue via Helm

```bash
helm install kueue oci://registry.k8s.io/kueue/charts/kueue \\
  --version={KUEUE_HELM_VERSION} --create-namespace --namespace={KUEUE_NAMESPACE}
```

Requires [Helm](https://helm.sh/) to be installed.

## 2. Apply ResourceFlavor, ClusterQueue, and LocalQueue

Wait for Kueue CRDs to be established, then apply:

```bash
kubectl wait --for=condition=established crd/resourceflavors.kueue.x-k8s.io --timeout=60s
kubectl apply -f kueue-queue-setup.yaml
```

This creates:
- ResourceFlavor `default-flavor`
- ClusterQueue `{CLUSTER_QUEUE_NAME}` (cpu/memory quotas)
- LocalQueue `{KUEUE_QUEUE_NAME}` in namespace `{namespace}`
"""
    readme_path = f"{output_dir}/README_kueue.md"
    with open(readme_path, "w") as f:
        f.write(readme)
    logger.debug("Wrote %s and %s", kueue_file, readme_path)
