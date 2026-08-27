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
import hashlib
import logging
import os
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import yaml
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

#: Kueue reads a Job's priority off this **label** (an annotation is ignored, exactly as
#: for the queue-name label). It names a WorkloadPriorityClass, which orders the
#: ClusterQueue's *pending* workloads only -- unlike a pod ``priorityClassName`` it never
#: reaches kube-scheduler, so a higher-priority campaign never preempts running work.
KUEUE_PRIORITY_LABEL = "kueue.x-k8s.io/priority-class"
KUEUE_PRIORITY_CLASS_PLURAL = "workloadpriorityclasses"

#: ``jobgroup`` value on a campaign's WorkloadPriorityClass. The class carries the same
#: ``jobgroup``/``campaign-id`` labels as the campaign's Jobs and Pods so it is removed by
#: the ordinary campaign-scoped cleanup selector rather than by a GC pass of its own.
CAMPAIGN_PRIORITY_JOBGROUP = "campaign-priority"
CAMPAIGN_PRIORITY_CLASS_PREFIX = "robovast-campaign-"

#: Priority is ``_PRIORITY_BASE - seconds since _PRIORITY_REF``, so an older campaign
#: outranks a younger one forever and no label is ever rewritten as campaigns come and go.
#: The origin is arbitrary but fixed: it only has to keep the result inside int32, which
#: it does for ~63 years.
_PRIORITY_REF = datetime(2026, 1, 1)
_PRIORITY_BASE = 2_000_000_000

#: The taint a campaign node may carry, and so what anything that has to run *where campaigns
#: run* must tolerate. Named here because this is where the ResourceFlavor granting it is
#: written; ``image_warm`` reads it so its DaemonSet cannot drift from the flavor and skip
#: precisely the nodes worth warming.
KUEUE_JOB_TOLERATIONS = ({"key": "dedicated", "value": "batch", "effect": "NoSchedule"},)

#: Duplicated from :mod:`.kubernetes_gpu` rather than imported, to keep the dependency
#: one-way (that module imports this one for the helm and quantity helpers).
GPU_RESOURCE = "nvidia.com/gpu"

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
      # Requests are a RESERVATION the scheduler subtracts from the node for as long as
      # the controller exists; limits are only a ceiling it may burst to. Reserving 4
      # cores / 16Gi for the controller made the largest single hole in the capacity the
      # quota below is sized from -- on a one-node cluster it is 4 of the ~6 cores that
      # campaign pods can never have, while the queue happily admitted work for all of
      # them. The burst headroom (and the QPS/concurrency tuning under it, which is what
      # clears a large event backlog) is unchanged: only the reservation shrinks.
      limits:
        cpu: "6"
        memory: "24Gi"
      requests:
        cpu: "500m"
        memory: "4Gi"
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

# Built as dicts and serialised, not formatted into a YAML string. The string version
# grew a duplicate mapping key: it opened `spec:` for the flavor's tolerations and the
# node-label helper appended a *second* `spec:`, so PyYAML (last wins) silently dropped
# the toleration and kubectl -- stricter about duplicate keys -- could reject the document
# outright. It also emitted an unquoted `True` where a node label value must be a string.
# Neither bug is expressible here, which is the point: a mapping cannot have the same key
# twice, and safe_dump quotes what needs quoting.
def _queue_manifests(namespace, queue_name, cluster_queue, cpu_quota, memory_quota,
                     node_labels=None, gpu_quota=0):
    """The ResourceFlavor + ClusterQueue + LocalQueue trio, as manifest dicts."""
    flavor_spec = {"tolerations": [dict(x) for x in KUEUE_JOB_TOLERATIONS]}
    if node_labels:
        # Label VALUES are strings to Kubernetes; a bare YAML `true` or `3` is rejected
        # by the API server, and str() here is what keeps a `.vast` from having to quote.
        flavor_spec["nodeLabels"] = {str(k): str(v) for k, v in node_labels.items()}

    covered = ["cpu", "memory"]
    resources = [
        {"name": "cpu", "nominalQuota": cpu_quota},
        {"name": "memory", "nominalQuota": memory_quota},
    ]
    # Omitted entirely at zero rather than written as `nominalQuota: 0`. Both block
    # admission, but an absent resource gets Kueue's clearer "not covered by ClusterQueue"
    # diagnosis, while a zero looks deliberately configured -- and absence is what keeps a
    # CPU-only cluster's manifests identical to what it had before GPUs existed here.
    if gpu_quota:
        covered.append(GPU_RESOURCE)
        resources.append({"name": GPU_RESOURCE, "nominalQuota": int(gpu_quota)})

    return [
        {
            "apiVersion": "kueue.x-k8s.io/v1beta2",
            "kind": "ResourceFlavor",
            "metadata": {"name": KUEUE_RESOURCE_FLAVOR_NAME},
            "spec": flavor_spec,
        },
        {
            "apiVersion": "kueue.x-k8s.io/v1beta2",
            "kind": "ClusterQueue",
            "metadata": {"name": cluster_queue},
            "spec": {
                "namespaceSelector": {},
                "resourceGroups": [{
                    "coveredResources": covered,
                    "flavors": [{
                        "name": KUEUE_RESOURCE_FLAVOR_NAME,
                        "resources": resources,
                    }],
                }],
            },
        },
        {
            "apiVersion": "kueue.x-k8s.io/v1beta2",
            "kind": "LocalQueue",
            "metadata": {"namespace": namespace, "name": queue_name},
            "spec": {"clusterQueue": cluster_queue},
        },
    ]


