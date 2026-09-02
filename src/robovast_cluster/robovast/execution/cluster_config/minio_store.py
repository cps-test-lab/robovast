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

"""The volume under an embedded MinIO, and the refusals that guard changing it.

A provider that embeds an object store declares the pod; this decides what ``/data`` is
mounted from, and says no when the live pod cannot be made to match. Shared because the
answer must be the same wherever a store is embedded, and because a refusal copied per
provider is a refusal that goes stale in one of them.

**The store holds finished campaigns.** ``finalize_campaign`` publishes a campaign's whole
directory into it, and every download, re-postprocessing and index ingest reads from there,
so its volume decides whether a campaign survives its pod. A ``hostPath`` on the node this
deployment is already pinned to is the default because a stock cluster provisions nothing;
a StorageClass is better where there is one.
"""

import logging

from kubernetes import client

logger = logging.getLogger(__name__)

#: The pod every embedded-store provider deploys, the container inside it, and the volume its
#: ``/data`` mounts. One spelling, because the manifest that creates them, the reader that
#: measures them and the refusal that inspects them must agree.
MINIO_POD_NAME = "robovast"
MINIO_CONTAINER_NAME = "minio"
MINIO_VOLUME_NAME = "minio-storage"
MINIO_DATA_DIR = "/data"

#: Size of the store's claim when a StorageClass backs it. Only meaningful then: a hostPath
#: store is bounded by the node's disk, which no manifest can declare.
DEFAULT_STORE_STORAGE_SIZE = "500Gi"


def store_volume(storage_path, storage_class=""):
    """The volume backing ``/data``: a claim where one can be provisioned, else a hostPath.

    ``emptyDir`` is not offered. It was the default while the store was believed to be a
    buffer that results passed through; it is not one, and a volume that a restart empties
    is the wrong home for the only complete copy of a finished campaign.

    ``DirectoryOrCreate`` rather than ``Directory`` so a first setup needs nothing prepared
    on the node -- the same choice the registry and workspaces volumes make.
    """
    if storage_class:
        return {"name": MINIO_VOLUME_NAME,
                "persistentVolumeClaim": {"claimName": MINIO_VOLUME_NAME}}
    return {"name": MINIO_VOLUME_NAME,
            "hostPath": {"path": storage_path, "type": "DirectoryOrCreate"}}


def store_pvc_manifest(namespace, storage_class, size=""):
    """The claim :func:`store_volume` names, or ``None`` where a hostPath backs the store."""
    if not storage_class:
        return None
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": MINIO_VOLUME_NAME, "namespace": namespace,
                     "labels": {"app": "robovast-service"}},
        "spec": {"accessModes": ["ReadWriteOnce"],
                 "storageClassName": storage_class,
                 "resources": {"requests": {"storage": size or DEFAULT_STORE_STORAGE_SIZE}}},
    }


def apply_store_volume(docs, volume, claim=None):
    """Put *volume* under the store pod's ``/data``, and put *claim* ahead of the pod.

    The provider owns its manifest; this replaces only the one volume the store container
    mounts, found through that container's own mount rather than by name, so a provider that
    spells its volume differently is still corrected rather than quietly left alone.

    Claims come first because ``apply_manifests`` creates in order, and a pod scheduled
    against a claim that does not exist yet stays Pending.
    """
    docs = [d for d in docs if d is not None]
    for doc in docs:
        if doc.get("kind") != "Pod":
            continue
        spec = doc.get("spec", {})
        container = next((c for c in spec.get("containers", [])
                          if c.get("name") == MINIO_CONTAINER_NAME), None)
        if container is None:
            continue
        mount = next((m for m in container.get("volumeMounts", [])
                      if m.get("mountPath") == MINIO_DATA_DIR), None)
        if mount is None:
            continue
        placed = dict(volume, name=mount["name"])
        spec["volumes"] = [placed if v.get("name") == mount["name"] else v
                           for v in spec.get("volumes", [])]
    return ([claim] if claim else []) + docs


def live_store_backing(namespace, pod_name=MINIO_POD_NAME):
    """What the running store pod mounts at ``/data``, as ``(kind, detail)``.

    ``(None, None)`` when there is no such pod -- nothing is live, so nothing constrains what
    setup may create.
    """
    try:
        pod = client.CoreV1Api().read_namespaced_pod(pod_name, namespace)
    except client.exceptions.ApiException as e:
        if e.status == 404:
            return None, None
        raise
    container = next((c for c in (pod.spec.containers or [])
                      if c.name == MINIO_CONTAINER_NAME), None)
    if container is None:
        return None, None
    mount = next((m for m in (container.volume_mounts or [])
                  if m.mount_path == MINIO_DATA_DIR), None)
    if mount is None:
        return None, None
    volume = next((v for v in (pod.spec.volumes or []) if v.name == mount.name), None)
    if volume is None:
        return None, None
    if volume.host_path is not None:
        return "hostPath", volume.host_path.path
    if volume.persistent_volume_claim is not None:
        return "claim", volume.persistent_volume_claim.claim_name
    if volume.empty_dir is not None:
        return "emptyDir", None
    return None, None


