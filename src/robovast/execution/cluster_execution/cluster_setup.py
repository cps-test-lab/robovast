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
import os
import re
from importlib.metadata import entry_points

import yaml

from robovast.common.cli.project_config import ProjectConfig
from robovast.common.common import load_config

from .kubernetes_kueue import (apply_kueue_queues, install_kueue_helm,
                               uninstall_kueue_helm)

logger = logging.getLogger(__name__)

# Flag file name to store the cluster config name that was used for setup
CLUSTER_CONFIG_FLAG_FILE = ".robovast_cluster_config"

# ServiceAccount the in-cluster controller pod runs as (search campaigns). Must
# match controller_launcher.CONTROLLER_SERVICE_ACCOUNT.
CONTROLLER_SERVICE_ACCOUNT = "robovast-controller"


def _controller_cluster_role_name(namespace):
    """Name for the cluster-scoped controller RBAC objects.

    ClusterRole/ClusterRoleBinding are not namespaced, so the namespace is
    folded into the name to let controller setups in different namespaces
    coexist without clobbering each other.
    """
    return f"robovast-controller-nodes-{namespace}"


def _controller_rbac_manifests(namespace):
    """ServiceAccount + (Cluster)Role + (Cluster)RoleBinding for the controller pod.

    The in-cluster controller (search) creates/monitors/deletes scenario Jobs and
    reads their pods/logs in its own namespace (namespaced Role).  It also reads
    node metadata (count/labels/CPU-manager policy) to enrich ``execution.yaml``;
    nodes are cluster-scoped, so that needs a read-only ClusterRole.
    """
    role_name = "robovast-controller"
    cluster_role_name = _controller_cluster_role_name(namespace)
    return [
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {"name": CONTROLLER_SERVICE_ACCOUNT, "namespace": namespace},
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": {"name": role_name, "namespace": namespace},
            "rules": [
                {"apiGroups": ["batch"], "resources": ["jobs"],
                 "verbs": ["create", "get", "list", "watch", "delete", "deletecollection"]},
                # read_namespaced_job_status hits the jobs/status subresource,
                # which is a distinct RBAC resource from jobs.
                {"apiGroups": ["batch"], "resources": ["jobs/status"],
                 "verbs": ["get", "list", "watch"]},
                {"apiGroups": [""], "resources": ["pods", "pods/log"],
                 "verbs": ["get", "list", "watch", "delete", "deletecollection"]},
                # Variations that declare an auxiliary container run their commands
                # in a controller-pod sidecar via the pods/exec subresource
                # (see cluster_execution.container_runner.ClusterContainerRunner).
                {"apiGroups": [""], "resources": ["pods/exec"],
                 "verbs": ["create", "get"]},
            ],
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": {"name": role_name, "namespace": namespace},
            "subjects": [{"kind": "ServiceAccount", "name": CONTROLLER_SERVICE_ACCOUNT,
                          "namespace": namespace}],
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role",
                        "name": role_name},
        },
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
            "subjects": [{"kind": "ServiceAccount", "name": CONTROLLER_SERVICE_ACCOUNT,
                          "namespace": namespace}],
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole",
                        "name": cluster_role_name},
        },
    ]


_CONTROLLER_RBAC_NAME = "robovast-controller"


