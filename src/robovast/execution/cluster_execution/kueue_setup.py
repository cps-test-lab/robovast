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

"""Kueue installation and queue setup for cluster execution."""

import logging
import subprocess

from kubernetes import client, config
from kubernetes.utils.quantity import parse_quantity

logger = logging.getLogger(__name__)

KUEUE_NAMESPACE = "kueue-system"
KUEUE_HELM_RELEASE = "kueue"
KUEUE_HELM_REPO = "oci://registry.k8s.io/kueue/charts/kueue"
KUEUE_HELM_VERSION = "0.16.1"
KUEUE_QUEUE_NAME = "robovast"
CLUSTER_QUEUE_NAME = "robovast-cluster-queue"

# Fallback quotas when cluster resources cannot be queried
DEFAULT_CPU_QUOTA = 8
DEFAULT_MEMORY_QUOTA = "32Gi"

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


def get_cluster_allocatable_resources():
    """Query the cluster for available CPU and memory (allocatable minus requested).

    Sums allocatable from all nodes, subtracts requests from Running/Pending pods,
    and returns the available capacity for Kueue quotas.

    Returns:
        tuple: (cpu_quota: int, memory_quota: str) e.g. (8, "32Gi").
               Uses DEFAULT_* if cluster cannot be queried.
    """
    try:
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()

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
    )
    if result.returncode != 0:
        logger.warning("Helm command failed: %s", result.stderr)
        if check:
            raise RuntimeError(
                f"Helm command failed: {result.stderr or result.stdout}"
            )
        return False, result.stderr or ""
    return True, ""


def _run_kubectl_apply(yaml_content, check=True):
    """Apply YAML via kubectl. Returns success."""
    cmd = ["kubectl", "apply", "-f", "-"]
    logger.debug("Applying Kueue queue manifests via kubectl")
    result = subprocess.run(
        cmd,
        input=yaml_content,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        logger.warning("kubectl apply failed: %s", result.stderr)
        if check:
            raise RuntimeError(
                f"kubectl apply failed: {result.stderr or result.stdout}"
            )
        return False
    return True


def install_kueue_helm():
    """Install Kueue via Helm in kueue-system namespace.

    Requires Helm to be installed and in PATH.
    If Kueue is already installed, upgrades to the specified version.
    """
    result = subprocess.run(
        ["helm", "list", "-n", KUEUE_NAMESPACE, "-q", "-f", KUEUE_HELM_RELEASE],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        logger.info(
            "Kueue Helm release already exists, upgrading to version %s",
            KUEUE_HELM_VERSION,
        )
        _run_helm(
            [
                "upgrade",
                KUEUE_HELM_RELEASE,
                KUEUE_HELM_REPO,
                f"--version={KUEUE_HELM_VERSION}",
                f"--namespace={KUEUE_NAMESPACE}",
            ]
        )
        return

    logger.info("Installing Kueue via Helm in namespace %s...", KUEUE_NAMESPACE)
    _run_helm(
        [
            "install",
            KUEUE_HELM_RELEASE,
            KUEUE_HELM_REPO,
            f"--version={KUEUE_HELM_VERSION}",
            "--create-namespace",
            f"--namespace={KUEUE_NAMESPACE}",
        ]
    )
    logger.info("Kueue installed successfully. Waiting for controller to be ready...")
    # Wait for deployment to be ready
    subprocess.run(
        [
            "kubectl",
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


def uninstall_kueue_helm():
    """Uninstall Kueue Helm release from kueue-system namespace."""
    logger.info("Uninstalling Kueue Helm release...")
    ok, err = _run_helm(
        ["uninstall", KUEUE_HELM_RELEASE, f"--namespace={KUEUE_NAMESPACE}"],
        check=False,
    )
    if not ok:
        if "release: not found" in (err or "").lower():
            logger.info("Kueue Helm release not found, skipping uninstall")
        else:
            raise RuntimeError(f"Failed to uninstall Kueue: {err}")


def apply_kueue_queues(namespace="default"):
    """Create ResourceFlavor, ClusterQueue, and LocalQueue for RoboVAST.

    Quotas are set from cluster allocatable CPU and memory.

    Args:
        namespace: Kubernetes namespace for the LocalQueue (execution namespace)
    """
    cpu_quota, memory_quota = get_cluster_allocatable_resources()
    yaml_content = KUEUE_QUEUES_YAML.format(
        namespace=namespace,
        queue_name=KUEUE_QUEUE_NAME,
        cluster_queue=CLUSTER_QUEUE_NAME,
        cpu_quota=cpu_quota,
        memory_quota=memory_quota,
    ).strip()
    _run_kubectl_apply(yaml_content)
    logger.info(
        "Kueue queues configured: LocalQueue '%s' in namespace '%s'",
        KUEUE_QUEUE_NAME,
        namespace,
    )


def prepare_kueue_setup(output_dir, namespace="default"):
    """Write Kueue queue manifests and README to output_dir.

    Quotas are set from cluster allocatable CPU and memory when cluster is
    accessible, otherwise defaults are used.

    Args:
        output_dir: Directory to write files
        namespace: Kubernetes namespace for LocalQueue
    """
    cpu_quota, memory_quota = get_cluster_allocatable_resources()
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

After Kueue controller is ready:

```bash
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
