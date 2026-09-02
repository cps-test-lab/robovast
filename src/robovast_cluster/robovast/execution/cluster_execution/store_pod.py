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

"""The ``robovast`` pod: the object store, the image registry and the campaign index.

``vast cluster setup`` creates one pod per deployment for the object store (MinIO, or
nothing at all where the store is an external bucket). This module adds the deployment's
other two pieces of **setup-lifetime infrastructure** to it -- the container registry
(:mod:`.registry_deploy`) and the Postgres campaign index (:mod:`.index_deploy`) -- and
publishes them on the Service that pod already has.

**Why they are not in the service pod, where both started.** ``robovast-service`` is a
Deployment, and every ``vast service upgrade`` rolls it: a container living there is
restarted by each upgrade, including one that only bumps the controller image, and its
volume follows the Deployment rather than the cluster. Neither the registry nor the index
is service-lifetime state. The registry holds the images already-submitted campaigns will
be pulled from; the index holds rows that took hours to ingest. Both belong to the
*cluster*, are created once at setup, and are torn down deliberately by
``vast cluster cleanup``.

**One pod, one Service, four ports.** The store pod's ClusterIP Service already selects
exactly this pod with exactly the selector these containers need, so a second or third
Service object would duplicate that selector, add objects for setup to create and cleanup
to delete, and introduce further names the service's environment has to agree with -- for
no isolation, since a ClusterIP is not a security boundary and a port on it is reachable
from the same pods either way. So ``s3``/``console``/``registry``/``index`` are four ports
on one Service.

**Which addresses change, and which do not.** The index is reached by the service over the
pod network, so its DSN now names :func:`store_host` instead of ``127.0.0.1``. The
registry's address does **not** change: an image ref is resolved twice, by BuildKit inside
a pod and by the kubelet on the node, and only the service's published Ingress host works
for both (see :mod:`.registry_deploy`). That host is unchanged -- what moves is the
Ingress' ``/v2`` backend, from the service's Service to this one. No image ref, no
``imagePullSecret`` and no node configuration is affected.
"""

#: The Pod every embedded-store cluster config deploys, the ClusterIP Service in front of
#: it, and the label the Service selects on. Spelled once, here, because three things must
#: agree on it: the provider manifests that create the pod, the DSN baked into the
#: service's environment, and the Ingress rule routing ``/v2``. Two spellings would drift
#: into a deployment that cannot find its own registry or index while every half looks
#: correct on its own.
STORE_POD_NAME = "robovast"
STORE_SERVICE_NAME = "robovast"
STORE_POD_SELECTOR = {"role": "robovast"}


def store_host(namespace: str = "default") -> str:
    """The in-cluster DNS name the store pod's Service answers on.

    Assembled from the Service name and the namespace it is deployed into -- never from a
    configured hostname or a cluster domain. ``<service>.<namespace>.svc`` is the portable
    half of a Kubernetes name: it resolves in every cluster through the pod's own search
    domain, so nothing here needs to know what the cluster domain was set to, and no
    site-specific host is written into the source.

    The ``.svc`` suffix is not decoration. A bare ``robovast`` resolves through the pod's
    search path, which begins with the *client's own* namespace -- correct only as long as
    the service and the store are deployed together, and silently wrong the day they are
    not.
    """
    return f"{STORE_SERVICE_NAME}.{namespace}.svc"


def _find(docs, kind, name):
    return next((d for d in docs if d.get("kind") == kind
                 and d.get("metadata", {}).get("name") == name), None)


def _add_port(service, name, port):
    ports = service.setdefault("spec", {}).setdefault("ports", [])
    if not any(p.get("port") == port for p in ports):
        ports.append({"name": name, "port": port, "targetPort": port, "protocol": "TCP"})