def refuse_a_store_the_manifest_cannot_change(namespace, wanted, pod_name=MINIO_POD_NAME):
    """Raise when the live store's volume differs from *wanted* and cannot be re-applied.

    ``apply_manifests`` tolerates a 409 and keeps the running pod -- deliberately, since
    recreating it on every setup would be a far worse default. The cost is that a changed
    volume is reported and never applied, so setup would print "completed successfully" over
    a store still backed by whatever it was backed by before.

    **What the remedy costs depends on what is live, so the message says which.** A blanket
    "your campaigns will be discarded" is false wherever the store already outlives its pod,
    and a warning that is wrong two times in three is one an operator stops reading.
    """
    kind, detail = live_store_backing(namespace, pod_name)
    if kind is None:
        return
    wanted_kind = "claim" if "persistentVolumeClaim" in wanted else "hostPath"
    wanted_detail = (wanted.get("persistentVolumeClaim", {}).get("claimName")
                     if wanted_kind == "claim" else wanted.get("hostPath", {}).get("path"))
    if kind == wanted_kind and detail == wanted_detail:
        return

    recreate = (f"Delete it (`kubectl delete pod {pod_name} -n {namespace}`) or run "
                f"`vast cluster cleanup`, then run setup again.")
    if kind == "emptyDir":
        cost = (
            f"Its campaign store is a volume inside the pod, so recreating it DISCARDS every "
            f"campaign the store holds. Archive what matters first -- `vast share <campaign>` "
            f"or `vast campaign download <campaign>` -- because nothing else holds a complete "
            f"copy. {recreate}")
    elif kind == "hostPath":
        cost = (
            f"Its campaigns are in {detail} on the node and are NOT moved: the recreated pod "
            f"starts empty at {wanted_detail}, and the old directory keeps its bytes with "
            f"nothing left naming it. Move that directory on the node first, or re-run with "
            f"`--store-path {detail}` to leave the store where it is. {recreate}")
    else:
        cost = (
            f"Its campaigns are in the {detail} volume and are NOT copied to the new backing; "
            f"the recreated pod starts empty. {recreate}")
    raise RuntimeError(
        f"the results store pod in namespace {namespace} is running with a {kind} store and "
        f"cannot be converted by re-applying its manifest -- an existing pod is kept as it "
        f"is, so the new backing would be reported and never take effect. {cost}")


def refuse_a_store_placement(config_name, storage_path, storage_class, backend="s3"):
    """Raise when a store placement is given to a provider that cannot apply it.

    Two providers cannot: one whose campaigns live in a bucket, which no node holds, and one
    that builds the store's volume itself. Both would otherwise accept the setting, record
    it, and place nothing -- and a setting that is read back later reads as though it had
    applied.
    """
    if not storage_path and not storage_class:
        return
    named = "--store-path" if storage_path else "--store-class"
    where = ("keeps campaigns in a bucket, which no node holds"
             if backend != "s3" else "provisions the store's volume itself")
    raise RuntimeError(
        f"{named} places this deployment's object store, but the '{config_name}' provider "
        f"{where}. Drop it; --data-root is still worth passing, because the registry, the "
        f"index, the workspaces and the build cache are placed on nodes here as everywhere.")


#: What the kubelet cannot measure, said once. A ``hostPath`` volume carries no per-volume
#: stats -- the kubelet reports only the volumes it manages -- so a store on one has no meter
#: of its own. Not a fault, and not an empty store: the disk it sits on is the one the **Disk**
#: meter already reports, because the store pod and the service pod share a node.
HOSTPATH_NOT_MEASURED = ("the store is a directory on its node, which the kubelet does not "
                         "measure per-volume; the Disk meter reports the same filesystem")


def store_usage(node_summaries, pod_name=MINIO_POD_NAME, volume_name=MINIO_VOLUME_NAME):
    """The store volume's ``(used, ceiling, reason)`` from the kubelet's per-pod stats.

    **The denominator is ``used + available``, not ``capacityBytes``.** A volume that declares
    no size limit reports the whole node filesystem as its capacity -- a filesystem it shares
    with the images, the containers and every campaign directory -- so ``capacityBytes`` reads
    as headroom that is not there. ``availableBytes`` is what the filesystem will actually
    still take, so ``used + available`` is the ceiling this store can really reach. That the
    denominator moves as the rest of the node fills is the feature: what an operator needs
    before a sweep is how much more the store can hold *now*.

    Three outcomes, and the third is why this returns a reason at all:

    * measured -- ``(used, ceiling, "")``.
    * **not here** -- ``(None, None, "")``. The pod was in none of the summaries read, so it
      may sit on a node whose kubelet was not asked. A store the caller cannot see is not a
      store of size zero, and the caller should keep looking.
    * **here but unmeasurable** -- ``(None, None, reason)``. The pod is in the stats and its
      volume is not, which is what a ``hostPath`` looks like from here. Saying so is what
      stops the caller walking every remaining node for an answer that will not come, and
      what lets a reader tell "no meter" from "not looked at yet".
    """
    found_pod = False
    for summary in (node_summaries or {}).values():
        for pod in (summary.get("pods") or []):
            if (pod.get("podRef") or {}).get("name") != pod_name:
                continue
            found_pod = True
            for volume in (pod.get("volume") or []):
                if volume.get("name") != volume_name:
                    continue
                used = volume.get("usedBytes")
                available = volume.get("availableBytes")
                if used is None or available is None:
                    # No meter beats falling back to the capacity that caused this.
                    return None, None, ""
                return int(used), int(used) + int(available), ""
    return (None, None, HOSTPATH_NOT_MEASURED if found_pod else "")
