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

"""Setup utilities for cluster execution."""

import logging
from importlib.metadata import entry_points

from robovast.client.project_config import get_vast_file_override
from robovast.common.common import load_config

from .kubernetes_gpu import ensure_nvidia_device_plugin, uninstall_nvidia_device_plugin
from .kubernetes_kueue import (apply_kueue_queues, install_kueue_helm, uninstall_kueue_helm,
                               verify_kueue_admission_ready)

logger = logging.getLogger(__name__)

def _controller_cluster_role_name(namespace):
    """Name for the cluster-scoped controller RBAC objects.

    ClusterRole/ClusterRoleBinding are not namespaced, so the namespace is
    folded into the name to let controller setups in different namespaces
    coexist without clobbering each other.
    """
    return f"robovast-controller-nodes-{namespace}"


def _controller_rbac_manifests(namespace):
    """Cluster-scoped node access for the **robovast-service**.

    Campaigns are driven in-process by the service now, so the namespaced
    permissions that used to live here (jobs, pods, pods/exec) moved onto the
    service's own Role in :mod:`.service_deploy` together with its ServiceAccount.
    What remains is the read-only **ClusterRole** for node metadata (count/labels/
    CPU-manager policy) that enriches ``execution.yaml``: nodes are cluster-scoped,
    so they cannot live in that namespaced Role — and it is now bound to the
    service's ServiceAccount, which is what does the reading.

    The legacy ``robovast-controller`` ServiceAccount/Role are no longer created;
    :func:`delete_controller_rbac` removes them from clusters set up earlier.
    """
    from .service_deploy import SERVICE_ACCOUNT  # pylint: disable=import-outside-toplevel
    cluster_role_name = _controller_cluster_role_name(namespace)
    return [
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": {"name": cluster_role_name},
            "rules": [
                {"apiGroups": [""], "resources": ["nodes"],
                 "verbs": ["get", "list"]},
                # connect_get_node_proxy_with_path("configz") reads the kubelet
                # config via the nodes/proxy subresource.
                {"apiGroups": [""], "resources": ["nodes/proxy"],
                 "verbs": ["get"]},
                # Whether a `nvidia` RuntimeClass exists decides whether a GPU pod gets
                # `runtimeClassName` -- and on a cluster where nvidia is a registered
                # runtime rather than the default one, that field is the whole difference
                # between a usable GPU and a device the container cannot render on. Also
                # cluster-scoped, so it belongs here rather than on the namespaced Role.
                # Without it the check reads "no such RuntimeClass" for a 403 and the pod
                # loses the field silently, which is how this was found.
                {"apiGroups": ["node.k8s.io"], "resources": ["runtimeclasses"],
                 "verbs": ["get", "list"]},
            ],
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRoleBinding",
            "metadata": {"name": cluster_role_name},
            "subjects": [{"kind": "ServiceAccount", "name": SERVICE_ACCOUNT,
                          "namespace": namespace}],
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole",
                        "name": cluster_role_name},
        },
    ]


def apply_controller_rbac(namespace="default", kube_context=None):
    """Create/update the service's node-read ClusterRole + binding (idempotent).

    The namespaced permissions moved onto the service's own Role (see
    :mod:`.service_deploy`) when campaigns stopped running in their own pod; only
    the cluster-scoped node access is applied here.
    """
    from kubernetes import client  # pylint: disable=import-outside-toplevel
    from kubernetes.client.rest import ApiException  # pylint: disable=import-outside-toplevel

    from .kube_client import load_kube_config  # pylint: disable=import-outside-toplevel
    from .service_deploy import SERVICE_ACCOUNT  # pylint: disable=import-outside-toplevel

    load_kube_config(context=kube_context)
    rbac = client.RbacAuthorizationV1Api()
    cluster_role, cluster_binding = _controller_rbac_manifests(namespace)

    # ClusterRole (read-only node access) — create, or replace its rules.
    try:
        rbac.create_cluster_role(cluster_role)
    except ApiException as exc:
        if exc.status != 409:
            raise
        rbac.patch_cluster_role(cluster_role["metadata"]["name"], {"rules": cluster_role["rules"]})

    # ClusterRoleBinding — create, or replace its subjects (so a binding created
    # for the old controller ServiceAccount is repointed at the service).
    try:
        rbac.create_cluster_role_binding(cluster_binding)
    except ApiException as exc:
        if exc.status != 409:
            raise
        rbac.patch_cluster_role_binding(
            cluster_binding["metadata"]["name"],
            {"subjects": cluster_binding["subjects"],
             "roleRef": cluster_binding["roleRef"]})
    logger.debug("Applied node-read RBAC (ServiceAccount %s) in namespace %s",
                 SERVICE_ACCOUNT, namespace)