def campaign_priority_value(campaign_id: str) -> int:
    """The Kueue priority for *campaign_id*: higher means started earlier.

    Kueue orders a ClusterQueue's pending workloads by priority, then by Workload
    ``creationTimestamp``. That timestamp is the wrong key for a search campaign: its
    batches are submitted one after another, so a long-running campaign's later batches
    are always *younger* than a campaign that started after it, and the two end up
    taking turns instead of the older one finishing first.

    Deriving the priority from the campaign's own start time fixes that, and because the
    value is monotone in that start time the ordering is permanent -- no label is ever
    rewritten, and nothing has to know which campaigns are currently live.

    The start time is read back out of the campaign id (``<name>-YYYY-MM-DD-HHMMSScc``),
    which is where it already lives; see ``campaign_id_for``. The two datetimes are
    subtracted **naively**, never via ``.timestamp()``: this must be monotone in the
    wall-clock label, and going through epoch seconds would fold the repeated hour of a
    DST fall-back onto itself and invert two campaigns' order.

    Raises:
        CampaignConfigError: *campaign_id* carries no parsable timestamp. Every campaign
            id has one by construction (``is_campaign_dir`` enforces the shape), and
            quietly handing an odd one the lowest possible priority would starve it
            behind every other campaign for as long as it ran.
    """
    from robovast.common.errors import CampaignConfigError
    from robovast.common.execution import get_campaign_timestamp

    stamp = get_campaign_timestamp(campaign_id)
    try:
        # Hundredths of a second may follow HHMMSS; seconds resolution is plenty to
        # separate two campaigns, and the ids are unique regardless.
        started = datetime.strptime(stamp[:17], "%Y-%m-%d-%H%M%S")
    except ValueError as exc:
        raise CampaignConfigError(
            f"Campaign id '{campaign_id}' has no parsable "
            f"'<name>-YYYY-MM-DD-HHMMSS' timestamp, so its Kueue priority cannot be "
            f"derived and the campaign would queue behind every other one."
        ) from exc
    return _PRIORITY_BASE - int((started - _PRIORITY_REF).total_seconds())


#: Kubernetes' cap on a label VALUE. Lower than the 253 a label value's *object* namesake
#: may use, and the binding one here: this string is both the WorkloadPriorityClass's name
#: and the value every Job carries under :data:`KUEUE_PRIORITY_LABEL` to reference it.
_LABEL_VALUE_MAX = 63


def campaign_priority_class_name(campaign_id: str) -> str:
    """Name of the WorkloadPriorityClass carrying *campaign_id*'s priority.

    Bounded by what a **label value** may be, not by what an object name may be. The two
    limits differ -- 63 against 253 -- and this string has to satisfy both, because the
    Jobs reference the class by carrying its name as a label. Capping at 253 produced a
    Job the API server refused outright (``422 ... must be no more than 63 characters``)
    for every campaign whose id ran past 45 characters, which with the 20-character
    timestamp means any campaign *name* over about 25. It failed at Job creation -- after
    the image build and the whole variation phase -- and surfaced as a raw Kubernetes
    traceback rather than as anything about the name.

    A long id keeps a digest of itself rather than being truncated flat. Truncation alone
    would be worse than the error it fixes: two long names collapse onto one class, so two
    campaigns would silently share a priority, and the campaign-scoped cleanup that removes
    the class by label would take the other one's with it.
    """
    from .cluster_execution import _label_safe_campaign  # noqa: PLC0415 - avoids a cycle

    # _label_safe_campaign already yields an RFC 1123 name; the prefix keeps these
    # distinguishable from any priority class an operator defined by hand.
    name = CAMPAIGN_PRIORITY_CLASS_PREFIX + _label_safe_campaign(campaign_id)
    if len(name) <= _LABEL_VALUE_MAX:
        return name
    digest = hashlib.sha256(campaign_id.encode()).hexdigest()[:8]
    # rstrip: a label value may not end in a separator, which a cut can easily land on.
    return name[:_LABEL_VALUE_MAX - len(digest) - 1].rstrip("-.") + "-" + digest


