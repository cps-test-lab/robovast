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

"""Kueue installation, queue setup, and workload cleanup for cluster execution."""

import logging
import subprocess
import tempfile
import os

from kubernetes import client, config
from kubernetes.utils.quantity import parse_quantity

logger = logging.getLogger(__name__)

KUEUE_NAMESPACE = "kueue-system"
KUEUE_HELM_RELEASE = "kueue"
KUEUE_HELM_REPO = "oci://registry.k8s.io/kueue/charts/kueue"
KUEUE_HELM_VERSION = "0.16.1"
KUEUE_QUEUE_NAME = "robovast"
CLUSTER_QUEUE_NAME = "robovast-cluster-queue"

# Workload CRD for cleanup (v1beta2 used by Kueue 0.16+)
KUEUE_WORKLOAD_GROUP = "kueue.x-k8s.io"
KUEUE_WORKLOAD_VERSION = "v1beta2"
KUEUE_WORKLOAD_PLURAL = "workloads"
KUEUE_RESOURCE_FLAVOR_NAME = "default-flavor"

# Fallback quotas when cluster resources cannot be queried
DEFAULT_CPU_QUOTA = 8
DEFAULT_MEMORY_QUOTA = "32Gi"

# values.yaml applied on every Kueue Helm install/upgrade
KUEUE_HELM_VALUES = """
controllerManager:
  manager:
    configuration:
      clientConnection:
        qps: 1000      # High QPS to clear the 10,000 event backlog
        burst: 2000
      controller:
        groupKindConcurrency:
          Job.batch: 100               # Process finished jobs faster
          Workload.kueue.x-k8s.io: 100  # Admit new jobs faster
      # IMPORTANT: Native Kueue cleanup
      workloadRetentionPolicy:
        afterFinished: 30s    # Clean up the "Workload" 30s after the Job is done
"""

# ResourceFlavor + ClusterQueue + LocalQueue (execution namespace set at runtime)
# {cpu_quota} and {memory_quota} are filled from cluster allocatable resources
KUEUE_QUEUES_YAML = """
apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata:
  name: default-flavor
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata:
  name: {cluster_queue}
spec:
  namespaceSelector: {{}}
  resourceGroups:
  - coveredResources: ["cpu", "memory"]
    flavors:
    - name: default-flavor
      resources:
      - name: cpu
        nominalQuota: {cpu_quota}
      - name: memory
        nominalQuota: {memory_quota}
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: LocalQueue
metadata:
  namespace: {namespace}
  name: {queue_name}
spec:
  clusterQueue: {cluster_queue}
"""


def _parse_resource(val):
    """Parse Kubernetes resource quantity to numeric value. Returns 0 for None/missing."""
    if val is None:
        return 0
    try:
        return float(parse_quantity(val))
    except (ValueError, TypeError):
        return 0


def set_cluster_queue_stop_policy(stop_policy, kube_context=None):
    """Set the stopPolicy on the robovast ClusterQueue.

    Useful before bulk job deletion so Kueue does not admit new jobs during cleanup.
    Common values: ``"HoldAll"`` (pause admission and preempt running workloads),
    ``"Hold"`` (pause new admissions only), ``"None"`` or empty string to resume.

    Args:
        stop_policy: Policy string, e.g. ``"HoldAll"``.
        kube_context: Kubernetes context to use. ``None`` uses the active context.
    """
    try:
        config.load_kube_config(context=kube_context)
    except config.ConfigException:
        pass
    custom_api = client.CustomObjectsApi()
    body = {"spec": {"stopPolicy": stop_policy or "None"}}
    try:
        custom_api.patch_cluster_custom_object(
            group=KUEUE_WORKLOAD_GROUP,
            version=KUEUE_WORKLOAD_VERSION,
            plural="clusterqueues",
            name=CLUSTER_QUEUE_NAME,
            body=body,
        )
        logger.debug(
            "ClusterQueue '%s' stopPolicy set to '%s'", CLUSTER_QUEUE_NAME, stop_policy
        )
    except client.rest.ApiException as e:
        if e.status == 404:
            logger.debug(
                "ClusterQueue '%s' not found, skipping stopPolicy patch", CLUSTER_QUEUE_NAME
            )
        else:
            logger.warning("Could not set ClusterQueue '%s' stopPolicy: %s", CLUSTER_QUEUE_NAME, e)