def delete_controller_rbac(namespace="default", kube_context=None):
    """Remove the service's node-read ClusterRole and its binding. Best-effort."""
    from kubernetes import client  # pylint: disable=import-outside-toplevel
    from kubernetes.client.rest import ApiException  # pylint: disable=import-outside-toplevel

    from .kube_client import load_kube_config  # pylint: disable=import-outside-toplevel

    try:
        load_kube_config(context=kube_context)
    except Exception as exc:  # pragma: no cover - best-effort cleanup
        logger.warning("Failed to load kube config for RBAC cleanup: %s", exc)
        return
    rbac = client.RbacAuthorizationV1Api()
    cluster_role_name = _controller_cluster_role_name(namespace)
    deletions = [
        ("ClusterRoleBinding", lambda: rbac.delete_cluster_role_binding(cluster_role_name)),
        ("ClusterRole", lambda: rbac.delete_cluster_role(cluster_role_name)),
    ]
    for kind, call in deletions:
        try:
            call()
        except ApiException as exc:
            if exc.status != 404:
                logger.warning("Failed to delete controller %s: %s", kind, exc)
        except Exception as exc:  # pragma: no cover - best-effort cleanup
            logger.warning("Failed to delete controller %s: %s", kind, exc)


def get_kubernetes_node_labels_from_config(config_path=None):
    """Read job and control pod node labels from the vast config.

    Reads from::

        execution:
          kubernetes:
            jobs:
              node_labels:
                <key>: <value>   # applied to ResourceFlavor (Kueue job scheduling)
            control:
              node_labels:
                <key>: <value>   # applied as nodeSelector to the robovast control pod

    Args:
        config_path: Path to the ``.vast`` config file, or ``None`` when nothing named
            one — then no labels apply. This function never *discovers* a config: which
            one to read is the caller's decision, because for a cluster-wide deploy the
            answer is "only one that was named explicitly" (see :func:`setup_server`).

    Returns:
        tuple: ``(jobs_node_labels, control_node_labels)`` — each is a ``dict``
            or ``None`` when not configured.

    Raises:
        ValueError: If the named config cannot be read. Falling back to "no labels"
            would schedule pods on arbitrary nodes while the command reported success.
    """
    if config_path is None:
        logger.info(
            "No .vast config named ('vast -V <file>') — no node labels applied. Pass "
            "'vast -V <file>' to pin pods to a node pool via "
            "execution.kubernetes.{jobs,control}.node_labels.")
        return None, None
    try:
        execution = load_config(config_path, subsection="execution", allow_missing=True)
    except Exception as exc:
        # Deploying with "no labels" because the config was unreadable would put job
        # and control pods on arbitrary nodes for the cluster's whole lifetime, while
        # setup reported success — the failure has to surface here.
        raise ValueError(
            f"could not read node labels from '{config_path}': {exc}\n"
            "Fix that .vast, or name another one with 'vast -V <file>'. Cluster setup "
            "needs no config at all — drop the '-V' to deploy with no node selectors."
        ) from exc
    k8s = (execution or {}).get("kubernetes") or {}
    jobs_labels = (k8s.get("jobs") or {}).get("node_labels") or None
    control_labels = (k8s.get("control") or {}).get("node_labels") or None
    # Normalise: must be a plain dict or None
    if jobs_labels and not isinstance(jobs_labels, dict):
        logger.warning("execution.kubernetes.jobs.node_labels is not a mapping — ignoring")
        jobs_labels = None
    if control_labels and not isinstance(control_labels, dict):
        logger.warning("execution.kubernetes.control.node_labels is not a mapping — ignoring")
        control_labels = None
    return jobs_labels, control_labels


