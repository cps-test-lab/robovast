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

"""The ``robovast`` pod: the image registry.

``vast cluster setup`` creates one pod per deployment for its **setup-lifetime
infrastructure** -- the container registry (:mod:`.registry_deploy`) -- and one ClusterIP
Service in front of it. Every provider deploys exactly this pod; what differs between
providers is where its volume comes from, and that arrives as arguments.

**Why it is not in the service pod.** ``robovast-service`` is a Deployment, and every
``vast service upgrade`` rolls it: a container living there is restarted by each upgrade,
including one that only bumps the controller image, and its volume follows the Deployment
rather than the cluster. The registry is not service-lifetime state: it holds the images
already-submitted campaigns will be pulled from. It belongs to the *cluster*, is created
once at setup, and is torn down deliberately by ``vast cluster cleanup``.

**Campaigns are not here.** They live on the service's results volume
(:data:`.service_deploy.RESULTS_VOLUME_NAME`), which the service Deployment carries, and
the tables derived from them are built beside them, in each campaign's ``.cache/``. The
registry holds blobs that are rebuilt on demand.

**Which address is which.** The registry is reached through the service's published
Ingress host: an image ref is resolved twice, by BuildKit inside a pod and by the kubelet
on the node, and only that host works for both (see :mod:`.registry_deploy`). The
Ingress' ``/v2`` backend is this pod's Service.
"""

#: The Pod every cluster config deploys, the ClusterIP Service in front of it, and the
#: label the Service selects on. Spelled once, here, because the manifest that creates the
#: pod and the Ingress rule routing ``/v2`` must agree on it. Two spellings would drift into
#: a deployment that cannot find its own registry while every half looks correct on its own.
STORE_POD_NAME = "robovast"
STORE_SERVICE_NAME = "robovast"
STORE_POD_SELECTOR = {"role": "robovast"}

#: The container name of an object store a live ``robovast`` pod may carry. Named so
#: such a pod is refused (:func:`carries_an_object_store`): campaigns live on the service's
#: results volume, and a pod holding a store nothing reads is a deployment whose campaigns
#: are somewhere the service does not look.
OBJECT_STORE_CONTAINER_NAME = "minio"


