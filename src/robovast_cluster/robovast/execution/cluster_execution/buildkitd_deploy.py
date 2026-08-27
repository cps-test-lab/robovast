# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""The shared BuildKit daemon: one long-lived builder with a store that outlives a build.

Every build used to get a **fresh** BuildKit, spawned inside its own Job by
``buildctl-daemonless.sh``. That is why the registry layer cache exists at all -- with nothing
on a node reusable between builds, a registry was the only cache there could be. It also meant
two costs were paid on every single build, and neither showed up as anything but "the build is
slow":

* the base image was pulled again -- measured at 95-110 s per container, on builds where every
  layer was already a cache hit;
* ``RUN --mount=type=cache`` was thrown away, so a pip layer that missed re-downloaded its
  wheels in full. One torch group took 226 s where the same step, with a warm download cache,
  takes 172 s.

A daemon that keeps its store fixes both, and nothing else can: a cold builder has to
materialise the base whatever the registry holds.

**What this is not.** The image-warm DaemonSet beside this file warms *containerd's* store on
each node, which is what makes campaign pods start quickly. Rootless BuildKit runs the OCI
worker, which has its own content store and cannot read containerd's -- so a warmed node has
never done anything for a build. The two are complementary, and neither substitutes for the
other. (Running buildkitd against ``--containerd-worker`` would unify them, at the price of
privileged access to the containerd socket. Deliberately not done here.)