def load_cluster_config_plugins():
    """Load all available cluster config plugins from entry points.

    Returns:
        dict: Dictionary mapping plugin names to their class objects
    """
    plugins = {}
    try:
        eps = entry_points(group='robovast.cluster_configs')
        for ep in eps:
            try:
                plugin_class = ep.load()
                plugins[ep.name] = plugin_class
            except Exception as e:
                logger.warning(f"Failed to load cluster config plugin '{ep.name}': {e}")
    except Exception as e:
        logger.warning(f"Failed to load cluster config plugins: {e}")

    return plugins


def get_cluster_config(config_name):
    """Get a cluster configuration instance by name.

    Args:
        config_name: Name of the cluster config plugin to use.

    Returns:
        BaseConfig: Instance of the selected cluster configuration class

    Raises:
        ValueError: If config_name is not found in available plugins
    """
    if config_name is None:
        return None

    plugins = load_cluster_config_plugins()

    if config_name not in plugins:
        available = ", ".join(plugins.keys()) if plugins else "none"
        raise ValueError(
            f"Cluster config '{config_name}' not found. "
            f"Available configs: {available}"
        )

    # Instantiate and return the config class
    return plugins[config_name]()


def get_cluster_config_for_context(context_key=None, namespace="default"):
    """Get a cluster config instance, reconstructed **from the deployed service**.

    This is the way to obtain a config object for commands that run *after*
    ``setup`` (``cleanup``, off-cluster ``serve --backend cluster``). It reads the config name + setup kwargs from the in-cluster
    ``robovast-service`` Deployment's env — the authoritative record setup wrote
    there — and calls :meth:`~BaseConfig.restore_from_setup_kwargs` so that
    credential-dependent methods such as :meth:`~BaseConfig.get_s3_credentials`
    work without the user re-passing ``-o`` flags, and **from any host** (no local
    flag file). Bucket cleanup no longer uses this — it runs server-side.

    Args:
        context_key (str | None): Kubernetes context; ``None`` uses the active one.
        namespace (str): Namespace the service Deployment runs in.

    Returns:
        BaseConfig: Configured cluster config instance, or ``None`` if the service
            is not deployed in that context/namespace.

    Raises:
        ValueError: If the stored config name is not found in the available plugins.
    """
    from .service_deploy import read_service_config_from_cluster
    name, setup_kwargs = read_service_config_from_cluster(namespace, context_key)
    if name is None:
        return None
    cfg = get_cluster_config(name)
    if cfg is not None and setup_kwargs:
        cfg.restore_from_setup_kwargs(setup_kwargs)
    return cfg


