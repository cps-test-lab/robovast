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

"""The container registry RoboVAST runs for itself, in the object-store pod.

Experiment images (a project's ``build:`` section) have to be pushed somewhere the
cluster can pull them back from. Requiring an external registry made that a site
prerequisite -- and a site without one could not build at all. This runs one in the
``robovast`` pod ``vast cluster setup`` creates (:mod:`.store_pod`), so a build target
always exists.

**Why not the service pod, where it started.** ``robovast-service`` is a Deployment rolled
by every ``vast service upgrade``, so the registry restarted on each version bump and its
blob volume followed the Deployment rather than the cluster. The blobs are what
already-submitted campaigns are pulled from: they are cluster-lifetime state, created at
setup and discarded only by ``vast cluster cleanup``.

Moving it changes **no image ref**. The prefix is still the service's published Ingress
host (see below); what moved is the Ingress' ``/v2`` backend, from the service's Service
to the store pod's. Push and pull both go on resolving the same public name.

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

The registry **authenticates wherever it is published**, and does so in the registry
rather than at the Ingress. An Ingress annotation would have been smaller, but the only
ones that exist are ingress-nginx's, and a cluster whose controller is anything else --
GKE's ``gce``, for one -- would accept them, ignore them, and serve an open registry while
reporting success. Auth the registry enforces itself holds on every controller and on the
in-cluster route as well.

It stays open on a deployment with no Ingress, because there is then no route to it: the
prefix campaigns build into is derived from the Ingress host, so an unpublished deployment
cannot build at all. Publishing is exactly the moment it becomes reachable, so publishing
is what turns auth on -- the same rule that already refuses an Ingress without an access
token.
"""

import logging

from . import data_paths

logger = logging.getLogger(__name__)

#: The registry listens here inside the pod. Not 5000-on-the-host: nothing publishes this
#: port directly, it is reached through the store pod's Service and the Ingress' ``/v2``
#: rule.
REGISTRY_PORT = 5000

#: Container name inside the store pod.
REGISTRY_CONTAINER_NAME = "registry"

#: Upstream registry. Pinned to a major tag rather than a digest because it is
#: infrastructure the campaign never runs *in* -- nothing about a result depends on which
#: patch release stored the bytes.
REGISTRY_IMAGE = "registry:2"

#: Where the registry keeps its blobs inside the container.
REGISTRY_DATA_DIR = "/var/lib/registry"

#: Name of the volume carrying :data:`REGISTRY_DATA_DIR`.
REGISTRY_VOLUME_NAME = "registry-data"

#: The registry's own debug listener, which serves ``/debug/health`` **unauthenticated**.
#:
#: Load-bearing once auth is on: the probes used to read ``/v2/``, which then answers 401,
#: and an ``httpGet`` probe counts anything outside 200-399 as a failure. The container
#: would never become Ready, the store pod would never come up, and nothing about the
#: message would point at authentication. Not published by any Service -- it is reachable
#: only from the kubelet on the pod's own address.
REGISTRY_DEBUG_PORT = 5001

#: The one account the built-in registry knows. A name rather than a per-user identity on
#: purpose: this authenticates *the deployment's own* push and pull, which is one actor --
#: BuildKit pushing, the nodes pulling, and the service asking whether a ref already
#: exists. Who may reach the UI is a separate question with a separate answer.
REGISTRY_AUTH_USER = "robovast"

#: Secret holding the bcrypt htpasswd file the registry reads, and the directory it is
#: mounted at. Separate from the ``dockerconfigjson`` Secret carrying the same credential
#: for the clients: one is the server's password file, the other is what a docker client
#: presents, and they are different formats of one fact minted in one place.
REGISTRY_HTPASSWD_SECRET_NAME = "robovast-registry-htpasswd"
REGISTRY_HTPASSWD_KEY = "htpasswd"
REGISTRY_AUTH_VOLUME_NAME = "registry-auth"
REGISTRY_AUTH_DIR = "/etc/robovast-registry-auth"
REGISTRY_AUTH_REALM = "robovast"