The build Job stays a Job; it just becomes a **client** of this daemon rather than a builder in
its own right. ``buildctl --local`` resolves its paths client-side and streams them over the
session, so the staged context in the Job's own emptyDir still works untouched.
"""

import logging

logger = logging.getLogger(__name__)

#: One object per deployment, so the name is fixed rather than derived -- that is what makes an
#: ``upgrade`` patch the existing daemon instead of standing a second one up beside it.
BUILDKITD_NAME = "robovast-buildkitd"

#: buildctl's default port, and no reason to move it.
BUILDKITD_PORT = 1234

#: Where the daemon's store is mounted, and -- via ``root`` in the generated config -- where it
#: is told to keep it.
#:
#: **Declared, not inferred.** The rootless image defaults its store to
#: ``$HOME/.local/share/buildkit`` while the rootful one uses ``/var/lib/buildkit``, so a mount
#: that matches "the default" is only correct for as long as nobody changes image variant. Get
#: it wrong and the daemon writes to the container's writable layer: the volume is mounted, the
#: pod is healthy, every build works -- and nothing persists. The only symptom is that the base
#: image keeps being pulled, which is indistinguishable from this whole change not having been
#: made. Setting ``root`` explicitly is what turns that from a silent failure into a fact.
BUILDKITD_STORE_DIR = "/home/user/.local/share/buildkit"

#: Default host directory backing the store when no StorageClass is given. A stock RKE2 cluster
#: ships no StorageClass at all, so a PVC there stays ``Pending`` forever -- the same reason the
#: in-cluster registry defaults to a hostPath. It ties the cache to one node, which is why the
#: daemon carries the ``robovast.io/build-node`` selector (:mod:`.node_placement`).
DEFAULT_BUILDKITD_HOST_PATH = "/data/robovast-buildkit"

#: Size of the PVC when one is used. Only meaningful on a cluster that has a StorageClass; the
#: hostPath default is bounded by the node's disk and by the GC ceiling below.
DEFAULT_BUILDKITD_STORAGE_SIZE = "200Gi"

#: What the daemon is allowed to keep. **Not optional, and not a detail.** The point of this
#: component is that state survives, which is exactly what makes an unbounded store fill a disk
#: it shares with something else. On the hostPath default that disk belongs to a node the
#: service pod may also be pinned to, and a full one means DiskPressure evictions rather than a
#: failed build.
#:
#: What the daemon may keep. **Not optional**: the point of this component is that state
#: survives, which is exactly what makes an unbounded store fill a disk shared with other
#: things. On the hostPath default that disk belongs to a node the service pod may also be
#: pinned to, and a full one means DiskPressure evictions rather than a failed build.
#:
#: The three keys do different jobs, and ``minFreeSpace`` is the one that makes the other two
#: safe to state in absolute terms. A fixed ceiling is only a ceiling on a disk at least that
#: large -- on a smaller one the store simply grows until the *node* runs out. ``minFreeSpace``
#: is measured against the filesystem rather than the cache, so it forces pruning long before
#: an oversized ceiling is reached, whatever the disk turns out to be. That is what lets these
#: be chosen for the deployment in front of us (500 GB free) without becoming a trap on a
#: deployment that is not.
#:
#: ``reservedSpace`` is a floor, not a target: cache below it is kept even when old, which is
#: what stops a quiet week from evicting the base image this exists to hold.
DEFAULT_BUILDKITD_GC_RESERVED = "100GB"
DEFAULT_BUILDKITD_GC_MAX_USED = "150GB"
DEFAULT_BUILDKITD_GC_MIN_FREE = "50GB"

#: What the daemon reserves. Unlike the warm DaemonSet's near-nothing, this is a real workload:
#: it compiles, unpacks and compresses layers, and a solve holds gigabytes.
#:
#: The number matters more than it looks. Admission measures free capacity as node allocatable
#: minus the requests of every pod bound to a node, so whatever is asked for here is taken
#: directly out of what campaigns may run. Asking for nothing is not the cheap option -- it lands the pod in
#: BestEffort QoS, first in line for eviction under node memory pressure, i.e. exactly during a
#: heavy build. Modest requests with generous limits is the honest shape: reserve what it needs
#: to be schedulable and survive, burst into whatever is idle.
BUILDKITD_CPU_REQUEST = "500m"
BUILDKITD_MEMORY_REQUEST = "2Gi"
BUILDKITD_CPU_LIMIT = "8"
BUILDKITD_MEMORY_LIMIT = "16Gi"

#: Cap on concurrent build steps across the **whole daemon**, not per build. Left to its default
#: it is the node's CPU count, which is capacity admission has already counted out to campaign
#: jobs -- so an unbounded daemon competes with the runs it exists to serve.
BUILDKITD_MAX_PARALLELISM = 4

_CA_MOUNT = "/certs"
#: Deliberately NOT under ``$HOME``. The config arrives as a read-only ConfigMap volume, and a
#: volume mount shadows the whole directory it lands on -- so mounting it at the conventional
#: ``~/.config/buildkit`` would make that path read-only for rootlesskit too, which keeps its
#: own state under ``$HOME``. Passing ``--config`` explicitly means the location is ours to
#: choose, so it is chosen somewhere that collides with nothing.
_CONF_DIR = "/etc/robovast-buildkit"
_CA_BUNDLE = "/tmp/robovast-ca-bundle.crt"
_SYSTEM_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"
_STORE_VOLUME = "buildkit-store"
_CONF_VOLUME = "buildkit-config"


def buildkitd_address(namespace: str) -> str:
    """The ``--addr`` a build client dials.

    A Service DNS name rather than a pod IP: the pod is replaced by every ``upgrade`` and by any
    reschedule, and a build submitted seconds before that must not be holding an address that
    has stopped existing.
    """
    return f"tcp://{BUILDKITD_NAME}.{namespace}.svc:{BUILDKITD_PORT}"


def buildkitd_toml(*, registry_host: str = "", gc_reserved: str = DEFAULT_BUILDKITD_GC_RESERVED,
                   gc_max_used: str = DEFAULT_BUILDKITD_GC_MAX_USED,
                   gc_min_free: str = DEFAULT_BUILDKITD_GC_MIN_FREE) -> str:
    """The daemon's config: where its store is, what it may keep, and whose CA to trust.

    ``root`` is here rather than left to the image's default for the reason
    :data:`BUILDKITD_STORE_DIR` gives -- a wrong store path is invisible.

    The GC keys are the ones this BuildKit understands, and they are version-coupled: the older
    ``gckeepstorage`` was replaced by the ``reservedSpace``/``maxUsedSpace``/``minFreeSpace``
    triple, so this config and ``BUILDKIT_IMAGE``'s pin have to move together. That coupling is
    half the reason the image is pinned at all.

    ``registry_host`` puts a private registry's CA where the **daemon** can use it, which is
    where it now belongs: the daemon resolves, pulls and pushes, so it is the side making the
    TLS connection to the registry API. The client keeps its own copy on ``SSL_CERT_FILE`` for
    the token endpoint -- see the note in ``cluster_image_build.build_job_manifest``.
    """
    lines = [f'root = "{BUILDKITD_STORE_DIR}"', "",
             "[worker.oci]",
             "  enabled = true",
             "  gc = true",
             "  [[worker.oci.gcpolicy]]",
             f'    reservedSpace = "{gc_reserved}"',
             f'    maxUsedSpace = "{gc_max_used}"',
             f'    minFreeSpace = "{gc_min_free}"']
    if registry_host:
        lines += ["", f'[registry."{registry_host}"]', f'  ca = ["{_CA_MOUNT}/ca.pem"]']
    return "\n".join(lines) + "\n"


def buildkitd_configmap_manifest(namespace: str, *, registry_host: str = "",
                                 gc_reserved: str = DEFAULT_BUILDKITD_GC_RESERVED,
                                 gc_max_used: str = DEFAULT_BUILDKITD_GC_MAX_USED,
                                 gc_min_free: str = DEFAULT_BUILDKITD_GC_MIN_FREE) -> dict:
    """The ConfigMap holding :func:`buildkitd_toml`."""
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": f"{BUILDKITD_NAME}-config", "namespace": namespace,
                     "labels": {"app": BUILDKITD_NAME}},
        "data": {"buildkitd.toml": buildkitd_toml(
            registry_host=registry_host, gc_reserved=gc_reserved,
            gc_max_used=gc_max_used, gc_min_free=gc_min_free)},
    }


def buildkitd_volume(storage_path: str = DEFAULT_BUILDKITD_HOST_PATH,
                     storage_class: str = "") -> dict:
    """The store's backing volume: a PVC where there is a StorageClass, a hostPath otherwise.

    ``emptyDir`` is not one of the options, and refusing it is the whole point of the component
    -- an emptyDir store is what the per-build daemon already had.
    """
    if storage_class:
        return {"name": _STORE_VOLUME,
                "persistentVolumeClaim": {"claimName": BUILDKITD_NAME}}
    return {"name": _STORE_VOLUME,
            "hostPath": {"path": storage_path or DEFAULT_BUILDKITD_HOST_PATH,
                         "type": "DirectoryOrCreate"}}


def buildkitd_pvc_manifest(namespace: str, storage_class: str,
                           size: str = DEFAULT_BUILDKITD_STORAGE_SIZE) -> "dict | None":
    """The claim :func:`buildkitd_volume` names, or ``None`` when backed by a hostPath.

    Created by :func:`apply_buildkitd` rather than handed to ``deploy_service``: that function
    builds a PVC for the registry and then applies no PVC at all, so the one branch of that
    precedent this could have copied is the untested one.

    ``ReadWriteOnce`` because there is exactly one replica. That is also why the Deployment uses
    ``Recreate`` -- two pods could not mount this at once, so a rolling update would deadlock
    waiting for a new pod that cannot start until the old one is gone.
    """
    if not storage_class:
        return None
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": BUILDKITD_NAME, "namespace": namespace,
                     "labels": {"app": BUILDKITD_NAME}},
        "spec": {"accessModes": ["ReadWriteOnce"],
                 "storageClassName": storage_class,
                 "resources": {"requests": {"storage": size or DEFAULT_BUILDKITD_STORAGE_SIZE}}},
    }


def _tolerations() -> list:
    """What the daemon must tolerate to be schedulable where the work is.

    Read from where the campaign job pods get it rather than restated, so there is one place
    to change if the taint moves -- the same reasoning the warm DaemonSet gives. It used to
    be Kueue's ResourceFlavor that injected this; a job pod carries it itself now.
    """
    from .node_placement import CAMPAIGN_NODE_TOLERATIONS
    return [dict(t) for t in CAMPAIGN_NODE_TOLERATIONS]


def buildkitd_deployment_manifest(*, namespace: str, storage_path: str = "",
                                  storage_class: str = "", node_selector=None,
                                  pull_secret_name: str = "", ca_configmap_name: str = "",
                                  host_aliases=None, cpu_request: str = BUILDKITD_CPU_REQUEST,
                                  memory_request: str = BUILDKITD_MEMORY_REQUEST,
                                  stamp: str = "") -> dict:
    """The daemon itself.

    ``Recreate`` rather than a rolling update: one replica over ReadWriteOnce storage cannot
    have two pods, and on a hostPath two writers on one store would be worse than downtime.
    The cost is real and belongs in the open -- replacing this pod kills every build in flight,
    and the build path deliberately *detaches* a waiting campaign rather than cancelling it, so
    a sibling campaign can be waiting on a build this restart destroys. There is no drain.

    ``stamp`` goes into the pod template's restart annotation. Unlike the warm DaemonSet, this
    one should **not** be stamped on every deploy: rolling it discards nothing persistent, but it
    does interrupt whatever was building, and an upgrade that changed nothing about the daemon
    has no reason to. Pass a stamp only when the daemon's own definition changed.
    """
    from .cluster_image_build import BUILDKIT_IMAGE
    from .service_deploy import RESTART_ANNOTATION

    args = [
        "--addr", f"tcp://0.0.0.0:{BUILDKITD_PORT}",
        # Rootless BuildKit runs inside a user namespace it creates itself; without this it
        # tries to create another one per RUN step and dies on nodes whose kernel or runtime
        # refuses the nesting. The same flag the per-build daemon carried, for the same reason.
        "--oci-worker-no-process-sandbox",
        "--config", f"{_CONF_DIR}/buildkitd.toml",
        "--oci-max-parallelism", str(BUILDKITD_MAX_PARALLELISM),
    ]

    volumes = [
        buildkitd_volume(storage_path or DEFAULT_BUILDKITD_HOST_PATH, storage_class),
        {"name": _CONF_VOLUME, "configMap": {"name": f"{BUILDKITD_NAME}-config"}},
    ]
    mounts = [
        {"name": _STORE_VOLUME, "mountPath": BUILDKITD_STORE_DIR},
        {"name": _CONF_VOLUME, "mountPath": _CONF_DIR, "readOnly": True},
    ]
    command = None
    if ca_configmap_name:
        volumes.append({"name": "registry-ca", "configMap": {"name": ca_configmap_name}})
        mounts.append({"name": "registry-ca", "mountPath": _CA_MOUNT, "readOnly": True})
        # Two mechanisms, because they answer different requests: the per-registry ``ca`` in
        # buildkitd.toml covers the registry API, while Go's system pool covers the token
        # endpoint named in WWW-Authenticate. Rootless cannot write /etc/ssl/certs, so the
        # bundle is assembled in /tmp and pointed at.
        # `rootlesskit` is re-stated because setting `command` REPLACES the image's
        # entrypoint, which is `["rootlesskit", "buildkitd"]`. Dropping it would start
        # buildkitd outside the user namespace rootlesskit creates, and rootless BuildKit
        # cannot work without one -- so the daemon would fail at startup, in a branch that
        # only runs on deployments with a private registry CA.
        command = ["sh", "-c",
                   f"{{ cat {_SYSTEM_CA_BUNDLE} 2>/dev/null || true; "
                   f"cat {_CA_MOUNT}/ca.pem; }} > {_CA_BUNDLE} && "
                   f"export SSL_CERT_FILE={_CA_BUNDLE} && "
                   "exec rootlesskit buildkitd " + " ".join(args)]

    container = {
        "name": "buildkitd",
        "image": BUILDKIT_IMAGE,
        "imagePullPolicy": "IfNotPresent",
        "ports": [{"containerPort": BUILDKITD_PORT, "name": "buildkit"}],
        "volumeMounts": mounts,
        "resources": {
            "requests": {"cpu": cpu_request or BUILDKITD_CPU_REQUEST,
                         "memory": memory_request or BUILDKITD_MEMORY_REQUEST},
            "limits": {"cpu": BUILDKITD_CPU_LIMIT, "memory": BUILDKITD_MEMORY_LIMIT},
        },
        # Rootless BuildKit needs both Unconfined: rootlesskit's mount namespace is what
        # AppArmor and seccomp otherwise refuse, with "failed to share mount point: /:
        # permission denied" on RKE2/containerd. The same pair the per-build pod carried.
        "securityContext": {
            "runAsUser": 1000, "runAsGroup": 1000,
            "seccompProfile": {"type": "Unconfined"},
            "appArmorProfile": {"type": "Unconfined"},
        },
        # A TCP probe, not an exec: readiness here means "a client can dial and be served",
        # which is exactly what the port accepting a connection says.
        "readinessProbe": {"tcpSocket": {"port": BUILDKITD_PORT},
                           "initialDelaySeconds": 3, "periodSeconds": 10},
        "livenessProbe": {"tcpSocket": {"port": BUILDKITD_PORT},
                          "initialDelaySeconds": 30, "periodSeconds": 30},
    }
    if command:
        container["command"] = command
    else:
        container["args"] = args

    # The store has to be writable by uid 1000 before buildkitd touches it, and nothing else
    # will make it so. A `DirectoryOrCreate` hostPath is created by the kubelet as root:root
    # 0755, and `fsGroup` -- the usual answer -- does not apply to hostPath: Kubernetes does
    # ownership management only for volume types that support it, and that is not one. Without
    # this the daemon dies at startup with "open .../buildkitd.lock: permission denied", which
    # reads like a bug in the image rather than a property of the mount.
    #
    # Not recursive, deliberately. The directory is empty when it is first created, and
    # everything under it after that is written by the daemon as 1000 -- so a `chown -R` would
    # buy nothing and walk a store that is meant to grow to a hundred gigabytes on every single
    # start. It uses the buildkit image rather than pulling another, since that one is by
    # definition already on the node.
    init_container = {
        "name": "store-permissions",
        "image": BUILDKIT_IMAGE,
        "imagePullPolicy": "IfNotPresent",
        "command": ["chown", "1000:1000", BUILDKITD_STORE_DIR],
        "securityContext": {"runAsUser": 0, "runAsGroup": 0},
        "volumeMounts": [{"name": _STORE_VOLUME, "mountPath": BUILDKITD_STORE_DIR}],
    }

    pod_spec = {"containers": [container], "initContainers": [init_container],
                "volumes": volumes, "tolerations": _tolerations()}
    if node_selector:
        pod_spec["nodeSelector"] = dict(node_selector)
    if pull_secret_name:
        pod_spec["imagePullSecrets"] = [{"name": pull_secret_name}]
    if host_aliases:
        # The daemon resolves the registry for its own pulls and pushes, so it needs the same
        # aliases the client does. Note these are frozen into the pod spec until the next
        # apply, where the client reads them per build -- a change now needs an upgrade.
        pod_spec["hostAliases"] = host_aliases

    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": BUILDKITD_NAME, "namespace": namespace,
                     "labels": {"app": BUILDKITD_NAME}},
        "spec": {
            "replicas": 1,
            "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": {"app": BUILDKITD_NAME}},
            "template": {
                "metadata": {"labels": {"app": BUILDKITD_NAME},
                             **({"annotations": {RESTART_ANNOTATION: stamp}} if stamp else {})},
                "spec": pod_spec,
            },
        },
    }


def buildkitd_service_manifest(namespace: str) -> dict:
    """A ClusterIP for the daemon, and deliberately nothing more.

    No Ingress. A buildkitd endpoint is unauthenticated -- BuildKit offers mTLS and nothing
    else -- and publishing one would hand arbitrary in-cluster builds to anyone who found it.
    Reachable from inside the namespace is already a wider audience than it looks: campaign
    pods run images a ``.vast`` chose. See the security note in ``docs/cluster_execution.rst``.
    """
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": BUILDKITD_NAME, "namespace": namespace,
                     "labels": {"app": BUILDKITD_NAME}},
        "spec": {
            "selector": {"app": BUILDKITD_NAME},
            "ports": [{"name": "buildkit", "port": BUILDKITD_PORT,
                       "targetPort": BUILDKITD_PORT}],
        },
    }


def apply_buildkitd(namespace: str, *, kube_context=None, storage_path: str = "",
                    storage_class: str = "", storage_size: str = "", node_selector=None,
                    pull_secret_name: str = "", ca_configmap_name: str = "",
                    registry_host: str = "", host_aliases=None,
                    cpu_request: str = "", memory_request: str = "",
                    gc_reserved: str = "", gc_max_used: str = "", gc_min_free: str = "",
                    stamp: str = "") -> None:
    """Create or converge the daemon: its claim, its config, its Deployment and its Service.

    Applied here rather than through ``deploy_service`` because that function dispatches on
    ``{m["kind"]: m}`` -- one entry per kind -- so a second Deployment and a second Service
    would silently replace the service's own and never be applied.

    Order matters: the claim and the config must exist before the Deployment references them,
    or the pod sits in ``ContainerCreating`` on a missing volume with nothing saying why.
    """
    from kubernetes import client

    from .kube_client import load_kube_config

    load_kube_config(kube_context)
    core, apps = client.CoreV1Api(), client.AppsV1Api()

    pvc = buildkitd_pvc_manifest(namespace, storage_class, storage_size)
    if pvc:
        try:
            core.create_namespaced_persistent_volume_claim(namespace, pvc)
            logger.info("created the buildkitd volume claim in %s", namespace)
        except client.exceptions.ApiException as e:
            if e.status != 409:
                raise
            # Tolerated, never replaced: a bound claim's spec is near-immutable, and the
            # resize/reclass path is an operator decision (delete and lose the cache), not
            # something an upgrade should take on its own.
            logger.info("buildkitd volume claim already exists in %s; left as it is", namespace)

    cfg = buildkitd_configmap_manifest(
        namespace, registry_host=registry_host,
        gc_reserved=gc_reserved or DEFAULT_BUILDKITD_GC_RESERVED,
        gc_max_used=gc_max_used or DEFAULT_BUILDKITD_GC_MAX_USED,
        gc_min_free=gc_min_free or DEFAULT_BUILDKITD_GC_MIN_FREE)
    try:
        core.create_namespaced_config_map(namespace, cfg)
    except client.exceptions.ApiException as e:
        if e.status != 409:
            raise
        core.replace_namespaced_config_map(f"{BUILDKITD_NAME}-config", namespace, cfg)

    if node_selector is None:
        # `None` is "resolve", `{}` is "explicitly unpinned" -- same three-valued contract as
        # the service's, and for the same reason: a caller that forgets must not thereby unpin
        # a hostPath cache. Auto-picking is refused; only `setup` decides a placement.
        from .node_placement import (  # pylint: disable=import-outside-toplevel
            BUILD_NODE_LABEL, resolve_placement)
        placement = resolve_placement(core, BUILD_NODE_LABEL, node_local=not storage_class,
                                      allow_auto_pick=False)
        node_selector = placement.selector if placement else {}

    dep = buildkitd_deployment_manifest(
        namespace=namespace, storage_path=storage_path, storage_class=storage_class,
        node_selector=node_selector, pull_secret_name=pull_secret_name,
        ca_configmap_name=ca_configmap_name, host_aliases=host_aliases,
        cpu_request=cpu_request or BUILDKITD_CPU_REQUEST,
        memory_request=memory_request or BUILDKITD_MEMORY_REQUEST, stamp=stamp)
    try:
        apps.create_namespaced_deployment(namespace, dep)
        logger.info("created the buildkitd Deployment in %s", namespace)
    except client.exceptions.ApiException as e:
        if e.status != 409:
            raise
        # Replace, not patch. A strategic merge keeps fields the new spec omits -- so a volume
        # switched from a claim to a hostPath would merge into one carrying both sources and be
        # rejected, and a dropped mount would linger. Replace says what this means: make it look
        # like this. `spec.selector` is immutable and constant here, so there is nothing for a
        # replace to be refused over.
        apps.replace_namespaced_deployment(BUILDKITD_NAME, namespace, dep)
        logger.info("converged the buildkitd Deployment in %s", namespace)

    svc = buildkitd_service_manifest(namespace)
    try:
        core.create_namespaced_service(namespace, svc)
    except client.exceptions.ApiException as e:
        if e.status != 409:
            raise
        # Patched on ports only: clusterIP is assigned once and is immutable, so a replace
        # carrying no clusterIP is rejected.
        core.patch_namespaced_service(BUILDKITD_NAME, namespace,
                                      {"spec": {"ports": svc["spec"]["ports"]}})


def buildkitd_storage_from_cluster(namespace: str, kube_context=None) -> dict:
    """Recover the daemon's storage settings from the live objects, as ``apply_buildkitd`` kwargs.

    These arrive as ``setup`` flags and **nothing records them**: they exist only in the
    Deployment and the claim that Deployment produced. So an ``upgrade`` re-rendering the daemon
    from defaults would move a PVC-backed store back to a hostPath -- a new empty cache, on
    whichever node the pod next lands on, while the old claim still holds its space and nothing
    says so. The symptom is the one this component exists to remove: base images pulled again,
    which looks exactly like the daemon not being there.

    ``node_selector`` is here despite not being a storage setting, because it is a property of
    the store rather than of scheduling: a hostPath store lives on the node the pod landed on,
    so losing the pin loses the cache as thoroughly as losing the path does.

    Returns ``{}`` when there is no Deployment -- the honest answer for a deployment that
    predates the daemon, and the caller then creates it from its own defaults. A cluster that
    *fails* to answer is not that answer and is not swallowed: defaulting there is precisely the
    silent migration above, so the exception propagates.
    """
    from kubernetes import client  # pylint: disable=import-outside-toplevel

    from .kube_client import load_kube_config  # pylint: disable=import-outside-toplevel

    load_kube_config(kube_context)
    apps, core = client.AppsV1Api(), client.CoreV1Api()
    try:
        dep = apps.read_namespaced_deployment(BUILDKITD_NAME, namespace)
    except client.exceptions.ApiException as e:
        if e.status != 404:
            raise
        logger.debug("no buildkitd Deployment in %s; nothing to recover", namespace)
        return {}

    pod_spec = dep.spec.template.spec
    # The budget belongs to the same recovery: it is a `setup` flag recorded nowhere but the
    # daemon's own config, so an upgrade re-rendering from defaults would silently re-size a
    # store an operator had deliberately bounded -- on the deployment whose disk was the reason
    # they bounded it. Merged before the branches below, both of which return.
    settings = _gc_budget_from_cluster(namespace, core)

    from .node_placement import BUILD_NODE_LABEL  # pylint: disable=import-outside-toplevel
    node_selector = pod_spec.node_selector or {}
    if node_selector.get(BUILD_NODE_LABEL):
        settings["node_selector"] = {BUILD_NODE_LABEL: node_selector[BUILD_NODE_LABEL]}

    store = next((v for v in (pod_spec.volumes or []) if v.name == _STORE_VOLUME), None)
    if store is not None and store.host_path is not None:
        settings["storage_path"] = store.host_path.path or ""
        return settings

    # Everything below defends one invariant: a PVC-backed store must never be re-rendered as a
    # hostPath. `buildkitd_volume` takes the PVC branch only for a non-empty storage class, so
    # recovering an empty one -- or no identifiable store at all -- would do exactly that.
    claim = store.persistent_volume_claim if store is not None else None
    if claim is None:
        raise RuntimeError(
            f"the {BUILDKITD_NAME} Deployment in {namespace} has no identifiable "
            f"'{_STORE_VOLUME}' store (neither a hostPath nor a claim), so converging it would "
            "have to guess where its cache lives. Delete the Deployment and re-run 'vast exec "
            "cluster setup' with the --buildkit-storage-* flags this deployment wants.")

    # The class and the size live on the claim; the pod spec only names it. Both are recovered:
    # the class is what selects the PVC branch at all, and re-rendering without the size would
    # ask for a different one.
    try:
        pvc = core.read_namespaced_persistent_volume_claim(claim.claim_name, namespace)
    except client.exceptions.ApiException as e:
        if e.status != 404:
            raise
        # A claim deleted from under a running daemon. `delete_buildkitd` invites an operator to
        # delete it for the space, but only once the Deployment is gone; this state is that done
        # in the wrong order, and it leaves the pod unschedulable anyway.
        raise RuntimeError(
            f"the {BUILDKITD_NAME} Deployment in {namespace} mounts claim '{claim.claim_name}', "
            "which does not exist, so its storage class cannot be recovered. Re-run 'vast exec "
            "cluster setup' with --buildkit-storage-class to state it.") from e

    if not pvc.spec.storage_class_name:
        raise RuntimeError(
            f"claim '{claim.claim_name}' in {namespace} names no storage class, so the "
            f"{BUILDKITD_NAME} store cannot be re-rendered as the claim it is. Re-run 'vast exec "
            "cluster setup' with --buildkit-storage-class to state it.")
    settings["storage_class"] = pvc.spec.storage_class_name
    requested = (pvc.spec.resources.requests if pvc.spec.resources else None) or {}
    if requested.get("storage"):
        settings["storage_size"] = requested["storage"]
    return settings


#: ``buildkitd.toml`` GC key -> the ``apply_buildkitd`` keyword that sets it.
_GC_KEYS = {"reservedSpace": "gc_reserved", "maxUsedSpace": "gc_max_used",
            "minFreeSpace": "gc_min_free"}


def _gc_budget_from_cluster(namespace: str, core) -> dict:
    """The GC budget a deployed daemon is already running with, as ``apply_buildkitd`` kwargs.

    Parsed from the ConfigMap rather than tracked beside it, so there is one source of truth,
    and read with a real TOML parser rather than by matching text -- what is being read is the
    file the daemon itself reads.

    ``{}`` when there is no config or it cannot be understood: the caller then applies its own
    defaults, which is the right answer for a daemon that predates this setting. Unlike the
    storage recovery below, guessing wrong here costs disk rather than the whole cache, so it
    does not refuse.
    """
    import tomllib

    from kubernetes import client

    try:
        cfg = core.read_namespaced_config_map(f"{BUILDKITD_NAME}-config", namespace)
        policy = tomllib.loads(cfg.data["buildkitd.toml"])["worker"]["oci"]["gcpolicy"][0]
    except (client.exceptions.ApiException, KeyError, IndexError, TypeError,
            tomllib.TOMLDecodeError) as e:
        logger.debug("no buildkitd GC budget to recover from %s: %s", namespace, e)
        return {}
    return {kw: str(policy[key]) for key, kw in _GC_KEYS.items() if key in policy}


def delete_buildkitd(namespace: str, kube_context=None) -> bool:
    """Remove the daemon. True if there was one. **Leaves the claim.**

    Teardown deletes named objects rather than the namespace, so a Deployment left behind holds
    a pod and its reservation indefinitely for a deployment that no longer exists.

    The PVC is deliberately not deleted: it is a cache, but it is also the only copy of hours of
    pulled and unpacked layers, and ``cleanup`` is run for reasons that do not always mean "and
    throw that away". An operator who wants the space deletes the claim by name.
    """
    from kubernetes import client

    from .kube_client import load_kube_config

    load_kube_config(kube_context)
    removed = False
    core, apps = client.CoreV1Api(), client.AppsV1Api()
    for delete, name, kind in (
            (apps.delete_namespaced_deployment, BUILDKITD_NAME, "Deployment"),
            (core.delete_namespaced_service, BUILDKITD_NAME, "Service"),
            (core.delete_namespaced_config_map, f"{BUILDKITD_NAME}-config", "ConfigMap")):
        try:
            delete(name, namespace)
            removed = True
        except client.exceptions.ApiException as e:
            if e.status != 404:
                raise
            logger.debug("no buildkitd %s to remove in %s", kind, namespace)
    if removed:
        logger.info("removed the buildkitd daemon from %s", namespace)
    return removed


def buildkitd_ready(namespace: str) -> bool:
    """Is there a pod ready to serve a build?

    Asked over **pods**, not the Deployment, on purpose: the service's Role grants ``pods`` and
    no ``apps``, so this is answerable from the service without widening what it may read.

    A false here must be reported, never worked around. A build submitted to an absent daemon
    fails on a gRPC dial, and the error classifier would read that as the project's own build
    configuration being wrong -- sending whoever hit it to edit a ``.vast`` over a cluster
    fault.
    """
    from kubernetes import client

    try:
        pods = client.CoreV1Api().list_namespaced_pod(
            namespace, label_selector=f"app={BUILDKITD_NAME}")
    except Exception as e:  # noqa: BLE001 - an unreadable API is not a ready daemon
        logger.warning("Could not check whether the build daemon is ready: %s", e)
        return False
    for pod in pods.items:
        conditions = (pod.status.conditions or []) if pod.status else []
        if any(c.type == "Ready" and c.status == "True" for c in conditions):
            return True
    return False