def campaign_priority_class_manifest(campaign_id: str) -> dict:
    """The WorkloadPriorityClass for *campaign_id*, as a manifest dict."""
    from .cluster_execution import _label_safe_campaign  # noqa: PLC0415 - avoids a cycle

    return {
        "apiVersion": f"{KUEUE_WORKLOAD_GROUP}/{KUEUE_WORKLOAD_VERSION}",
        "kind": "WorkloadPriorityClass",
        "metadata": {
            "name": campaign_priority_class_name(campaign_id),
            # The same pair every Job and Pod of the campaign carries, so the ordinary
            # campaign-scoped cleanup selector removes this too.
            "labels": {
                "jobgroup": CAMPAIGN_PRIORITY_JOBGROUP,
                "campaign-id": _label_safe_campaign(campaign_id),
            },
        },
        "value": campaign_priority_value(campaign_id),
        "description": f"RoboVAST campaign {campaign_id}, prioritized by start time",
    }


def ensure_campaign_priority_class(campaign_id, kube_context=None) -> str:
    """Create *campaign_id*'s WorkloadPriorityClass if absent; return its name.

    Idempotent, so every batch may call it: the campaign's priority never changes, and a
    second create is a 409 that means the first one worked.

    Fails loudly on anything else. Kueue rejects a Job whose priority-class label names a
    class that does not exist, so submitting the batch anyway would fail job-by-job with
    a webhook error instead of once, here, with the reason.
    """
    from .kube_client import load_kube_config

    load_kube_config(context=kube_context)
    manifest = campaign_priority_class_manifest(campaign_id)
    name = manifest["metadata"]["name"]
    try:
        client.CustomObjectsApi().create_cluster_custom_object(
            group=KUEUE_WORKLOAD_GROUP, version=KUEUE_WORKLOAD_VERSION,
            plural=KUEUE_PRIORITY_CLASS_PLURAL, body=manifest)
        logger.info("Created Kueue priority class '%s' (value %d) for campaign '%s'",
                    name, manifest["value"], campaign_id)
    except client.rest.ApiException as exc:
        if exc.status != 409:
            raise
        logger.debug("Kueue priority class '%s' already exists", name)
    return name


def cleanup_campaign_priority_classes(campaign=None, kube_context=None):
    """Delete the WorkloadPriorityClass(es) RoboVAST created for campaigns.

    Scoped to *campaign* when given, otherwise every campaign priority class -- the same
    two-way scoping the Job and Pod cleanups use, off the same labels.

    Must run **after** the campaign's Workloads and Jobs are gone. Deleting the class
    cannot disturb work already queued (Kueue copies the resolved value onto each
    Workload when it creates it), but a Job created against a missing class is rejected,
    so removing it while the campaign still submits would break the campaign.
    """
    from .cluster_execution import _label_safe_campaign  # noqa: PLC0415 - avoids a cycle

    selector = f"jobgroup={CAMPAIGN_PRIORITY_JOBGROUP}"
    if campaign is not None:
        selector += f",campaign-id={_label_safe_campaign(campaign)}"
    try:
        client.CustomObjectsApi().delete_collection_cluster_custom_object(
            group=KUEUE_WORKLOAD_GROUP, version=KUEUE_WORKLOAD_VERSION,
            plural=KUEUE_PRIORITY_CLASS_PLURAL, label_selector=selector,
            body=client.V1DeleteOptions(grace_period_seconds=0,
                                        propagation_policy="Background"))
        logger.debug("Deleted Kueue priority classes matching '%s'", selector)
    except client.rest.ApiException as e:
        if e.status == 404:
            logger.debug("WorkloadPriorityClass CRD not found; skipping priority cleanup")
        else:
            # Advisory: a leftover priority class is inert (nothing references it once the
            # campaign's jobs are gone) and the next full cleanup removes it. Failing the
            # campaign's teardown over it would be worse than the litter.
            logger.warning("Could not delete Kueue priority classes (%s): %s", selector, e)


def _parse_resource(val):
    """Parse Kubernetes resource quantity to numeric value. Returns 0 for None/missing."""
    if val is None:
        return 0
    try:
        return float(parse_quantity(val))
    except (ValueError, TypeError):
        return 0


def set_cluster_queue_stop_policy(stop_policy, kube_context=None):
    """Set the stopPolicy on the robovast ClusterQueue.

    Useful before bulk job deletion so Kueue does not admit new jobs during cleanup.
    Common values: ``"Hold"`` (pause new admissions), ``"HoldAndDrain"`` (pause and
    preempt running workloads), ``"None"`` or empty string to resume.

    Args:
        stop_policy: Policy string, e.g. ``"Hold"``.
        kube_context: Kubernetes context to use. ``None`` uses the active context.
    """
    from .kube_client import load_kube_config
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


def _crd_registered(custom_api, plural) -> bool:
    """Whether *plural* exists as a Kueue resource type at all.

    Distinguishes "the object is absent" from "the kind is not installed" -- the API
    returns 404 for both, and only the second means Kueue's install is broken. Errs
    towards ``True``: a check that cannot answer must not accuse the install.
    """
    try:
        custom_api.list_cluster_custom_object(
            group=KUEUE_WORKLOAD_GROUP, version=KUEUE_WORKLOAD_VERSION,
            plural=plural, limit=1)
        return True
    except client.rest.ApiException as e:
        return e.status != 404
    except Exception:  # noqa: BLE001 - a diagnostic detail must never mask the real error
        return True