def attach_infrastructure(docs, namespace="default", index_storage_path="",
                          index_storage_class="", registry_storage_path="",
                          registry_storage_class=""):
    """Add the registry and the index to a provider's parsed store manifest.

    *docs* is the provider's ``robovast`` manifest, parsed, with its store volume already
    placed by :func:`cluster_config.minio_store.apply_store_volume`. The containers are appended to
    the Pod named :data:`STORE_POD_NAME` and their ports to the Service of the same name.

    A provider whose object storage is external deploys no store pod of its own and passes
    an empty *docs*; it gets a Pod and a Service back carrying only these two containers,
    so the registry Ingress and the index DSN name the same host on every provider rather
    than each provider needing its own answer.

    Idempotent by name, so a manifest that already carries them is returned unchanged.
    Returns a new list; PVCs are placed first, because ``apply_manifests`` creates in order
    and a Pod scheduled against a claim that does not exist yet stays Pending.
    """
    from . import index_deploy, registry_deploy  # pylint: disable=import-outside-toplevel

    docs = [d for d in docs if d is not None]
    pod = _find(docs, "Pod", STORE_POD_NAME)
    if pod is None:
        pod = {"apiVersion": "v1", "kind": "Pod",
               "metadata": {"name": STORE_POD_NAME, "namespace": namespace,
                            "labels": dict(STORE_POD_SELECTOR)},
               "spec": {"containers": [], "volumes": []}}
        docs.append(pod)
    spec = pod.setdefault("spec", {})
    containers = spec.setdefault("containers", [])
    volumes = spec.setdefault("volumes", [])
    for container, volume in (
            (registry_deploy.registry_container(),
             registry_deploy.registry_volume(registry_storage_path,
                                             registry_storage_class)),
            (index_deploy.index_container(),
             index_deploy.index_volume(index_storage_path, index_storage_class))):
        if not any(c.get("name") == container["name"] for c in containers):
            containers.append(container)
        if not any(v.get("name") == volume["name"] for v in volumes):
            volumes.append(volume)

    service = _find(docs, "Service", STORE_SERVICE_NAME)
    if service is None:
        service = {"apiVersion": "v1", "kind": "Service",
                   "metadata": {"name": STORE_SERVICE_NAME, "namespace": namespace},
                   "spec": {"type": "ClusterIP", "ports": [],
                            "selector": dict(STORE_POD_SELECTOR)}}
        docs.append(service)
    _add_port(service, "registry", registry_deploy.REGISTRY_PORT)
    _add_port(service, "index", index_deploy.INDEX_PORT)

    claims = [c for c in (registry_deploy.registry_pvc_manifest(namespace,
                                                                registry_storage_class),
                          index_deploy.index_pvc_manifest(namespace, index_storage_class))
              if c]
    return claims + docs


def infrastructure_claims(namespace="default"):
    """Every claim :func:`attach_infrastructure` can create **and cleanup may remove**.

    Cleanup does not know which storage flags the setup that created this cluster was given,
    so this enumerates what *could* exist; deletion tolerates a 404, which makes that both
    correct and cheap.

    **Only the re-derivable ones are here.** Built images are rebuilt on demand and the index
    is re-ingested from the campaigns beside it, so removing either costs time and nothing
    else. The object store's own claim is deliberately absent: it holds the campaigns, and a
    cleanup that deleted them would mean something different on a provisioned cluster than on
    one whose store is a directory cleanup leaves alone. ``vast cluster cleanup --delete-data``
    is how that is asked for.
    """
    from . import index_deploy, registry_deploy  # pylint: disable=import-outside-toplevel

    return [registry_deploy.registry_pvc_manifest(namespace, "local-path"),
            index_deploy.index_pvc_manifest(namespace, "local-path")]


def infrastructure_container_names():
    """The containers :func:`attach_infrastructure` puts in the store pod, by name."""
    from . import index_deploy, registry_deploy  # pylint: disable=import-outside-toplevel


    return (registry_deploy.REGISTRY_CONTAINER_NAME, index_deploy.INDEX_CONTAINER_NAME)


def missing_infrastructure(pod) -> list:
    """Which of the store pod's infrastructure containers a **live** pod does not run.

    ``apply_manifests`` tolerates a 409 on the store pod and keeps the running one --
    deliberately, since recreating it would discard the campaign store. The cost is that a
    cluster set up before the registry and the index moved here does not gain them by
    re-running setup, and would otherwise carry on looking healthy: the service would come
    up, its Ingress would route ``/v2`` at a container that is not there, and its DSN would
    name a port nothing listens on. Neither failure appears until a build is pushed or a
    campaign is queried.

    *pod* is a ``V1Pod`` (or ``None`` for "no such pod"), so the caller decides what an
    unreadable cluster means.
    """
    if pod is None:
        return list(infrastructure_container_names())
    running = {getattr(c, "name", None) for c in (pod.spec.containers or [])}
    return [name for name in infrastructure_container_names() if name not in running]