def setup_server(config_name=None, list_configs=False, force=False,
                 service_kwargs=None, gpu_replicas=None, no_gpu=False, **cluster_kwargs):
    """Set up transfer mechanism for cluster execution.

    Args:
        config_name (str, optional): Name of the cluster config plugin to use
        list_configs (bool): If True, list available configs and exit
        gpu_replicas (int, optional): Time-slicing replicas to advertise per physical GPU.
            ``None`` provisions GPUs opportunistically (the default) and never fails over
            a cluster that has none; a value makes GPU support an explicit requirement.
        no_gpu (bool): Skip GPU provisioning entirely.
        **cluster_kwargs: Cluster-specific options to pass to setup_cluster()

    Named parameters rather than ``cluster_kwargs`` entries on purpose: ``cluster_kwargs``
    is the ``-o key=value`` channel, splatted into the provider's ``setup_cluster`` and
    persisted as the cluster's recorded config. GPU provisioning is neither
    provider-specific nor worth recording -- the node's advertised capacity is the record,
    and unlike a stored number it cannot go stale.

    Returns:
        None

    Raises:
        RuntimeError: If cluster is already set up
    """
    if list_configs:
        plugins = load_cluster_config_plugins()
        if plugins:
            logger.info("Available cluster configurations:")
            for name in sorted(plugins.keys()):
                logger.info(f"  - {name}")
        else:
            logger.info("No cluster configurations available.")
        return

    if config_name is None:
        raise ValueError(
            "No cluster config specified. Use --config <name> to select a config, "
            "or --list to see available configs."
        )

    # Check if cluster is already set up — the deployed service's env is the
    # record (no local flag file), so this is correct even from another host.
    kube_context = cluster_kwargs.pop('kube_context', None)
    context_key = kube_context
    namespace = cluster_kwargs.get("namespace", "default")

    from .service_deploy import read_service_config_from_cluster
    existing_config, _ = read_service_config_from_cluster(namespace, kube_context)
    if existing_config and not force:
        key_label = f" for context '{context_key}'" if context_key else ""
        # Point at `upgrade` first, because it is what almost everyone reaching this
        # message actually wants. `setup` *provisions*: it re-runs the Kueue install, the
        # object store and the registry storage, and it takes its ingress/registry/storage
        # options as arguments -- so a re-run without the flags of the original run
        # re-provisions with different ones. `upgrade` reads those back from the live
        # cluster and touches only the image, RBAC and the env-derived Secrets.
        raise RuntimeError(
            f"Cluster is already set up with '{existing_config}' config{key_label}.\n"
            "To roll out a new image or refresh credentials, use:\n"
            "    vast exec cluster upgrade\n"
            "It recovers this cluster's own settings and cannot lose them, which a\n"
            "re-run of `setup` can: setup takes --ingress-host/--registry-* as\n"
            "arguments and re-provisions with whatever it is given this time.\n"
            "To genuinely re-provision, pass --force (supply the original flags too),\n"
            "or 'vast exec cluster cleanup' first to start from nothing."
        )

    # Before anything is installed or deployed. A refused Ingress combination is a pure
    # argument error, and it used to be raised inside deploy_service -- after Kueue was
    # installed and the flavor's storage deployed, leaving a half-set-up cluster behind
    # for a mistake that costs nothing to catch here.
    from .service_deploy import validate_ingress_options  # pylint: disable=import-outside-toplevel
    validate_ingress_options(**{k: v for k, v in (service_kwargs or {}).items()
                                if k in ("ingress_host", "tls_secret", "issuer",
                                         "insecure_http")})

    if no_gpu and gpu_replicas is not None:
        raise ValueError("--no-gpu and --gpu-replicas are contradictory; pass one.")
    if gpu_replicas is not None and gpu_replicas < 1:
        raise ValueError("--gpu-replicas must be at least 1 (use --no-gpu to skip GPUs).")

    cluster_config = get_cluster_config(config_name)

    # Node labels are the only thing this deploy reads from a .vast, and it reads them
    # ONLY from a config the operator named with 'vast -V <file>'. Never from an ambient
    # project: a .robovast_project is discovered by walking up to the filesystem root,
    # so a project one directory — or ten — above a CWD that has nothing to do with
    # this cluster would otherwise decide which nodes its pods may run on, or (via a
    # stale pointer) fail the deploy over a file the operator never mentioned.
    jobs_node_labels, control_node_labels = get_kubernetes_node_labels_from_config(
        get_vast_file_override())
    if jobs_node_labels:
        logger.info("Job node labels (ResourceFlavor): %s", jobs_node_labels)
    if control_node_labels:
        logger.info("Control pod node labels (nodeSelector): %s", control_node_labels)

    # BEFORE Kueue, and the order is load-bearing: `apply_kueue_queues` sizes the
    # ClusterQueue's GPU quota from what the nodes advertise, and a node advertises nothing
    # until this DaemonSet is running. Install it afterwards and the quota is sized from
    # zero GPUs by construction -- which Kueue answers by suspending every GPU job forever
    # rather than failing, so the campaign hangs and setup reported success.
    #
    # Going first rather than between the two also means Kueue's own install and its
    # rollout wait overlap the plugin registering, so the capacity check below usually
    # returns on its first poll.
    ensure_nvidia_device_plugin(kube_context=kube_context, gpu_replicas=gpu_replicas,
                                skip=no_gpu)

    # Install Kueue and queues first (always)
    install_kueue_helm(kube_context=kube_context)
    apply_kueue_queues(namespace=namespace, kube_context=kube_context,
                       node_labels=jobs_node_labels, cluster_config=cluster_config)
    # Post-condition: `kubectl apply` reporting success is not proof that the queues are
    # usable — a ClusterQueue whose ResourceFlavor is missing stays Active=False, and
    # setup would otherwise finish "successfully" while every future job hangs suspended.
    verify_kueue_admission_ready(namespace=namespace, kube_context=kube_context,
                                 settle_timeout=60)

    # RBAC for the in-cluster search controller pod (create/monitor jobs).
    apply_controller_rbac(namespace=namespace, kube_context=kube_context)

    cluster_config.setup_cluster(
        kube_context=kube_context,
        control_node_labels=control_node_labels,
        **cluster_kwargs,
    )

    # Deploy the persistent robovast-service (Deployment + ClusterIP Service +
    # its own RBAC) so clients drive campaigns over HTTP (mode 3), reached via
    # `kubectl port-forward svc/robovast-service`. Requires a controller image
    # that contains the `robovast.service` package + `vast serve`; set
    # ROBOVAST_PROJECT to run the family from your own registry.
    # The Deployment env carries config_name + cluster_kwargs, which is now the
    # single source of truth for every later command (read back via
    # read_service_config_from_cluster) — no local flag file to write.
    from .service_deploy import deploy_service, published_host, wait_for_service_ready
    service_kwargs = dict(service_kwargs or {})
    # Keep the registry prefix a re-run cannot drop. It is baked from the Ingress host,
    # so a `setup` without --ingress-host used to make `_registry_env` return None: the
    # Secret went unlisted from the Deployment's envFrom, the pod lost the prefix, and
    # in-cluster builds became impossible -- while the Ingress itself was untouched, so
    # nothing looked wrong until a campaign was submitted and refused.
    #
    # `deploy_service` separates registry_host from ingress_host precisely so a caller
    # can re-bake the prefix without rebuilding the Ingress; `upgrade` already used that
    # and `setup` did not. Recovering the host from the live Ingress makes the two agree.
    if "registry_host" not in service_kwargs:
        host = service_kwargs.get("ingress_host")
        if not host:
            # Only now, and only tolerantly: this dials the API server, and setup must
            # not hang or die because it could not *look up* something it is merely
            # trying to preserve. No answer means there is nothing to preserve.
            try:
                host = published_host(namespace, kube_context)
            except Exception:  # noqa: BLE001 - unreachable, unpublished, or no RBAC
                host = ""
        if host:
            service_kwargs["registry_host"] = host
    deploy_service(namespace=namespace, kube_context=kube_context,
                   config_name=config_name, config_kwargs=cluster_kwargs,
                   **service_kwargs)
    logger.debug("Cluster config '%s' recorded in the robovast-service Deployment.",
                 config_name)
    # Only now is the cluster actually set up. Returning at "Deployment created"
    # reported success for a pod that may never start.
    wait_for_service_ready(namespace=namespace, kube_context=kube_context)
    # A fresh deployment is maximally cold: no node holds any family image, so the first
    # campaign would pay a full pull of robovast-roqsim before running anything. Last,
    # and fire-and-forget, because setup has already succeeded by this point.
    from .image_warm import warm_family_images  # pylint: disable=import-outside-toplevel
    warm_family_images(namespace, kube_context)