def cleanup_kueue_workloads(
    namespace="default",
    label_selector=None,
    run_id=None,
    k8s_batch_client=None,
):
    """Delete Kueue Workload objects for scenario run jobs.

    Workloads don't inherit job labels (jobgroup, run-id). They use
    kueue.x-k8s.io/queue-name=robovast. When run_id is given, only workloads
    owned by jobs of that run are deleted (matched via ownerReferences and job
    UIDs). Without run_id, all workloads in the robovast queue are deleted.
    If Kueue is not installed (Workload CRD missing), logs and returns without
    failing.

    Args:
        namespace: Kubernetes namespace
        label_selector: Label selector used to list jobs for run_id scoping
        run_id: If given, only delete workloads for this run's jobs
        k8s_batch_client: BatchV1Api client; required when run_id is given
    """
    try:
        custom_api = client.CustomObjectsApi()
        delete_opts = client.V1DeleteOptions(
            grace_period_seconds=0, propagation_policy="Background"
        )
        queue_selector = "kueue.x-k8s.io/queue-name=robovast"

        if run_id is not None and k8s_batch_client is not None:
            # Collect UIDs of jobs belonging to this run so we only delete
            # the workloads that are owned by those jobs.
            job_uid_selector = label_selector or f"jobgroup=scenario-runs"
            try:
                job_list = k8s_batch_client.list_namespaced_job(
                    namespace=namespace, label_selector=job_uid_selector
                )
                run_job_uids = {job.metadata.uid for job in job_list.items}
            except client.rest.ApiException as e:
                logger.warning(f"Could not list jobs for run-scoped workload cleanup: {e}")
                run_job_uids = set()

            if not run_job_uids:
                logger.debug("No jobs found for run '%s', skipping workload cleanup", run_id)
                return

            # List all workloads in the queue and delete only those owned by
            # jobs of the target run.
            logger.debug(
                "Deleting Kueue workloads owned by %d job(s) for run '%s'",
                len(run_job_uids), run_id,
            )
            workloads = custom_api.list_namespaced_custom_object(
                group=KUEUE_WORKLOAD_GROUP,
                version=KUEUE_WORKLOAD_VERSION,
                namespace=namespace,
                plural=KUEUE_WORKLOAD_PLURAL,
                label_selector=queue_selector,
            )
            deleted = 0
            for wl in workloads.get("items", []):
                owner_uids = {
                    ref["uid"]
                    for ref in (wl.get("metadata", {}).get("ownerReferences") or [])
                }
                if owner_uids & run_job_uids:
                    wl_name = wl["metadata"]["name"]
                    try:
                        custom_api.delete_namespaced_custom_object(
                            group=KUEUE_WORKLOAD_GROUP,
                            version=KUEUE_WORKLOAD_VERSION,
                            namespace=namespace,
                            plural=KUEUE_WORKLOAD_PLURAL,
                            name=wl_name,
                            body=delete_opts,
                        )
                        deleted += 1
                    except client.rest.ApiException as e:
                        if e.status == 404:
                            pass  # already gone
                        else:
                            logger.warning(f"Could not delete workload '{wl_name}': {e}")
            logger.info(
                "Successfully deleted %d scenario-runs Kueue workload(s) for run '%s'",
                deleted, run_id,
            )
        else:
            # No run_id scoping: delete all robovast queue workloads at once
            logger.debug(f"Deleting all Kueue workloads with selector '{queue_selector}'")
            custom_api.delete_collection_namespaced_custom_object(
                group=KUEUE_WORKLOAD_GROUP,
                version=KUEUE_WORKLOAD_VERSION,
                namespace=namespace,
                plural=KUEUE_WORKLOAD_PLURAL,
                label_selector=queue_selector,
                body=delete_opts,
            )
            logger.info("Successfully deleted scenario-runs Kueue workloads")

        # Hard cleanup: force-remove finalizers from any workloads that are stuck in
        # Terminating (their own finalizers block deletion after the soft delete above).
        try:
            remaining = custom_api.list_namespaced_custom_object(
                group=KUEUE_WORKLOAD_GROUP,
                version=KUEUE_WORKLOAD_VERSION,
                namespace=namespace,
                plural=KUEUE_WORKLOAD_PLURAL,
                label_selector=queue_selector,
            )
            for wl in remaining.get("items", []):
                meta = wl.get("metadata", {})
                if meta.get("deletionTimestamp") or meta.get("finalizers"):
                    wl_name = meta["name"]
                    try:
                        custom_api.patch_namespaced_custom_object(
                            group=KUEUE_WORKLOAD_GROUP,
                            version=KUEUE_WORKLOAD_VERSION,
                            namespace=namespace,
                            plural=KUEUE_WORKLOAD_PLURAL,
                            name=wl_name,
                            body={"metadata": {"finalizers": None}},
                        )
                        logger.info(
                            "Removed finalizers from stuck Kueue workload '%s'", wl_name
                        )
                    except client.rest.ApiException as patch_err:
                        if patch_err.status != 404:
                            logger.warning(
                                "Could not patch workload '%s' finalizers: %s", wl_name, patch_err
                            )
        except client.rest.ApiException as list_err:
            logger.warning("Could not list workloads for finalizer hard-cleanup: %s", list_err)

    except client.rest.ApiException as e:
        if e.status == 404:
            logger.debug(
                "Kueue Workload CRD not found (Kueue may not be installed), skipping workload cleanup"
            )
        else:
            logger.error(f"Error deleting Kueue workloads: {e}")
            raise