def apply_controller_rbac(namespace="default", kube_context=None):
    """Create/update the controller ServiceAccount + Role/RoleBinding (idempotent)."""
    from kubernetes import client, config  # pylint: disable=import-outside-toplevel
    from kubernetes.client.rest import \
        ApiException  # pylint: disable=import-outside-toplevel

    config.load_kube_config(context=kube_context)
    core = client.CoreV1Api()
    rbac = client.RbacAuthorizationV1Api()
    sa, role, binding, cluster_role, cluster_binding = _controller_rbac_manifests(namespace)

    # ServiceAccount — create, tolerate existing.
    try:
        core.create_namespaced_service_account(namespace, sa)
    except ApiException as exc:
        if exc.status != 409:
            raise

    # Role — create, or replace its rules if it exists (so verb changes apply).
    try:
        rbac.create_namespaced_role(namespace, role)
    except ApiException as exc:
        if exc.status != 409:
            raise
        rbac.patch_namespaced_role(_CONTROLLER_RBAC_NAME, namespace, {"rules": role["rules"]})

    # RoleBinding — create, tolerate existing.
    try:
        rbac.create_namespaced_role_binding(namespace, binding)
    except ApiException as exc:
        if exc.status != 409:
            raise

    # ClusterRole (read-only node access) — create, or replace its rules.
    try:
        rbac.create_cluster_role(cluster_role)
    except ApiException as exc:
        if exc.status != 409:
            raise
        rbac.patch_cluster_role(cluster_role["metadata"]["name"], {"rules": cluster_role["rules"]})

    # ClusterRoleBinding — create, tolerate existing.
    try:
        rbac.create_cluster_role_binding(cluster_binding)
    except ApiException as exc:
        if exc.status != 409:
            raise
    logger.debug("Applied controller RBAC (ServiceAccount %s) in namespace %s",
                 CONTROLLER_SERVICE_ACCOUNT, namespace)


def delete_controller_rbac(namespace="default", kube_context=None):
    """Remove the controller ServiceAccount + Role/RoleBinding (best-effort)."""
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


def _sanitize_context_key(key: str) -> str:
    """Sanitize a context key so it can be used safely as a filename suffix."""
    return re.sub(r'[^A-Za-z0-9_-]', '_', key)


def get_cluster_config_flag_path(context_key=None):
    """Get the path to the cluster config flag file.

    When *context_key* is given the file is named
    ``.robovast_cluster_config.<key>`` so separate setups for different
    clusters coexist.  When it is ``None`` the legacy single-file name
    ``.robovast_cluster_config`` is used.

    The flag file is stored in the same directory as the project file.

    Returns:
        str: Path to the cluster config flag file

    Raises:
        RuntimeError: If no project file is found
    """
    project_file = ProjectConfig.find_project_file()
    if not project_file:
        raise RuntimeError(
            "Project not initialized. Run 'vast init <config-file>' first."
        )

    project_dir = os.path.dirname(project_file)
    if context_key:
        filename = f"{CLUSTER_CONFIG_FLAG_FILE}.{_sanitize_context_key(context_key)}"
    else:
        filename = CLUSTER_CONFIG_FLAG_FILE
    return os.path.join(project_dir, filename)


def save_cluster_setup_info(config_name, setup_kwargs, context_key=None):
    """Save the cluster setup info to a flag file.

    Args:
        config_name (str): Name of the cluster config plugin used for setup
        setup_kwargs (dict): Arguments passed to the setup function
        context_key (str, optional): Kubernetes context name for the flag file.
    """
    flag_path = get_cluster_config_flag_path(context_key)
    data = {
        "name": config_name,
        "kwargs": setup_kwargs
    }
    with open(flag_path, 'w') as f:
        yaml.dump(data, f, default_flow_style=False)


def load_cluster_setup_info(context_key=None):
    """Load the cluster setup info from the flag file.

    Args:
        context_key (str, optional): Kubernetes context name.

    Returns:
        tuple: (config_name, setup_kwargs)
    """
    try:
        flag_path = get_cluster_config_flag_path(context_key)
        if os.path.exists(flag_path):
            with open(flag_path, 'r') as f:
                content = f.read().strip()
                try:
                    data = yaml.safe_load(content)
                    if isinstance(data, dict):
                        return data.get("name"), data.get("kwargs", {})
                    # Backward compatibility handling (if it's just the name string)
                    return str(data) if data else None, {}
                except yaml.YAMLError:
                    # Backward compatibility fallback
                    return content, {}
    except RuntimeError:
        # Project not initialized
        pass
    return None, {}