def htpasswd_entry(password: str, user: str = REGISTRY_AUTH_USER) -> str:
    """One bcrypt ``user:hash`` line, which is the only format the registry accepts.

    ``registry:2`` verifies with Go's bcrypt and rejects an htpasswd file written with
    MD5 or SHA1 -- it does not fall back, it fails every request, so there is no weaker
    hash to accidentally choose here.
    """
    import bcrypt  # noqa: PLC0415

    digest = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    return f"{user}:{digest}"


def htpasswd_secret_manifest(namespace: str, password: str,
                             user: str = REGISTRY_AUTH_USER) -> dict:
    """The Secret :func:`registry_container` mounts as its password file."""
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": REGISTRY_HTPASSWD_SECRET_NAME, "namespace": namespace,
                     "labels": {"app": "robovast-service"}},
        "stringData": {REGISTRY_HTPASSWD_KEY: htpasswd_entry(password, user)},
    }


def registry_auth_volume() -> dict:
    """The volume carrying the htpasswd file into the registry container."""
    return {"name": REGISTRY_AUTH_VOLUME_NAME,
            "secret": {"secretName": REGISTRY_HTPASSWD_SECRET_NAME}}

#: Default host path backing the registry when no StorageClass is available. Declared in
#: :mod:`.data_paths` with every other tenant's, so the resolver that places them all and the
#: volume that reads one cannot disagree about it.
DEFAULT_REGISTRY_HOST_PATH = data_paths.DEFAULT_REGISTRY_HOST_PATH

#: What the scheduler reserves for the registry, and what it may grow to.
#:
#: The request is deliberately tiny: ``registry:2`` streams uploads and downloads to and
#: from disk rather than buffering them, and an idle one sits in the low tens of MiB. Every
#: milli-core requested here is one campaign jobs cannot be admitted against, so this is a
#: floor rather than a comfortable estimate.
#:
#: The memory limit has headroom for concurrent layer transfers but is not open-ended: a
#: registry that is OOMKilled mid-push fails a build, which is cheap to retry -- unlike the
#: index, whose failure lands at the end of a campaign. No CPU limit, for the same reason
#: the index has none: throttling a push is a slow build with no visible cause.
REGISTRY_RESOURCES = {
    "requests": {"cpu": "10m", "memory": "32Mi"},
    "limits": {"memory": "1Gi"},
}

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


def registry_container(storage_path=DEFAULT_REGISTRY_HOST_PATH, authenticated=False):
    """The registry container to run in the object-store pod.

    *storage_path* is unused here (the volume carries it) and accepted so callers read
    as a pair with :func:`registry_volume`.
    """
    del storage_path
    env = [
        # Let a re-pushed tag replace its predecessor and let the garbage collector
        # reclaim it. Without this the registry refuses deletes outright, and an
        # experiment image rebuilt a hundred times keeps a hundred copies.
        {"name": "REGISTRY_STORAGE_DELETE_ENABLED", "value": "true"},
        {"name": "REGISTRY_STORAGE_FILESYSTEM_ROOTDIRECTORY",
         "value": REGISTRY_DATA_DIR},
        # Serves /debug/health without auth, which is what the probes read. See
        # REGISTRY_DEBUG_PORT: probing /v2/ stops working the moment auth is on.
        {"name": "REGISTRY_HTTP_DEBUG_ADDR", "value": f":{REGISTRY_DEBUG_PORT}"},
    ]
    mounts = [{"name": REGISTRY_VOLUME_NAME, "mountPath": REGISTRY_DATA_DIR}]
    if authenticated:
        env += [
            {"name": "REGISTRY_AUTH", "value": "htpasswd"},
            {"name": "REGISTRY_AUTH_HTPASSWD_REALM", "value": REGISTRY_AUTH_REALM},
            {"name": "REGISTRY_AUTH_HTPASSWD_PATH",
             "value": f"{REGISTRY_AUTH_DIR}/{REGISTRY_HTPASSWD_KEY}"},
        ]
        mounts.append({"name": REGISTRY_AUTH_VOLUME_NAME,
                       "mountPath": REGISTRY_AUTH_DIR, "readOnly": True})
    probe = {"httpGet": {"path": "/debug/health", "port": REGISTRY_DEBUG_PORT}}
    return {
        "name": REGISTRY_CONTAINER_NAME,
        "image": REGISTRY_IMAGE,
        "imagePullPolicy": "IfNotPresent",
        "ports": [{"containerPort": REGISTRY_PORT, "name": "registry"}],
        "env": env,
        "volumeMounts": mounts,
        "resources": REGISTRY_RESOURCES,
        "readinessProbe": dict(probe, initialDelaySeconds=2, periodSeconds=10),
        "livenessProbe": dict(probe, initialDelaySeconds=10, periodSeconds=20),
    }