def verify_kueue_admission_ready(namespace="default", kube_context=None,
                                 settle_timeout=0.0, required_resources=()):
    """Check that a scenario Job labeled into the robovast queue can be admitted.

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

    Deliberately does **not** look at quota *utilization*. A queue whose capacity is
    currently used up is healthy and the correct response is to wait; only a structurally
    broken admission path is an error.

    ``required_resources`` is the one apparent exception, and it is coverage rather than
    utilization: Kueue does not reject a workload asking for a resource no resourceGroup
    covers, nor one asking for more than the nominal quota of a resource that is covered.
    It suspends it, permanently. Neither can ever be admitted no matter how long the
    caller waits, so both are errors and not waits -- which is exactly the distinction
    the paragraph above draws.

    Args:
        namespace: Namespace the jobs run in — the LocalQueue must live there too.
        kube_context: Kubernetes context to use. ``None`` uses the active context.
        settle_timeout: Seconds to keep retrying a failing check before raising. For a
            check run straight after ``apply_kueue_queues``, where Kueue may not have
            reconciled the ClusterQueue against its ResourceFlavor yet; leave at 0 for a
            queue that has been up for a while, so a real breakage fails immediately.
        required_resources: Resource names the campaign's pods request beyond cpu/memory
            (e.g. ``("nvidia.com/gpu",)``). Each must be covered by the ClusterQueue with
            a non-zero nominal quota, or the jobs would be suspended forever.
    """
    from robovast.common.errors import CampaignConfigError

    from .kube_client import api_transport_errors, load_kube_config
    load_kube_config(context=kube_context)
    deadline = time.monotonic() + settle_timeout
    while True:
        try:
            with api_transport_errors(
                    f"checking the Kueue admission path in namespace '{namespace}'"):
                return _check_kueue_admission(
                    namespace, required_resources=required_resources)
        except CampaignConfigError:
            if time.monotonic() >= deadline:
                raise
            logger.debug("Kueue admission path not ready yet; retrying until it settles")
            time.sleep(2)


def _check_kueue_admission(namespace, required_resources=()):
    """One pass of :func:`verify_kueue_admission_ready` (no retry, config already loaded)."""
    from robovast.common.errors import CampaignConfigError
    custom_api = client.CustomObjectsApi()
    remedy = ("Run 'vast cluster setup' to (re)create the Kueue queues, "
              "or point the campaign at the namespace that has them.")

    local_queue = _queue_object(custom_api, "localqueues", KUEUE_QUEUE_NAME,
                                namespace=namespace)
    if local_queue is None:
        raise CampaignConfigError(
            f"Kueue LocalQueue '{KUEUE_QUEUE_NAME}' does not exist in namespace "
            f"'{namespace}', but every job is labeled into it "
            f"(kueue.x-k8s.io/queue-name={KUEUE_QUEUE_NAME}). Kueue would suspend the "
            f"jobs and never admit them.\n{remedy}")

    # The LocalQueue names its ClusterQueue; read it from the object rather than
    # assuming CLUSTER_QUEUE_NAME, so a hand-edited queue is diagnosed truthfully.
    cq_name = (local_queue.get("spec") or {}).get("clusterQueue") or CLUSTER_QUEUE_NAME
    cluster_queue = _queue_object(custom_api, "clusterqueues", cq_name)
    if cluster_queue is None:
        # The API answers 404 both for "no such object" and for "no such resource type",
        # so say which. They look identical and read the same, but a missing CRD means
        # the ClusterQueue cannot even be created until the CRD is restored -- following
        # the object-level remedy first is a dead end, and one that is hard to see from
        # the message alone.
        raise CampaignConfigError(
            f"Kueue ClusterQueue '{cq_name}' does not exist, but LocalQueue "
            f"'{KUEUE_QUEUE_NAME}' in namespace '{namespace}' points at it. Every job "
            f"would stay suspended with 'ClusterQueue {cq_name} doesn't exist'.\n"
            + ("The 'clusterqueues' CRD itself is not registered, so Kueue's install is "
               "incomplete -- the queue cannot be created until that is repaired. "
               if not _crd_registered(custom_api, "clusterqueues") else "")
            + remedy)

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

    # Coverage, checked against the same `spec` already in hand. A resource absent from
    # every resourceGroup, or present with a nominal quota of zero, is unadmittable
    # forever -- and Kueue says so only as text on a Workload condition that nothing
    # fails on, which is how a campaign came to hang instead of erroring.
    if required_resources:
        quotas = {}
        for group in spec.get("resourceGroups") or []:
            for flavor in group.get("flavors") or []:
                for res in flavor.get("resources") or []:
                    name = res.get("name")
                    if name is not None:
                        quotas[name] = res.get("nominalQuota")
        for name in required_resources:
            if name not in quotas:
                raise CampaignConfigError(
                    f"This campaign's pods request '{name}', but Kueue ClusterQueue "
                    f"'{cq_name}' does not cover it (it covers: "
                    f"{', '.join(sorted(quotas)) or 'nothing'}). Kueue would suspend "
                    f"every job indefinitely rather than rejecting it.\n{remedy}")
            if not _parse_resource(quotas[name]):
                raise CampaignConfigError(
                    f"This campaign's pods request '{name}', but Kueue ClusterQueue "
                    f"'{cq_name}' gives it a nominal quota of {quotas[name]!r}, so no "
                    f"job can ever be admitted.\n{remedy}")

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
    from .kube_client import load_kube_config
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
    from .kube_client import load_kube_config
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


