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

"""Reclaiming the node directories a cleanup deliberately leaves behind.

``vast cluster cleanup`` removes the deployment and keeps its data: the store holds finished
campaigns, and a teardown that silently deleted them would be a very expensive way to free a
node. So the directories stay, and this is how an operator asks for them back --
``--delete-data``, never by default.

**A node directory can only be removed from the node.** No API call reaches it, so this runs
one Job pinned where the data is, mounts each directory, and empties it. That is the same
shape :mod:`.node_governor` already uses to touch a host, minus the privileges: emptying a
directory needs a mount, not a privileged container.
"""

import logging
import time

from kubernetes import client

logger = logging.getLogger(__name__)

JOB_NAME = "robovast-purge"

#: Busybox, for the same reason the governor uses it: the work is one shell loop, and pulling
#: anything larger onto a node that is being torn down is time spent for nothing.
IMAGE = "busybox:1.36"

#: How long to wait for the Job to finish before reporting what is unknown rather than done.
PURGE_TIMEOUT_SECONDS = 300


def refuse_an_unsafe_path(path: str) -> None:
    """Raise unless *path* is specific enough to be safe to empty.

    Every path handed here is read from a live object rather than typed, so this guards
    against a *bug* rather than against an operator -- and the failure it guards against
    empties a node's root filesystem, which no message afterwards can undo. A path must be
    absolute and at least two components deep, so ``/``, ``/var`` and ``""`` are all refused
    before a container ever sees them.
    """
    parts = [p for p in (path or "").split("/") if p]
    if not path.startswith("/") or len(parts) < 2 or ".." in parts:
        raise ValueError(
            f"refusing to empty {path!r}: a path this shallow, relative or containing '..' "
            f"is a bug in what read it, not a directory anybody meant to clear")


def purge_manifest(namespace, paths, node_selector=None):
    """A Job that empties each of *paths* on the node *node_selector* selects.

    The **contents** go, not the directories: a mount point that vanished would leave the
    next setup creating a plain directory over a disk nobody noticed had gone.
    """
    for path in paths:
        refuse_an_unsafe_path(path)
    mounts, volumes, targets = [], [], []
    for i, path in enumerate(paths):
        name = f"target-{i}"
        mounts.append({"name": name, "mountPath": f"/purge/{i}"})
        volumes.append({"name": name,
                        "hostPath": {"path": path, "type": "DirectoryOrCreate"}})
        targets.append(f"/purge/{i}")
    script = " ; ".join(
        [f'echo "emptying {p}" && rm -rf {t}/..?* {t}/.[!.]* {t}/* 2>/dev/null || true'
         for p, t in zip(paths, targets)] + ["echo purge-complete"])
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": JOB_NAME, "namespace": namespace,
                     "labels": {"app": "robovast-service"}},
        "spec": {
            # One attempt. A retry would re-run a deletion that already happened, and a
            # failure here is for a person to read rather than for the cluster to paper over.
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": 300,
            "template": {
                "metadata": {"labels": {"app": "robovast-service"}},
                "spec": {
                    "restartPolicy": "Never",
                    # Whatever taints the data node carries, this has to land on it: the
                    # bytes are there and nowhere else.
                    "tolerations": [{"operator": "Exists"}],
                    **({"nodeSelector": dict(node_selector)} if node_selector else {}),
                    "containers": [{
                        "name": "purge",
                        "image": IMAGE,
                        "command": ["sh", "-c", script],
                        "volumeMounts": mounts,
                    }],
                    "volumes": volumes,
                },
            },
        },
    }


def purge_node_paths(namespace, paths, node_selector=None, kube_context=None):
    """Empty *paths* on the data node, and say what happened. Returns the paths acted on.

    Reports rather than raises when the Job cannot be watched to completion: cleanup is a
    teardown, and a caller that already deleted the deployment should still finish and say
    what it is unsure about.
    """
    from .kube_client import load_kube_config  # pylint: disable=import-outside-toplevel

    paths = [p for p in dict.fromkeys(paths) if p]
    if not paths:
        return []
    load_kube_config(context=kube_context)
    batch = client.BatchV1Api()
    manifest = purge_manifest(namespace, paths, node_selector)
    try:
        batch.delete_namespaced_job(JOB_NAME, namespace, propagation_policy="Background")
        time.sleep(2)   # the old pod must go before its replacement claims the same mounts
    except client.exceptions.ApiException as e:
        if e.status != 404:
            raise
    batch.create_namespaced_job(namespace, manifest)
    logger.info("emptying %d director%s on the data node: %s",
                len(paths), "y" if len(paths) == 1 else "ies", ", ".join(paths))

    deadline = time.monotonic() + PURGE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        job = batch.read_namespaced_job_status(JOB_NAME, namespace)
        if (job.status.succeeded or 0) >= 1:
            logger.info("removed the contents of %s", ", ".join(paths))
            return paths
        if (job.status.failed or 0) >= 1:
            raise RuntimeError(
                f"the purge Job failed; the directories {', '.join(paths)} may be partly "
                f"emptied. `kubectl logs job/{JOB_NAME} -n {namespace}` says why, and "
                f"re-running cleanup with --delete-data is safe.")
        time.sleep(2)
    raise RuntimeError(
        f"the purge Job did not finish within {PURGE_TIMEOUT_SECONDS}s. It may still be "
        f"running: `kubectl get job/{JOB_NAME} -n {namespace}`. Nothing else was skipped.")