def cleanup_kueue_cluster_resources(kube_context=None):
    """Force-remove finalizers from ClusterQueue and ResourceFlavor.

    Called during cluster teardown to unblock deletion of Kueue cluster-scoped
    resources that may be stuck with finalizers preventing ``helm uninstall`` from
    completing cleanly.

    Args:
        kube_context: Kubernetes context to use. ``None`` uses the active context.
    """
    try:
        config.load_kube_config(context=kube_context)
    except config.ConfigException:
        pass
    custom_api = client.CustomObjectsApi()
    patch_body = {"metadata": {"finalizers": None}}
    for plural, name, label in [
        ("clusterqueues", CLUSTER_QUEUE_NAME, "ClusterQueue"),
        ("resourceflavors", KUEUE_RESOURCE_FLAVOR_NAME, "ResourceFlavor"),
    ]:
        try:
            custom_api.patch_cluster_custom_object(
                group=KUEUE_WORKLOAD_GROUP,
                version=KUEUE_WORKLOAD_VERSION,
                plural=plural,
                name=name,
                body=patch_body,
            )
            logger.info("Removed finalizers from %s '%s'", label, name)
        except client.rest.ApiException as e:
            if e.status == 404:
                logger.debug("%s '%s' not found, skipping finalizer patch", label, name)
            else:
                logger.warning(
                    "Could not remove finalizers from %s '%s': %s", label, name, e
                )


