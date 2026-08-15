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

"""The container registry RoboVAST runs for itself, beside the service.

Experiment images (a project's ``build:`` section) have to be pushed somewhere the
cluster can pull them back from. Requiring an external registry made that a site
prerequisite -- and a site without one could not build at all. This runs one in the
service pod instead, so a build target always exists.

**Why it rides the service's own Ingress rather than a Service DNS name.** An image ref
is a single string used twice: BuildKit pushes to it from inside a pod (pod network,
CoreDNS), and the kubelet pulls from it on the node (node network, node resolver, node
TLS trust). Nothing in a pod spec reaches the second one -- ``hostAliases`` explicitly
does not, see ``BaseConfig.get_host_aliases`` -- so a ``.svc`` name resolves for the push
and is unresolvable for the pull, and a plain-HTTP NodePort needs
``/etc/rancher/rke2/registries.yaml`` plus a runtime restart on every node, which is
outside the Kubernetes API entirely.

Publishing ``/v2`` on the host the service already answers on sidesteps all of it: that
name is real DNS with a real certificate, so the node trusts and resolves it with no
configuration, and so does a developer's laptop -- ``docker pull`` from a workstation
works against the same URL, which is how someone reproduces a campaign's exact image
locally.

The registry is deliberately **unauthenticated**. It shares a hostname with the UI, which
*is* token-gated, so it is the more reachable half of that host; acceptable while the
service is not exposed to the internet, and the first thing to revisit when it is. Adding
auth is an htpasswd Secret plus an Ingress annotation -- see ``docs/cluster_execution.rst``.
"""

import logging

logger = logging.getLogger(__name__)

#: The registry listens here inside the pod. Not 5000-on-the-host: nothing publishes this
#: port directly, it is reached through the Service and the Ingress' ``/v2`` rule.
REGISTRY_PORT = 5000

#: Container name inside the robovast-service pod.
REGISTRY_CONTAINER_NAME = "registry"

#: Upstream registry. Pinned to a major tag rather than a digest because it is
#: infrastructure the campaign never runs *in* -- nothing about a result depends on which
#: patch release stored the bytes.
REGISTRY_IMAGE = "registry:2"

#: Where the registry keeps its blobs inside the container.
REGISTRY_DATA_DIR = "/var/lib/registry"

#: Name of the volume carrying :data:`REGISTRY_DATA_DIR`.
REGISTRY_VOLUME_NAME = "registry-data"

#: Default host path backing the registry when no StorageClass is available.
DEFAULT_REGISTRY_HOST_PATH = "/var/lib/robovast-registry"

#: The Ingress path that makes this a registry. The Docker registry API lives at ``/v2/``
#: by protocol, so routing that prefix to this container is what turns the service's
#: hostname into a usable registry host -- and why the ref is ``<host>/<name>:<tag>`` with
#: no path component.
REGISTRY_INGRESS_PATH = "/v2"


def registry_prefix(ingress_host):
    """The image-ref prefix campaigns build into, or ``""`` without an Ingress.

    Just the host: an image pushed here is ``<host>/<tag>:<hash>``, because a registry
    lives at the root of its host's ``/v2`` namespace.

    Empty when the service is not published. That is not a degraded registry, it is no
    reachable registry at all -- a ref the node cannot pull is worse than an honest
    refusal, so callers must treat the empty string as "builds unavailable".
    """
    return (ingress_host or "").strip()


def registry_container(storage_path=DEFAULT_REGISTRY_HOST_PATH):
    """The registry container to run alongside the service in its pod.

    *storage_path* is unused here (the volume carries it) and accepted so callers read
    as a pair with :func:`registry_volume`.
    """
    del storage_path
    return {
        "name": REGISTRY_CONTAINER_NAME,
        "image": REGISTRY_IMAGE,
        "imagePullPolicy": "IfNotPresent",
        "ports": [{"containerPort": REGISTRY_PORT, "name": "registry"}],
        "env": [
            # Let a re-pushed tag replace its predecessor and let the garbage collector
            # reclaim it. Without this the registry refuses deletes outright, and an
            # experiment image rebuilt a hundred times keeps a hundred copies.
            {"name": "REGISTRY_STORAGE_DELETE_ENABLED", "value": "true"},
            {"name": "REGISTRY_STORAGE_FILESYSTEM_ROOTDIRECTORY",
             "value": REGISTRY_DATA_DIR},
        ],
        "volumeMounts": [{"name": REGISTRY_VOLUME_NAME,
                          "mountPath": REGISTRY_DATA_DIR}],
        "readinessProbe": {
            "httpGet": {"path": "/v2/", "port": REGISTRY_PORT},
            "initialDelaySeconds": 2, "periodSeconds": 10},
        "livenessProbe": {
            "httpGet": {"path": "/v2/", "port": REGISTRY_PORT},
            "initialDelaySeconds": 10, "periodSeconds": 20},
    }


def registry_volume(storage_path=DEFAULT_REGISTRY_HOST_PATH, storage_class=""):
    """The volume backing the registry: a PVC when one can be provisioned, else hostPath.

    ``emptyDir`` is not offered. Every upgrade now restarts the pod (see
    ``service_deploy.RESTART_ANNOTATION``), which with an emptyDir would discard every
    built image on each version bump -- and campaign Jobs already submitted against those
    refs would go straight to ImagePullBackOff rather than fail honestly.

    hostPath is the default because a stock RKE2 cluster ships no StorageClass at all, so
    a PVC there stays Pending forever. It pins the registry's data to one node, which is
    why :func:`registry_node_selector` exists.
    """
    if storage_class:
        return {"name": REGISTRY_VOLUME_NAME,
                "persistentVolumeClaim": {"claimName": REGISTRY_VOLUME_NAME}}
    return {"name": REGISTRY_VOLUME_NAME,
            "hostPath": {"path": storage_path or DEFAULT_REGISTRY_HOST_PATH,
                         "type": "DirectoryOrCreate"}}


def registry_pvc_manifest(namespace, storage_class, size="50Gi"):
    """The PVC for :func:`registry_volume`, or ``None`` when backed by hostPath."""
    if not storage_class:
        return None
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": REGISTRY_VOLUME_NAME, "namespace": namespace,
                     "labels": {"app": "robovast-service"}},
        "spec": {"accessModes": ["ReadWriteOnce"],
                 "storageClassName": storage_class,
                 "resources": {"requests": {"storage": size}}},
    }


def registry_node_selector(node_name):
    """Pin the pod to *node_name*, or ``None``.

    Only meaningful with hostPath storage: the blobs live on one node's disk, so a pod
    rescheduled elsewhere would come up with an empty registry and every previously built
    image would silently vanish. On a single-node cluster this is a formality; on a
    multi-node one it is the difference between a registry and a cache that occasionally
    forgets everything.
    """
    if not node_name:
        return None
    return {"kubernetes.io/hostname": node_name}


def registry_ingress_path():
    """The ``/v2`` rule routing the registry half of the service's hostname.

    Ordered before the catch-all ``/`` rule by the caller. The service itself registers
    no ``/v2`` route, and nginx picks the path before either backend sees the request, so
    the UI's root mount is unaffected.
    """
    return {
        "path": REGISTRY_INGRESS_PATH,
        "pathType": "Prefix",
        "backend": {"service": {"name": "robovast-service",
                                "port": {"number": REGISTRY_PORT}}},
    }