def registry_volume(storage_path=DEFAULT_REGISTRY_HOST_PATH, storage_class=""):
    """The volume backing the registry: a PVC when one can be provisioned, else hostPath.

    ``emptyDir`` is not offered: any restart of the store pod would discard every built
    image, and campaign Jobs already submitted against those refs would go straight to
    ImagePullBackOff rather than fail honestly. Upgrades no longer restart this pod, but a
    crash, an eviction and a node reboot still do.

    A claim is still offered here, unlike the index's volume, and the asymmetry is
    deliberate: losing the index costs a re-ingest of data that still exists in the object
    store, while losing the blobs strands refs that submitted campaigns are already being
    pulled from. So a deployment that *has* a StorageClass may put the registry on it.

    hostPath is the default because a stock RKE2 cluster ships no StorageClass at all, so
    a PVC there stays Pending forever. It pins the registry's data to one node, which is
    why the store pod carries the ``robovast.io/data-node`` selector
    (:mod:`.node_placement`).
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


#: Ingress annotations the registry needs to be usable, not merely reachable.
#:
#: ingress-nginx defaults ``proxy-body-size`` to 1m, and an image layer is far larger, so
#: without this a push dies on ``413 Request Entity Too Large`` from nginx -- after the
#: build has been paid for, and pointing at the proxy rather than at anything RoboVAST
#: owns. ``0`` disables the limit, which is what a registry needs; the upload is streamed
#: to the container, not buffered whole.
#:
#: The read/send timeouts matter for the same reason: a multi-GB layer over a slow link
#: outlives nginx's 60s default and the push fails partway.
#:
#: Applied unconditionally. They are ingress-nginx keys and other controllers ignore
#: annotations they do not know, which is cheaper than getting the class detection wrong
#: and shipping a registry that 413s.
REGISTRY_INGRESS_ANNOTATIONS = {
    "nginx.ingress.kubernetes.io/proxy-body-size": "0",
    "nginx.ingress.kubernetes.io/proxy-read-timeout": "900",
    "nginx.ingress.kubernetes.io/proxy-send-timeout": "900",
}


def registry_ingress_path():
    """The ``/v2`` rule routing the registry half of the service's hostname.

    Ordered before the catch-all ``/`` rule by the caller. The service itself registers
    no ``/v2`` route, and nginx picks the path before either backend sees the request, so
    the UI's root mount is unaffected.

    The backend is the **store pod's** Service, which is where the registry now runs. One
    Ingress can front two Services on one hostname -- the rule names a backend per path --
    so the published name, its certificate and every image ref built from it are unchanged
    by the move. A deployment created before it carries a ``/v2`` rule pointing at the
    service's Service, which is a dead route rather than a wrong one; see
    :func:`service_deploy.registry_ingress_defects`.
    """
    from . import store_pod  # pylint: disable=import-outside-toplevel

    return {
        "path": REGISTRY_INGRESS_PATH,
        "pathType": "Prefix",
        "backend": {"service": {"name": store_pod.STORE_SERVICE_NAME,
                                "port": {"number": REGISTRY_PORT}}},
    }