def get_cluster_allocatable_resources(kube_context=None):
    """Query the cluster for available CPU and memory (allocatable minus requested).

    Sums allocatable from all nodes, subtracts requests from Running/Pending pods,
    and returns the available capacity for Kueue quotas.

    Args:
        kube_context: Kubernetes context to use. None uses the active context.

    Returns:
        tuple: (cpu_quota: int, memory_quota: str) e.g. (8, "32Gi").
               Uses DEFAULT_* if cluster cannot be queried.
    """
    try:
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config(context=kube_context)

        v1 = client.CoreV1Api()
        total_allocatable_cpu = 0.0
        total_allocatable_mem = 0  # bytes
        total_requested_cpu = 0.0
        total_requested_mem = 0  # bytes

        # 1. Sum allocatable from all nodes
        nodes = v1.list_node()
        for node in nodes.items:
            alloc = node.status.allocatable or {}
            total_allocatable_cpu += _parse_resource(alloc.get("cpu"))
            total_allocatable_mem += int(_parse_resource(alloc.get("memory")))

        # 2. Sum resource requests from Running/Pending pods
        pods = v1.list_pod_for_all_namespaces()
        for pod in pods.items:
            if pod.status.phase in ("Running", "Pending") and pod.spec:
                containers = list(pod.spec.containers or [])
                containers.extend(pod.spec.init_containers or [])
                for container in containers:
                    res = {}
                    if container.resources and container.resources.requests:
                        res = container.resources.requests
                    total_requested_cpu += _parse_resource(res.get("cpu", "0"))
                    total_requested_mem += int(_parse_resource(res.get("memory", "0")))

        # 3. Calculate availability
        avail_cpu = total_allocatable_cpu - total_requested_cpu
        avail_mem = total_allocatable_mem - total_requested_mem

        if avail_cpu <= 0 or avail_mem <= 0:
            logger.warning(
                "No available resources (allocatable - requested). Using defaults."
            )
            return DEFAULT_CPU_QUOTA, DEFAULT_MEMORY_QUOTA

        cpu_quota = max(1, int(avail_cpu))
        memory_gi = max(1, avail_mem // (1024**3))
        memory_quota = f"{memory_gi}Gi"

        logger.info(
            "Cluster: allocatable %dcpu/%dGi - requested %dcpu/%dGi = available %dcpu/%s",
            int(total_allocatable_cpu),
            total_allocatable_mem // (1024**3),
            int(total_requested_cpu),
            total_requested_mem // (1024**3),
            cpu_quota,
            memory_quota,
        )
        return cpu_quota, memory_quota

    except Exception as e:
        logger.warning(
            "Failed to query cluster resources: %s. Using defaults.",
            e,
        )
        return DEFAULT_CPU_QUOTA, DEFAULT_MEMORY_QUOTA


def _run_helm(args, check=True):
    """Run helm command. Returns (success, stderr)."""
    cmd = ["helm"] + args
    logger.debug("Running: %s", " ".join(cmd))
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if result.returncode != 0:
        logger.warning("Helm command failed: %s", result.stderr)
        if check:
            raise RuntimeError(
                f"Helm command failed: {result.stderr or result.stdout}"
            )
        return False, result.stderr or ""
    return True, ""


def _run_kubectl_apply(yaml_content, check=True, kube_context=None):
    """Apply YAML via kubectl. Returns success."""
    ctx_args = ["--context", kube_context] if kube_context else []
    cmd = ["kubectl"] + ctx_args + ["apply", "-f", "-"]
    logger.debug("Applying Kueue queue manifests via kubectl")
    result = subprocess.run(
        cmd,
        input=yaml_content,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        logger.warning("kubectl apply failed: %s", result.stderr)
        if check:
            raise RuntimeError(
                f"kubectl apply failed: {result.stderr or result.stdout}"
            )
        return False
    return True


def install_kueue_helm(kube_context=None):
    """Install Kueue via Helm in kueue-system namespace.

    Requires Helm to be installed and in PATH.
    If Kueue is already installed, upgrades to the specified version.

    Args:
        kube_context: Kubernetes context to use. None uses the active context.
    """
    ctx_helm = [f"--kube-context={kube_context}"] if kube_context else []
    ctx_kubectl = ["--context", kube_context] if kube_context else []
    result = subprocess.run(
        ["helm", "list", "-n", KUEUE_NAMESPACE, "-q", "-f", KUEUE_HELM_RELEASE] + ctx_helm,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        logger.info(
            "Kueue Helm release already exists, upgrading to version %s",
            KUEUE_HELM_VERSION,
        )
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", prefix="kueue_values_", delete=False
        ) as vf:
            vf.write(KUEUE_HELM_VALUES)
            values_path = vf.name
        try:
            _run_helm(
                [
                    "upgrade",
                    KUEUE_HELM_RELEASE,
                    KUEUE_HELM_REPO,
                    f"--version={KUEUE_HELM_VERSION}",
                    f"--namespace={KUEUE_NAMESPACE}",
                    f"--values={values_path}",
                ] + ctx_helm
            )
        finally:
            os.unlink(values_path)
        # Wait for CRDs after upgrade (upgrade may update CRDs)
        subprocess.run(
            ["kubectl"] + ctx_kubectl + [
                "wait",
                "--for=condition=established",
                "crd/resourceflavors.kueue.x-k8s.io",
                "--timeout=60s",
            ],
            capture_output=True,
            check=False,
        )
        return

    logger.info("Installing Kueue via Helm in namespace %s...", KUEUE_NAMESPACE)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", prefix="kueue_values_", delete=False
    ) as vf:
        vf.write(KUEUE_HELM_VALUES)
        values_path = vf.name
    try:
        _run_helm(
            [
                "install",
                KUEUE_HELM_RELEASE,
                KUEUE_HELM_REPO,
                f"--version={KUEUE_HELM_VERSION}",
                "--create-namespace",
                f"--namespace={KUEUE_NAMESPACE}",
                f"--values={values_path}",
            ] + ctx_helm
        )
    finally:
        os.unlink(values_path)
    logger.info("Kueue installed successfully. Waiting for controller and CRDs...")
    # Wait for ResourceFlavor CRD to be established (CRDs install before controller)
    subprocess.run(
        ["kubectl"] + ctx_kubectl + [
            "wait",
            "--for=condition=established",
            "crd/resourceflavors.kueue.x-k8s.io",
            "--timeout=120s",
        ],
        capture_output=True,
        check=False,
    )
    # Wait for deployment to be ready
    subprocess.run(
        ["kubectl"] + ctx_kubectl + [
            "rollout",
            "status",
            "deployment/kueue-controller-manager",
            "-n",
            KUEUE_NAMESPACE,
            "--timeout=120s",
        ],
        capture_output=True,
        check=False,
    )


def uninstall_kueue_helm(kube_context=None):
    """Uninstall Kueue Helm release from kueue-system namespace.

    Args:
        kube_context: Kubernetes context to use. None uses the active context.
    """
    logger.info("Uninstalling Kueue Helm release...")
    # Force-clear finalizers from cluster-scoped Kueue resources first so that helm
    # uninstall does not hang waiting for them to be garbage-collected.
    cleanup_kueue_cluster_resources(kube_context=kube_context)
    ctx_helm = [f"--kube-context={kube_context}"] if kube_context else []
    ok, err = _run_helm(
        ["uninstall", KUEUE_HELM_RELEASE, f"--namespace={KUEUE_NAMESPACE}"] + ctx_helm,
        check=False,
    )
    if not ok:
        if "release: not found" in (err or "").lower():
            logger.info("Kueue Helm release not found, skipping uninstall")
        else:
            raise RuntimeError(f"Failed to uninstall Kueue: {err}")


def apply_kueue_queues(namespace="default", kube_context=None):
    """Create ResourceFlavor, ClusterQueue, and LocalQueue for RoboVAST.

    Quotas are set from cluster allocatable CPU and memory.

    Args:
        namespace: Kubernetes namespace for the LocalQueue (execution namespace)
        kube_context: Kubernetes context to use. None uses the active context.
    """
    cpu_quota, memory_quota = get_cluster_allocatable_resources(kube_context=kube_context)
    yaml_content = KUEUE_QUEUES_YAML.format(
        namespace=namespace,
        queue_name=KUEUE_QUEUE_NAME,
        cluster_queue=CLUSTER_QUEUE_NAME,
        cpu_quota=cpu_quota,
        memory_quota=memory_quota,
    ).strip()
    _run_kubectl_apply(yaml_content, kube_context=kube_context)
    logger.info(
        "Kueue queues configured: LocalQueue '%s' in namespace '%s'",
        KUEUE_QUEUE_NAME,
        namespace,
    )


def prepare_kueue_setup(output_dir, namespace="default", kube_context=None):
    """Write Kueue queue manifests and README to output_dir.

    Quotas are set from cluster allocatable CPU and memory when cluster is
    accessible, otherwise defaults are used.

    Args:
        output_dir: Directory to write files
        namespace: Kubernetes namespace for LocalQueue
        kube_context: Kubernetes context to use. None uses the active context.
    """
    cpu_quota, memory_quota = get_cluster_allocatable_resources(kube_context=kube_context)
    yaml_content = KUEUE_QUEUES_YAML.format(
        namespace=namespace,
        queue_name=KUEUE_QUEUE_NAME,
        cluster_queue=CLUSTER_QUEUE_NAME,
        cpu_quota=cpu_quota,
        memory_quota=memory_quota,
    ).strip()
    kueue_file = f"{output_dir}/kueue-queue-setup.yaml"
    with open(kueue_file, "w") as f:
        f.write(yaml_content)

    readme = f"""# Kueue Setup Instructions

Kueue is installed for job queueing when you run `vast execution cluster setup`.

## 1. Install Kueue via Helm

```bash
helm install kueue oci://registry.k8s.io/kueue/charts/kueue \\
  --version={KUEUE_HELM_VERSION} --create-namespace --namespace={KUEUE_NAMESPACE}
```

Requires [Helm](https://helm.sh/) to be installed.

## 2. Apply ResourceFlavor, ClusterQueue, and LocalQueue

Wait for Kueue CRDs to be established, then apply:

```bash
kubectl wait --for=condition=established crd/resourceflavors.kueue.x-k8s.io --timeout=60s
kubectl apply -f kueue-queue-setup.yaml
```

This creates:
- ResourceFlavor `default-flavor`
- ClusterQueue `{CLUSTER_QUEUE_NAME}` (cpu/memory quotas)
- LocalQueue `{KUEUE_QUEUE_NAME}` in namespace `{namespace}`
"""
    readme_path = f"{output_dir}/README_kueue.md"
    with open(readme_path, "w") as f:
        f.write(readme)
    logger.debug("Wrote %s and %s", kueue_file, readme_path)