def _kueue_managed_jobs():
    """``{(namespace, job_name)}`` for every Job submitted to a Kueue queue, or ``None``
    when the cluster-wide Job list is not readable.

    ``None`` is not "no such jobs": the caller must then treat *every* Job-owned pod as
    Kueue's, which under-counts the reservation rather than over-counting it. Sizing the
    quota too small only makes jobs queue; sizing it too large is the over-admission this
    whole subtraction exists to prevent. A read-only service account without cluster-wide
    batch access is the realistic case, so it warns and continues instead of failing.
    """
    try:
        jobs = client.BatchV1Api().list_job_for_all_namespaces(
            label_selector=f"kueue.x-k8s.io/queue-name={KUEUE_QUEUE_NAME}")
    except Exception as exc:  # noqa: BLE001 - optional refinement, see docstring
        logger.warning(
            "Could not list Kueue-managed Jobs (%s); treating every Job-owned pod as "
            "Kueue's when sizing the quota.", exc)
        return None
    return {(job.metadata.namespace, job.metadata.name) for job in jobs.items}


def _unmanaged_pod_reservations(v1, node_names):
    """CPU cores and memory bytes held on *node_names* by pods Kueue does not manage.

    These are the reservations the ClusterQueue cannot see: the scheduler has already
    subtracted them from every node, but no Workload accounts for them, so quota granted
    on top of them is quota the scheduler cannot honor.

    Counts the same containers as the capacity report — native sidecars included, since
    Kubernetes adds their requests to the pod's effective total.

    Args:
        v1: A ``CoreV1Api`` with the kubeconfig already loaded.
        node_names: Names of the nodes the quota is being sized from.

    Returns:
        tuple: ``(cpu_cores: float, memory_bytes: int)``.
    """
    from .cluster_execution import _pod_job_name  # noqa: PLC0415 - avoids a cycle
    from .kube_client import pod_workload_containers  # noqa: PLC0415

    kueue_jobs = _kueue_managed_jobs()
    cpu = 0.0
    mem = 0
    pods = v1.list_pod_for_all_namespaces(
        field_selector="status.phase!=Succeeded,status.phase!=Failed")
    for pod in pods.items:
        if getattr(pod.spec, "node_name", None) not in node_names:
            continue
        job_name = _pod_job_name(pod)
        if job_name is not None:
            if kueue_jobs is None:
                continue
            if (pod.metadata.namespace, job_name) in kueue_jobs:
                continue
        for container in pod_workload_containers(pod):
            requests = (container.resources.requests
                        if container.resources else None) or {}
            cpu += _parse_resource(requests.get("cpu"))
            mem += int(_parse_resource(requests.get("memory")))
    return cpu, mem