def delete_server(config_name=None, **cluster_kwargs_override):
    """Clean up transfer mechanism for cluster execution.

    Args:
        config_name (str, optional): Name of the cluster config plugin to use.
                                     If not provided, will auto-detect from flag file.
        **cluster_kwargs_override: Optional kwargs to pass to cleanup_cluster (e.g. namespace).
                                   When auto-detecting, these override stored kwargs.
                                   When config_name is given, these are the only kwargs used.

    Returns:
        None
    """
    cluster_kwargs = {}

    # Auto-detect config from the deployed service (read before we tear it down);
    # the cluster is the source of truth, so this works from any host.
    kube_context = cluster_kwargs_override.get('kube_context')

    if config_name is None:
        from .service_deploy import read_service_config_from_cluster
        ns = cluster_kwargs_override.get("namespace", "default")
        name, stored_kwargs = read_service_config_from_cluster(ns, kube_context)
        config_name = name

        # Use stored kwargs for cleanup; CLI overrides take precedence
        if stored_kwargs:
            cluster_kwargs = dict(stored_kwargs)
        if cluster_kwargs_override:
            cluster_kwargs.update(cluster_kwargs_override)

        if config_name:
            logger.debug(f"Auto-detected cluster config: {config_name}")
        else:
            raise ValueError(
                "No cluster config specified and no deployed service found to read "
                "it from. Use --cluster-config <name> (with -o for credentials), or "
                "check the context/namespace."
            )
    else:
        # Explicit config: use only CLI-provided kwargs (e.g. -n namespace)
        cluster_kwargs = dict(cluster_kwargs_override)

    # Clean up scenario run jobs and pods first (before uninstalling Kueue,
    # so the Kueue controller is still running to handle job finalizer removal)
    namespace = cluster_kwargs.get("namespace", "default")
    kube_context = cluster_kwargs.pop("kube_context", None)
    try:
        from .cluster_execution import \
            cleanup_cluster_campaign  # pylint: disable=import-outside-toplevel,cyclic-import
        cleanup_cluster_campaign(namespace=namespace, context=kube_context)
    except Exception as e:
        logger.warning(f"Failed to clean up scenario run jobs during cluster cleanup: {e}")

    # Before the service, and unconditionally: teardown deletes named objects rather than the
    # namespace, so an image-warm DaemonSet left behind keeps a pod on every node indefinitely,
    # holding multi-GB images for a deployment that no longer exists.
    from .image_warm import delete_warm_daemonset  # pylint: disable=import-outside-toplevel
    delete_warm_daemonset(namespace, kube_context)

    # Remove the persistent robovast-service (Deployment + Service + RBAC).
    # Never touches the object store (the durable data home).
    from .service_deploy import delete_service
    delete_service(namespace=namespace, kube_context=kube_context)

    # Remove the controller RBAC created at setup.
    delete_controller_rbac(namespace=namespace, kube_context=kube_context)

    # Uninstall Kueue (always, since we always install it)
    uninstall_kueue_helm(kube_context=kube_context)

    # After Kueue, mirroring the setup order. The campaign jobs are already gone by this
    # point, so no pod still holds a GPU allocation when its advertiser disappears. A no-op
    # on every cluster that never had the plugin, which is what keeps teardown unchanged
    # for a CPU-only cluster.
    uninstall_nvidia_device_plugin(kube_context=kube_context)

    cluster_config = get_cluster_config(config_name)
    cluster_config.cleanup_cluster(kube_context=kube_context, **cluster_kwargs)
