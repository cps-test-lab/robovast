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

from robovast.common.cli.project_config import ProjectConfig
from robovast.common.common import load_config

from .kubernetes_kueue import (apply_kueue_queues, install_kueue_helm,
                               uninstall_kueue_helm,
                               verify_kueue_admission_ready)

logger = logging.getLogger(__name__)

# Legacy ServiceAccount name, kept so `cluster setup` still reconciles/removes the
# RBAC it created before campaigns moved into the service. Nothing runs as it now:
# the service drives campaigns in-process under its own ServiceAccount.
CONTROLLER_SERVICE_ACCOUNT = "robovast-controller"


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
    from robovast.execution.cluster_execution.service_deploy import \
        SERVICE_ACCOUNT  # pylint: disable=import-outside-toplevel
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


_CONTROLLER_RBAC_NAME = "robovast-controller"


def apply_controller_rbac(namespace="default", kube_context=None):
    """Create/update the service's node-read ClusterRole + binding (idempotent).

    The namespaced permissions moved onto the service's own Role (see
    :mod:`.service_deploy`) when campaigns stopped running in their own pod; only
    the cluster-scoped node access is applied here.
    """
    from kubernetes import client, config  # pylint: disable=import-outside-toplevel
    from kubernetes.client.rest import \
        ApiException  # pylint: disable=import-outside-toplevel

    from robovast.execution.cluster_execution.service_deploy import \
        SERVICE_ACCOUNT  # pylint: disable=import-outside-toplevel

    config.load_kube_config(context=kube_context)
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
    """Remove the node ClusterRole/binding **and** the legacy controller RBAC.

    The ``robovast-controller`` ServiceAccount/Role/RoleBinding are no longer
    created (campaigns run inside the service), but they linger on clusters set up
    before that change — so this still deletes them to leave nothing behind.
    Best-effort throughout.
    """
    from kubernetes import client, config  # pylint: disable=import-outside-toplevel
    from kubernetes.client.rest import \
        ApiException  # pylint: disable=import-outside-toplevel

    try:
        config.load_kube_config(context=kube_context)
    except Exception as exc:  # pragma: no cover - best-effort cleanup
        logger.warning("Failed to load kube config for RBAC cleanup: %s", exc)
        return
    core = client.CoreV1Api()
    rbac = client.RbacAuthorizationV1Api()
    cluster_role_name = _controller_cluster_role_name(namespace)
    deletions = [
        ("ClusterRoleBinding", lambda: rbac.delete_cluster_role_binding(cluster_role_name)),
        ("ClusterRole", lambda: rbac.delete_cluster_role(cluster_role_name)),
        ("RoleBinding", lambda: rbac.delete_namespaced_role_binding(_CONTROLLER_RBAC_NAME, namespace)),
        ("Role", lambda: rbac.delete_namespaced_role(_CONTROLLER_RBAC_NAME, namespace)),
        ("ServiceAccount", lambda: core.delete_namespaced_service_account(CONTROLLER_SERVICE_ACCOUNT, namespace)),
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
        config_path: Path to the ``.vast`` config file.  When ``None`` the
            active project config is used.

    Returns:
        tuple: ``(jobs_node_labels, control_node_labels)`` — each is a ``dict``
            or ``None`` when not configured.
    """
    if config_path is None:
        pc = ProjectConfig.load()
        if pc is None or not getattr(pc, 'config_path', None):
            return None, None
        config_path = pc.config_path
    try:
        execution = load_config(config_path, subsection="execution", allow_missing=True)
    except Exception:
        return None, None
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
    ``setup`` (``cleanup``, ``prepare-run``, off-cluster ``serve --backend
    cluster``). It reads the config name + setup kwargs from the in-cluster
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
    from robovast.execution.cluster_execution.service_deploy import \
        read_service_config_from_cluster
    name, setup_kwargs = read_service_config_from_cluster(namespace, context_key)
    if name is None:
        return None
    cfg = get_cluster_config(name)
    if cfg is not None and setup_kwargs:
        cfg.restore_from_setup_kwargs(setup_kwargs)
    return cfg


def setup_server(config_name=None, list_configs=False, force=False, **cluster_kwargs):
    """Set up transfer mechanism for cluster execution.

    Args:
        config_name (str, optional): Name of the cluster config plugin to use
        list_configs (bool): If True, list available configs and exit
        **cluster_kwargs: Cluster-specific options to pass to setup_cluster()

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

    from robovast.execution.cluster_execution.service_deploy import \
        read_service_config_from_cluster
    existing_config, _ = read_service_config_from_cluster(namespace, kube_context)
    if existing_config and not force:
        key_label = f" for context '{context_key}'" if context_key else ""
        raise RuntimeError(
            f"Cluster is already set up with '{existing_config}' config{key_label}.\n"
            f"Run 'vast execution cluster cleanup' first to clean up the existing setup."
        )

    cluster_config = get_cluster_config(config_name)

    # Read node labels from the vast config
    jobs_node_labels, control_node_labels = get_kubernetes_node_labels_from_config()
    if jobs_node_labels:
        logger.info("Job node labels (ResourceFlavor): %s", jobs_node_labels)
    if control_node_labels:
        logger.info("Control pod node labels (nodeSelector): %s", control_node_labels)

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
    # that contains the `robovast.service` package + `vast serve` (plan 0.7);
    # override with ROBOVAST_CONTROLLER_IMAGE to point at a current dev image.
    # The Deployment env carries config_name + cluster_kwargs, which is now the
    # single source of truth for every later command (read back via
    # read_service_config_from_cluster) — no local flag file to write.
    from robovast.execution.cluster_execution.service_deploy import deploy_service
    deploy_service(namespace=namespace, kube_context=kube_context,
                   config_name=config_name, config_kwargs=cluster_kwargs)
    logger.debug("Cluster config '%s' recorded in the robovast-service Deployment.",
                 config_name)


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
        from robovast.execution.cluster_execution.service_deploy import \
            read_service_config_from_cluster
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

    # Remove the persistent robovast-service (Deployment + Service + RBAC).
    # Never touches the object store (the durable data home).
    from robovast.execution.cluster_execution.service_deploy import delete_service
    delete_service(namespace=namespace, kube_context=kube_context)

    # Remove the controller RBAC created at setup.
    delete_controller_rbac(namespace=namespace, kube_context=kube_context)

    # Uninstall Kueue (always, since we always install it)
    uninstall_kueue_helm(kube_context=kube_context)

    cluster_config = get_cluster_config(config_name)
    cluster_config.cleanup_cluster(kube_context=kube_context, **cluster_kwargs)