def store_host(namespace: str = "default") -> str:
    """The in-cluster DNS name the ``robovast`` pod's Service answers on.

    Assembled from the Service name and the namespace it is deployed into -- never from a
    configured hostname or a cluster domain. ``<service>.<namespace>.svc`` is the portable
    half of a Kubernetes name: it resolves in every cluster through the pod's own search
    domain, so nothing here needs to know what the cluster domain was set to, and no
    site-specific host is written into the source.

    The ``.svc`` suffix is not decoration. A bare ``robovast`` resolves through the pod's
    search path, which begins with the *client's own* namespace -- correct only as long as
    the service and this pod are deployed together, and silently wrong the day they are
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


def attach_infrastructure(docs, namespace="default", registry_storage_path="",
                          registry_storage_class="", ingress_class="",
                          registry_authenticated=False):
    """The ``robovast`` pod's manifest: the registry, its claim, the Service.

    *docs* is what a caller already has of it, parsed -- ``[]`` for a fresh manifest, which
    is what every provider passes. The container is appended to the Pod named
    :data:`STORE_POD_NAME` (created here when absent) and its port to the Service of the
    same name, so the registry Ingress names one host on every provider.

    The volume is a claim where a class is given and a ``hostPath`` otherwise; a hostPath
    left empty takes :data:`.data_paths.DEFAULT_REGISTRY_HOST_PATH`.

    Idempotent by name, so a manifest that already carries them is returned unchanged.
    Returns a new list; PVCs are placed first, because ``apply_manifests`` creates in order
    and a Pod scheduled against a claim that does not exist yet stays Pending.
    """
    from . import registry_deploy  # pylint: disable=import-outside-toplevel

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
    container = registry_deploy.registry_container(authenticated=registry_authenticated)
    if not any(c.get("name") == container["name"] for c in containers):
        containers.append(container)
    wanted = [registry_deploy.registry_volume(registry_storage_path, registry_storage_class)]
    if registry_authenticated:
        # The password file, beside the blobs it guards.
        wanted.append(registry_deploy.registry_auth_volume())
    for volume in wanted:
        if not any(v.get("name") == volume["name"] for v in volumes):
            volumes.append(volume)

    service = _find(docs, "Service", STORE_SERVICE_NAME)
    if service is None:
        service = {"apiVersion": "v1", "kind": "Service",
                   "metadata": {"name": STORE_SERVICE_NAME, "namespace": namespace},
                   "spec": {"type": "ClusterIP", "ports": [],
                            "selector": dict(STORE_POD_SELECTOR)}}
        docs.append(service)
    # This Service is the registry's Ingress backend, so it needs whatever the controller
    # requires of one -- the same annotations the service's own Service gets, from the same
    # place. Merged rather than assigned: a provider's manifest may carry its own.
    backend = registry_deploy.ingress_backend_annotations(ingress_class)
    if backend:
        service["metadata"].setdefault("annotations", {}).update(backend)
    _add_port(service, "registry", registry_deploy.REGISTRY_PORT)

    claim = registry_deploy.registry_pvc_manifest(namespace, registry_storage_class)
    return ([claim] if claim else []) + docs


def infrastructure_claims(namespace="default"):
    """Every claim :func:`attach_infrastructure` can create **and cleanup may remove**.

    Cleanup does not know which storage flags the setup that created this cluster was given,
    so this enumerates what *could* exist; deletion tolerates a 404, which makes that both
    correct and cheap.

    **It is re-derivable.** Built images are rebuilt on demand, so removing the claim costs
    time and nothing else. The results volume is the service Deployment's and is not touched here:
    it holds the campaigns, and ``vast cluster cleanup --delete-data`` is how emptying it is
    asked for.
    """
    from . import registry_deploy  # pylint: disable=import-outside-toplevel

    return [registry_deploy.registry_pvc_manifest(namespace, "local-path")]


def infrastructure_container_names():
    """The containers :func:`attach_infrastructure` puts in the pod, by name."""
    from . import registry_deploy  # pylint: disable=import-outside-toplevel

    return (registry_deploy.REGISTRY_CONTAINER_NAME,)


def missing_infrastructure(pod) -> list:
    """Which of the pod's infrastructure containers a **live** pod does not run.

    ``apply_manifests`` tolerates a 409 on this pod and keeps the running one --
    deliberately, since recreating it on every setup would restart the registry for nothing.
    The cost is that a live pod missing a container does not gain it by re-running setup,
    and would otherwise carry on looking healthy: the service would come up and its Ingress
    would route ``/v2`` at a container that is not there. That failure does not appear until
    a build is pushed.

    *pod* is a ``V1Pod`` (or ``None`` for "no such pod"), so the caller decides what an
    unreadable cluster means.
    """
    if pod is None:
        return list(infrastructure_container_names())
    running = {getattr(c, "name", None) for c in (pod.spec.containers or [])}
    return [name for name in infrastructure_container_names() if name not in running]


def carries_an_object_store(pod) -> bool:
    """Whether a **live** pod runs an object-store container beside the registry.

    Campaigns live on the service's results volume, and nothing reads such a store: a
    deployment whose pod carries one keeps its campaigns where the service does not
    look. The same 409 that keeps a live pod keeps that container, so the pod has to be
    recreated -- and the campaigns in that store are not migrated, which is why the caller
    refuses rather than proceeds.
    """
    if pod is None:
        return False
    return any(getattr(c, "name", None) == OBJECT_STORE_CONTAINER_NAME
               for c in (pod.spec.containers or []))


def refuse_a_pod_on_the_wrong_node(namespace, node_labels):
    """Raise when the live ``robovast`` pod sits somewhere the resolved placement does not want.

    ``apply_manifests`` tolerates a 409 and **keeps** the existing object, so a changed
    ``nodeSelector`` does not take effect: setup would print "completed successfully" over
    a pod still on the old node, with the registry blobs on a machine nobody chose. Checked before the apply rather than reported afterwards, because a placement
    that is announced and not applied is the failure the placement label exists to remove.

    Recreating the pod costs nothing durable: the registry is on the node directory or the
    claim it was given, and a pod recreated on the same node finds it again; on another node
    the registry starts empty and images are rebuilt on demand.
    """
    from kubernetes import client  # pylint: disable=import-outside-toplevel

    if not node_labels:
        return
    try:
        pod = client.CoreV1Api().read_namespaced_pod(STORE_POD_NAME, namespace)
    except client.exceptions.ApiException as e:
        if e.status == 404:
            return          # nothing live; the manifest will simply be created
        raise
    selector = pod.spec.node_selector or {}
    if all(selector.get(k) == v for k, v in node_labels.items()):
        return
    raise RuntimeError(
        f"the {STORE_POD_NAME} pod (the registry) is already running on node "
        f"{pod.spec.node_name} and cannot be moved by re-applying its manifest -- an "
        f"existing pod is kept as it is, so the new placement would be reported but never "
        f"take effect. Delete it (`kubectl delete pod {STORE_POD_NAME} -n {namespace}`) or "
        f"run `vast cluster cleanup` first. Built images are rebuilt on demand; the campaigns "
        f"are not in this pod.")


def registry_enforces_auth(pod) -> bool:
    """Whether the **live** registry container is actually configured to authenticate.

    The same 409 that keeps an existing pod keeps its old container spec, so turning
    auth on in the manifest does not turn it on in the cluster. Without this the credential
    would be minted, written to both Secrets and reported as done, over a registry still
    serving anonymous pushes -- a setup that says it closed a hole it left open, which is
    worse than one that never claimed to.

    Reads the container's environment rather than a version or a label: it is the thing
    that decides, so it cannot be right here and wrong in the pod.

    ``False`` for a pod that is absent or carries no registry container -- both are already
    reported by :func:`missing_infrastructure`, and answering "not authenticating" is true
    of them anyway.
    """
    from . import registry_deploy  # pylint: disable=import-outside-toplevel

    if pod is None:
        return False
    for container in (pod.spec.containers or []):
        if getattr(container, "name", None) != registry_deploy.REGISTRY_CONTAINER_NAME:
            continue
        return any(getattr(e, "name", None) == "REGISTRY_AUTH"
                   for e in (getattr(container, "env", None) or []))
    return False
