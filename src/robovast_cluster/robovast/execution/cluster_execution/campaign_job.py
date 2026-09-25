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

"""The shape of an admitted campaign Job.

A campaign Job is a one-shot pod placed by the admission queue on a campaign node,
labelled with its group and campaign so that selectors count, watch and clean it up. Its
builder hands the pod here, so a label, a toleration or a pin is decided in one place.
"""

from .cluster_execution import _label_safe_campaign
from .node_placement import CAMPAIGN_NODE_TOLERATIONS, job_node_pool, job_node_selector


def campaign_job_manifest(*, name: str, namespace: str, jobgroup: str, campaign_id: str,
                          pod_spec: dict, ttl_seconds: int, pull_secret: str = "",
                          labels=None, annotations=None, pod_name: str = "",
                          pod_annotations=None) -> dict:
    """A campaign Job around *pod_spec*.

    ``backoffLimit: 0`` and ``restartPolicy: Never``: a campaign Job is one attempt, and a
    retry would be a second trial that nothing asked for. The pod
    tolerates the campaign nodes' taint (:func:`apply_campaign_pod_policy`) and carries
    *pull_secret* when one is configured. *ttl_seconds* is how long the finished Job stays
    readable for whoever waits on it.

    *labels* are added to the group and campaign labels on both the Job and its pod, so a
    selector on either finds the same set. *campaign_id* is made label-safe here.
    """
    all_labels = {"jobgroup": jobgroup, "campaign-id": _label_safe_campaign(campaign_id),
                  **(labels or {})}
    pod_metadata = {"labels": dict(all_labels)}
    if pod_name:
        pod_metadata["name"] = pod_name
    if pod_annotations:
        pod_metadata["annotations"] = dict(pod_annotations)
    metadata = {"name": name, "namespace": namespace, "labels": all_labels}
    if annotations:
        metadata["annotations"] = dict(annotations)
    spec = {"restartPolicy": "Never", **pod_spec}
    apply_campaign_pod_policy(spec, pull_secret)
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": metadata,
        "spec": {
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": ttl_seconds,
            "template": {"metadata": pod_metadata, "spec": spec},
        },
    }


def apply_campaign_pod_policy(pod_spec: dict, pull_secret: str = "") -> dict:
    """Let *pod_spec* onto campaign nodes and pull its own images. Returns it.

    The toleration goes on the pod itself: nothing else injects it, and a deployment that
    taints its campaign nodes without it does not fail loudly -- its pods simply never
    place. Additive and idempotent, so it is safe on a spec that already carries it.

    *pull_secret* is the Secret the pod's own images need
    (:func:`~.cluster_execution.resolve_pull_secret`); ``""`` leaves the spec without one.
    """
    tolerations = list(pod_spec.get("tolerations") or [])
    for toleration in CAMPAIGN_NODE_TOLERATIONS:
        if dict(toleration) not in tolerations:
            tolerations.append(dict(toleration))
    pod_spec["tolerations"] = tolerations
    if pull_secret:
        pod_spec["imagePullSecrets"] = [{"name": pull_secret}]
    return pod_spec


def pin_campaign_job(manifest: dict, node_id=None) -> dict:
    """Confine the Job's pod to the operator's node pool, then to *node_id*. Returns it.

    The node is the one admission granted room on, so the placement and the reservation
    are one decision; ``None`` leaves the pod anywhere in the pool. The pool is the
    operator's ``ROBOVAST_JOB_NODE_LABELS``, and it must reach the pod rather than only the
    accounting: the budget provider counts only nodes inside it, so a pod free to land
    outside would run on capacity nothing reserved. The pin narrows the pool rather than
    replacing it (:func:`~.node_placement.job_node_selector`).
    """
    pool = job_node_pool()
    if not pool and not node_id:
        # Nothing to confine; a minimal manifest (an offline emit, a test) keeps its shape.
        return manifest
    spec = manifest.setdefault("spec", {}).setdefault("template", {}).setdefault("spec", {})
    spec["nodeSelector"] = job_node_selector(spec.get("nodeSelector"), node_id, pool)
    return manifest