def load_cluster_config_name(context_key=None):
    """Load the cluster config name from the flag file.

    Args:
        context_key (str, optional): Kubernetes context name.

    Returns:
        str: Name of the cluster config plugin, or None if file doesn't exist
    """
    name, _ = load_cluster_setup_info(context_key)
    return name


def get_cluster_namespace(context_key=None):
    """Load the cluster namespace from the setup flag file.

    Args:
        context_key (str, optional): Kubernetes context name.

    Returns:
        str: Kubernetes namespace for cluster execution, or "default" if not set
    """
    _, kwargs = load_cluster_setup_info(context_key)
    return kwargs.get("namespace", "default")


def delete_cluster_config_flag(context_key=None):
    """Delete the cluster config flag file.

    Args:
        context_key (str, optional): Kubernetes context name.
    """
    try:
        flag_path = get_cluster_config_flag_path(context_key)
        if os.path.exists(flag_path):
            os.remove(flag_path)
    except RuntimeError:
        # Project not initialized, nothing to delete
        pass


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


def get_cluster_config_for_context(context_key=None):
    """Get a cluster config instance with setup kwargs restored from the flag file.

    This is the preferred way to obtain a config object for all commands that
    run *after* ``setup`` (e.g. ``run``, ``download``, ``upload-to-share``).
    It loads both the config name and the persisted kwargs from the cluster
    flag file and calls :meth:`~BaseConfig.restore_from_setup_kwargs` on the
    newly created instance so that credential-dependent methods such as
    :meth:`~BaseConfig.get_s3_credentials` work correctly without the user
    having to pass ``-o`` flags again.

    Args:
        context_key (str | None): Kubernetes context name used to look up the
            per-context flag file.  ``None`` uses the legacy single-file path.

    Returns:
        BaseConfig: Configured cluster config instance, or ``None`` if no flag
            file exists for the given context.

    Raises:
        ValueError: If the stored config name is not found in the available plugins.
    """
    name, setup_kwargs = load_cluster_setup_info(context_key)
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

    # Check if cluster is already set up
    kube_context = cluster_kwargs.pop('kube_context', None)
    context_key = kube_context

    existing_config = load_cluster_config_name(context_key)
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
    namespace = cluster_kwargs.get("namespace", "default")
    install_kueue_helm(kube_context=kube_context)
    apply_kueue_queues(namespace=namespace, kube_context=kube_context,
                       node_labels=jobs_node_labels, cluster_config=cluster_config)

    # RBAC for the in-cluster search controller pod (create/monitor jobs).
    apply_controller_rbac(namespace=namespace, kube_context=kube_context)

    cluster_config.setup_cluster(
        kube_context=kube_context,
        control_node_labels=control_node_labels,
        **cluster_kwargs,
    )

    # Save the config name and kwargs to flag file after successful setup
    flag_path = get_cluster_config_flag_path(context_key)
    save_cluster_setup_info(config_name, cluster_kwargs, context_key)
    logger.debug(f"Cluster config '{config_name}' saved to {flag_path}")


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

    # Auto-detect config from flag file if not provided
    kube_context = cluster_kwargs_override.get('kube_context')
    context_key = kube_context

    if config_name is None:
        name, stored_kwargs = load_cluster_setup_info(context_key)
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
                "No cluster config specified and no saved config found. "
                "Use --cluster-config <name> to select a config, or run setup first."
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

    # Remove the controller RBAC created at setup.
    delete_controller_rbac(namespace=namespace, kube_context=kube_context)

    # Uninstall Kueue (always, since we always install it)
    uninstall_kueue_helm(kube_context=kube_context)

    cluster_config = get_cluster_config(config_name)
    cluster_config.cleanup_cluster(kube_context=kube_context, **cluster_kwargs)

    # Delete the flag file after successful cleanup
    delete_cluster_config_flag(context_key)