def get_cluster_allocatable_resources(kube_context=None, cluster_config=None):
    """Return total allocatable CPU and memory for Kueue quota.

    Resolution order:

    1. If *cluster_config* is provided, delegate to
       ``cluster_config.get_cluster_allocatable_resources(kube_context)``.
       A provider-specific override (e.g. GCP) can query the autoscaler for
       the true *maximum* capacity, which is correct for autoscaling clusters.
    2. Otherwise query the Kubernetes node API: sums the **total allocatable**
       resources across all current nodes, minus what pods Kueue does *not*
       manage already reserve (see :func:`_unmanaged_pod_reservations`).

    Kueue manages the quota for the workloads it admits, and only for those. Everything
    else on the nodes — its own controller, the ingress and CNI DaemonSets, MinIO, the
    RoboVAST service — holds CPU and memory the scheduler has already subtracted and the
    ClusterQueue knows nothing about. Sizing the quota at 100% of allocatable therefore
    over-admits by exactly that much: the queue lets in one job too many, the scheduler
    has nowhere to put it, and the pod sits ``Unschedulable`` ("Insufficient cpu") until
    the batch loop gives up on it. Campaign pods are excluded from the subtraction —
    those *are* Kueue's to account for, and subtracting the ones that happen to be
    running at setup time would shrink the quota permanently to fit one moment's load.

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
        from .kube_client import load_kube_config
        load_kube_config(context=kube_context)

        v1 = client.CoreV1Api()
        total_allocatable_cpu = 0.0
        total_allocatable_mem = 0  # bytes

        nodes = v1.list_node()
        node_names = set()
        unreserved = []
        for node in nodes.items:
            alloc = node.status.allocatable or {}
            capacity = getattr(node.status, "capacity", None) or {}
            total_allocatable_cpu += _parse_resource(alloc.get("cpu"))
            total_allocatable_mem += int(_parse_resource(alloc.get("memory")))
            node_names.add(node.metadata.name)
            if (capacity.get("cpu")
                    and _parse_resource(alloc.get("cpu")) >= _parse_resource(
                        capacity.get("cpu"))):
                unreserved.append(node.metadata.name)

        if total_allocatable_cpu <= 0:
            raise RuntimeError(
                f"No allocatable CPU found across {len(nodes.items)} cluster "
                "node(s); cannot size the Kueue quota."
            )

        # allocatable == capacity means the kubelet holds nothing back: the quota below
        # can be handed out down to the last core, leaving the OS, the kubelet and the
        # container runtime to compete with pods for it. Worth saying once at setup --
        # it is invisible otherwise, and the remedy is a node-side setting no RoboVAST
        # command can reach.
        if unreserved:
            logger.warning(
                "%d node(s) reserve nothing for the system (allocatable == capacity): "
                "%s. Consider kubelet --system-reserved/--kube-reserved so the node's "
                "own processes are not scheduled against, then re-run cluster setup.",
                len(unreserved), ", ".join(sorted(unreserved)))

        reserved_cpu, reserved_mem = _unmanaged_pod_reservations(v1, node_names)

        cpu_quota = max(1, int(total_allocatable_cpu - reserved_cpu))
        memory_gi = max(1, (total_allocatable_mem - reserved_mem) // (1024**3))
        memory_quota = f"{memory_gi}Gi"

        logger.info(
            "Cluster allocatable: %.1f CPU(s), %d GiB across %d node(s); reserved by "
            "pods Kueue does not manage: %.1f CPU(s), %d GiB; quota: %d CPU(s), %s",
            total_allocatable_cpu,
            total_allocatable_mem // (1024**3),
            len(nodes.items),
            reserved_cpu,
            reserved_mem // (1024**3),
            cpu_quota,
            memory_quota,
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
    # Every campaign creates one of these to be admitted in start-time order; without the
    # CRD the priority class cannot be created and no batch can be submitted at all.
    "workloadpriorityclasses.kueue.x-k8s.io",
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
        tuple[bool, str]: ``(ok, detail)``; *detail* explains a failure so the
        caller can put it in the error it raises.
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
        return False, f"could not read the chart manifest: {manifest.stderr.strip()}"
    crd_docs = [
        doc for doc in manifest.stdout.split("\n---\n")
        if "kind: CustomResourceDefinition" in doc
    ]
    if not crd_docs:
        logger.warning("No CRDs found in Kueue chart manifest; cannot recover.")
        return False, "the chart manifest contains no CustomResourceDefinition"
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
        # Returned, not just logged: the caller raises, and without this the operator is
        # told the CRDs are missing "even after recovery" with no word on why the
        # recovery failed -- which is the one fact that says what to do next.
        return False, (applied.stderr or applied.stdout or "").strip()
    logger.info("Force-applied %d Kueue CRD(s) from chart manifest", len(crd_docs))
    return True, ""


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
        _healed, detail = _force_apply_kueue_crds(ctx_helm, ctx_kubectl)
        not_ready = _wait_for_kueue_crds(ctx_kubectl, timeout=60)
        if not_ready:
            raise RuntimeError(
                "Kueue installation incomplete: CRD(s) missing or not established "
                f"even after recovery: {not_ready}. The ClusterQueue cannot be "
                "created, so jobs would run unmanaged (no admission control)."
                + (f"\nThe recovery itself failed: {detail}" if detail else "")
                + "\nRun `vast cluster cleanup` and then `setup` again."
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


def helm_release_exists(release, namespace, ctx_helm):
    """Whether *release* is installed in *namespace*.

    Shared with the device-plugin installer so both take the same install-or-upgrade
    branch: a copy of this would be one place for the two to drift, and the whole point of
    the branch is that re-running setup must not fail on an already-installed chart.

    A helm that cannot answer reads as "not installed", which sends the caller down the
    ``install`` path -- and ``helm install`` on an existing release fails loudly instead of
    doing something surprising.
    """
    result = subprocess.run(
        ["helm", "list", "-n", namespace, "-q", "-f", release] + list(ctx_helm),
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def install_kueue_helm(kube_context=None):
    """Install Kueue via Helm in kueue-system namespace.

    Requires Helm to be installed and in PATH.
    If Kueue is already installed, upgrades to the specified version.

    Args:
        kube_context: Kubernetes context to use. None uses the active context.
    """
    adopt_orphaned_kueue_crds(kube_context=kube_context)
    ctx_helm = [f"--kube-context={kube_context}"] if kube_context else []
    ctx_kubectl = ["--context", kube_context] if kube_context else []
    if helm_release_exists(KUEUE_HELM_RELEASE, KUEUE_NAMESPACE, ctx_helm):
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


def orphaned_kueue_crds(kube_context=None):
    """Kueue CRDs present but owned by no Helm release: ``[(name, why)]``.

    Helm identifies ownership by the ``meta.helm.sh/release-name`` and
    ``release-namespace`` **annotations**. A CRD from a chart's ``crds/`` directory
    never gets them and is never deleted by ``helm uninstall`` — so after a teardown it
    lingers, carrying Helm's *labels* but none of its ownership, and the next install
    refuses with "invalid ownership metadata".
    """
    from .kube_client import load_kube_config  # pylint: disable=import-outside-toplevel

    load_kube_config(context=kube_context)
    api = client.ApiextensionsV1Api()
    orphaned = []
    for crd in api.list_custom_resource_definition().items:
        if crd.spec.group != KUEUE_WORKLOAD_GROUP:
            continue
        annotations = crd.metadata.annotations or {}
        if not annotations.get("meta.helm.sh/release-name"):
            orphaned.append((crd.metadata.name, "no Helm release owns it"))
    return orphaned


def adopt_orphaned_kueue_crds(kube_context=None):
    """Stamp Helm's ownership onto Kueue CRDs that have none, so install can proceed.

    Recovers a cluster torn down by a RoboVAST that did not remove them (see
    :func:`delete_kueue_crds`), or by a bare ``helm uninstall``. Without this, such a
    cluster is permanently un-setup-able from RoboVAST: every attempt fails with
    "invalid ownership metadata" and the remedy — deleting CRDs by hand — is nowhere in
    the error.

    Adoption rather than deletion, deliberately: deleting a CRD destroys every
    ClusterQueue and Workload defined by it, which on a cluster someone is still using
    would be a silent, unrecoverable loss for what is only a bookkeeping problem.
    """
    from kubernetes.client.rest import ApiException  # pylint: disable=import-outside-toplevel

    orphans = orphaned_kueue_crds(kube_context=kube_context)
    if not orphans:
        return

    api = client.ApiextensionsV1Api()
    patch = {"metadata": {"annotations": {
        "meta.helm.sh/release-name": KUEUE_HELM_RELEASE,
        "meta.helm.sh/release-namespace": KUEUE_NAMESPACE,
    }}}
    for name, why in orphans:
        logger.info("Adopting leftover Kueue CRD %s (%s) into the %s release",
                    name, why, KUEUE_HELM_RELEASE)
        try:
            api.patch_custom_resource_definition(name, patch)
        except ApiException as exc:
            raise RuntimeError(
                f"could not adopt the leftover CRD {name}: {exc.reason}. Helm will "
                "refuse to install over it; remove it with "
                f"'kubectl delete crd {name}' (this deletes its objects) and retry."
            ) from exc


def delete_kueue_crds(kube_context=None, timeout_s=120.0):
    """Delete Kueue's CRDs and confirm they are gone.

    ``helm uninstall`` deliberately never deletes CRDs — a chart's ``crds/`` directory
    is install-only, because deleting a CRD destroys every object of that kind. The
    consequence for a *teardown* is a cluster that cannot be set up again: the CRDs
    remain, un-owned, and the next ``helm upgrade --install`` fails with

        CustomResourceDefinition "clusterqueues.kueue.x-k8s.io" ... cannot be imported
        into the current release: missing key "meta.helm.sh/release-name"

    which reads as a RoboVAST bug and is really a leftover. So cleanup deletes them
    itself, and **verifies** — a CRD whose instances still carry finalizers stays
    ``Terminating`` forever once the controller that would clear them is gone, so a
    fire-and-forget delete would report success and leave the same trap.

    Raises:
        RuntimeError: something survived, naming what — a half-cleaned cluster the next
            setup will trip over is worth failing for.
    """
    from kubernetes.client.rest import ApiException  # pylint: disable=import-outside-toplevel

    from .kube_client import load_kube_config  # pylint: disable=import-outside-toplevel

    load_kube_config(context=kube_context)
    api = client.ApiextensionsV1Api()

    names = [crd.metadata.name
             for crd in api.list_custom_resource_definition().items
             if crd.spec.group == KUEUE_WORKLOAD_GROUP]
    if not names:
        logger.debug("No %s CRDs to remove", KUEUE_WORKLOAD_GROUP)
        return

    logger.info("Removing %d %s CRD(s): %s",
                len(names), KUEUE_WORKLOAD_GROUP, ", ".join(sorted(names)))
    for name in names:
        try:
            api.delete_custom_resource_definition(name)
        except ApiException as exc:
            if exc.status != 404:
                logger.warning("Could not delete CRD %s: %s", name, exc)

    deadline = time.monotonic() + timeout_s
    remaining = names
    cleared_finalizers = False
    while time.monotonic() < deadline:
        remaining = [crd.metadata.name
                     for crd in api.list_custom_resource_definition().items
                     if crd.spec.group == KUEUE_WORKLOAD_GROUP]
        if not remaining:
            logger.info("All %s CRDs removed", KUEUE_WORKLOAD_GROUP)
            return
        # One pass only: if they are still here after finalizers were cleared, waiting
        # longer will not help and the error below should say so.
        if not cleared_finalizers:
            _clear_finalizers_on_kueue_objects(remaining)
            cleared_finalizers = True
        time.sleep(2)

    raise RuntimeError(
        f"these {KUEUE_WORKLOAD_GROUP} CRDs would not delete: {', '.join(remaining)}. "
        "The next 'vast cluster setup' will fail on them ('invalid ownership "
        "metadata'). Remove them with "
        f"'kubectl delete crd {' '.join(remaining)}' and check for instances stuck "
        "with finalizers.")


def _clear_finalizers_on_kueue_objects(crd_names):
    """Strip finalizers from the instances holding a Terminating CRD open.

    Kueue's own controller normally removes these, but by teardown it is gone — so
    nothing does, and the CRD waits forever on objects that will never be released.
    """
    custom_api = client.CustomObjectsApi()
    patch = {"metadata": {"finalizers": None}}
    for crd_name in crd_names:
        plural = crd_name.split(".", 1)[0]
        try:
            listed = custom_api.list_cluster_custom_object(
                group=KUEUE_WORKLOAD_GROUP, version=KUEUE_WORKLOAD_VERSION,
                plural=plural)
        except Exception:  # noqa: BLE001 - best effort; the CRD may already be gone
            continue
        for item in listed.get("items", []):
            meta = item.get("metadata", {})
            if not meta.get("finalizers"):
                continue
            name, namespace = meta.get("name"), meta.get("namespace")
            try:
                if namespace:
                    custom_api.patch_namespaced_custom_object(
                        KUEUE_WORKLOAD_GROUP, KUEUE_WORKLOAD_VERSION, namespace,
                        plural, name, patch)
                else:
                    custom_api.patch_cluster_custom_object(
                        KUEUE_WORKLOAD_GROUP, KUEUE_WORKLOAD_VERSION, plural, name,
                        patch)
                logger.debug("Cleared finalizers on %s/%s", plural, name)
            except Exception as exc:  # noqa: BLE001 - best effort
                logger.debug("Could not clear finalizers on %s/%s: %s",
                             plural, name, exc)


def uninstall_kueue_helm(kube_context=None):
    """Uninstall Kueue and remove what Helm leaves behind.

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
    # Helm will not do this, by design (see delete_kueue_crds), and leaving them is what
    # makes the *next* setup fail — so a cleanup that skipped it was not a cleanup.
    delete_kueue_crds(kube_context=kube_context)


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
    # Read from the live nodes, exactly as cpu/memory are -- which is why no caller has to
    # pass it and why a re-run, an `upgrade` or a `--no-gpu` run all still get a truthful
    # quota with nothing persisted anywhere. Deliberately no `max(1, ...)`: copying the
    # cpu/memory floor would give a cluster whose plugin is down a quota of one GPU, which
    # Kueue admits against and no node can satisfy.
    from .kubernetes_gpu import get_cluster_allocatable_gpus
    try:
        gpu_quota = get_cluster_allocatable_gpus(kube_context=kube_context)
    except Exception as exc:  # noqa: BLE001 - a GPU-less cluster must not fail here
        logger.debug("Could not read GPU capacity (%s); sizing the queue without it", exc)
        gpu_quota = 0
    yaml_content = yaml.safe_dump_all(
        _queue_manifests(
            namespace=namespace,
            queue_name=KUEUE_QUEUE_NAME,
            cluster_queue=CLUSTER_QUEUE_NAME,
            cpu_quota=cpu_quota,
            memory_quota=memory_quota,
            node_labels=node_labels,
            gpu_quota=gpu_quota,
        ),
        default_flow_style=False,
        sort_keys=False,
    ).strip()

    ctx_kubectl = ["--context", kube_context] if kube_context else []
    ctx_helm = [f"--kube-context={kube_context}"] if kube_context else []
    # Self-heal the CRDs before applying the queues, not only during the Helm install.
    # A CRD can go missing *after* a successful setup -- Helm never re-creates a chart's
    # ``crds/`` on upgrade, so a later ``setup`` cannot restore it either, and the only
    # symptom is every future job sitting suspended behind a ClusterQueue that "does not
    # exist". Healing here makes re-running setup the repair its own error message tells
    # you to run, instead of a command that fails the same way on the missing CRD.
    _ensure_kueue_crds(ctx_helm, ctx_kubectl, timeout=60)

    # Retry to handle the race where a CRD from a previous uninstall is still
    # in Terminating state when we try to create resources.  Each attempt
    # re-waits for the CRDs to be fully established before applying.
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
