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
"""GKE: a managed node pool, whose machines are replaced rather than repaired.

What is provider-specific here is how the cluster answers for itself: which GKE cluster a
context names, how large its node pools may autoscale to, and how a node reports its
machine type. The deployment is the same as everywhere -- the ``robovast`` pod for the
registry, campaigns on the service's results volume -- and what
matters on GKE is how those volumes are backed: a node directory goes with the node, so
back the workspaces (and so the results) with a StorageClass::

    vast cluster setup gcp \\
        --workspaces-class standard-rwo \\
        --registry-class standard-rwo \\
        --buildkit-class premium-rwo

Keeping those disks is the operator's snapshot schedule, which RoboVAST does not manage.
"""
import json
import logging
import re
import subprocess

from kubernetes import client

from .base_config import BaseConfig


def _get_gke_cluster_info(kube_context=None): # pylint: disable=too-many-return-statements
    """Return ``(project, location, cluster_name)`` for the active GKE cluster.

    Resolution order:

    1. Parse the context name when it follows the ``gke_{project}_{location}
       _{cluster}`` convention created by ``gcloud container clusters
       get-credentials``.
    2. Fall back to reading the ``spec.providerID`` field on a cluster node
       (format ``gce://PROJECT/ZONE/INSTANCE``), then listing GKE clusters in
       that project to identify the cluster by zone/region.  This handles
       custom context names such as ``gcp-c4``.

    Returns ``(None, None, None)`` when the cluster cannot be identified as a
    GKE cluster or when the required tools are not available.
    """
    # 1. Try standard gke_ context name format
    if kube_context and kube_context.startswith("gke_"):
        parts = kube_context.split("_", 3)
        if len(parts) == 4:
            _, project, location, cluster = parts
            return project, location, cluster

    # 2. Detect from Kubernetes node metadata
    try:
        from robovast.execution.cluster_execution.kube_client import load_kube_config
        load_kube_config(context=kube_context)

        v1 = client.CoreV1Api()
        nodes = v1.list_node(limit=1)
        if not nodes.items:
            return None, None, None

        node = nodes.items[0]
        node_labels = node.metadata.labels or {}

        # GKE nodes always carry this label
        if "cloud.google.com/gke-nodepool" not in node_labels:
            return None, None, None

        # providerID: "gce://PROJECT/ZONE/INSTANCE-NAME"
        provider_id = (node.spec.provider_id or "").strip()
        m = re.match(r"gce://([^/]+)/([^/]+)/", provider_id)
        if not m:
            return None, None, None

        project = m.group(1)
        zone = m.group(2)  # e.g. "us-central1-a"
        region = "-".join(zone.split("-")[:-1])  # e.g. "us-central1"

        # List all GKE clusters in the project and match by location
        r = subprocess.run(
            [
                "gcloud", "container", "clusters", "list",
                "--project", project,
                "--format=json",
            ],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if r.returncode != 0 or not r.stdout.strip():
            logging.debug(
                "gcloud container clusters list failed or returned empty: %s",
                r.stderr.strip(),
            )
            return None, None, None

        clusters = json.loads(r.stdout)
        # Match clusters whose location is the node's zone or region
        matching = [
            c for c in clusters
            if c.get("location") in (zone, region)
        ]
        if len(matching) == 1:
            return project, matching[0]["location"], matching[0]["name"]
        if len(matching) > 1:
            # Multiple clusters in same zone/region — try to match by endpoint
            # against the kubeconfig server URL
            try:
                contexts = subprocess.run(
                    ["kubectl", "config", "view", "--minify", "-o",
                     "jsonpath={.clusters[0].cluster.server}"],
                    capture_output=True, text=True, timeout=5, check=False,
                )
                server = contexts.stdout.strip().lstrip("https://")
                for c in matching:
                    if c.get("endpoint", "") and c["endpoint"] in server:
                        return project, c["location"], c["name"]
            except Exception:
                pass
            # Ambiguous – return first match with a warning
            logging.warning(
                "Multiple GKE clusters found in %s/%s; using '%s'. "
                "Pass --context gke_<project>_<location>_<cluster> to be explicit.",
                project, zone, matching[0]["name"],
            )
            return project, matching[0]["location"], matching[0]["name"]

        return None, None, None

    except Exception as exc:
        logging.debug("GKE cluster detection via node metadata failed: %s", exc)
        return None, None, None


class GcpClusterConfig(BaseConfig):
    """GKE cluster config: autoscaler-aware capacity, VM instance types, no governor."""

    #: GKE node pools are Compute Engine VMs, whose guest kernels expose no cpufreq policy.
    #: See :attr:`BaseConfig.governor_is_settable`.
    governor_is_settable = False

    def prepare_setup_cluster(self, output_dir, **kwargs):
        """Write the ``robovast`` pod manifest and the README for a manual setup."""
        self.write_store_pod_manifest(output_dir, **kwargs)
        readme_content = """# GCP Cluster Setup Instructions

Finished campaigns live on the service's **results volume**, beside the workspaces; the
`robovast` pod in this manifest holds the container registry.

On GKE, back every volume with a StorageClass rather than a node directory: a managed
node pool replaces machines, and a hostPath goes with the machine. The stock classes are
`standard-rwo` (balanced persistent disk) and `premium-rwo` (SSD):

```bash
vast cluster setup gcp \\
    --workspaces-class standard-rwo \\
    --registry-class standard-rwo \\
    --buildkit-class premium-rwo
```

`--workspaces-class` backs the workspaces and the results with it, which is where the
campaigns are. Keeping those disks is the operator's snapshot schedule, which RoboVAST
does not manage.

## Setup Steps

### 1. Apply the RoboVAST manifest

```bash
kubectl apply -f robovast-manifest.yaml
```

### 2. Wait for the pod to be ready

```bash
kubectl wait --for=condition=ready pod/robovast --timeout=120s
```

The registry answers on `/v2` of the service's published host.
"""
        with open(f"{output_dir}/README_gcp.md", "w") as f:
            f.write(readme_content)

    def get_instance_type_command(self):
        """Get command to retrieve instance type of the current node."""
        return (
            'INSTANCE_TYPE=$(curl -s -H "Metadata-Flavor: Google" '
            'http://metadata.google.internal/computeMetadata/v1/instance/machine-type '
            "| awk -F'/' '{print $NF}')"
        )

    def get_cluster_allocatable_resources(self, kube_context=None):
        """Return GKE autoscaler **max** capacity, for sizing admission.

        Queries ``gcloud container clusters describe`` to obtain each node
        pool's autoscaling *maxNodeCount* and machine type, then multiplies
        by the vCPU / memory figures from ``gcloud compute machine-types
        describe``.  This gives the true upper bound even when the cluster
        is currently scaled down.

        Falls back to ``(None, None)`` (the K8s node API query in
        ``cluster_capacity``) when:

        * *kube_context* is not a GKE context (``gke_…`` prefix),
        * ``gcloud`` is not installed or returns an error, or
        * no usable node-pool data can be extracted.

        Args:
            kube_context: Kubernetes context name.  ``None`` uses the active
                context (resolved via ``kubectl config current-context``).

        Returns:
            tuple: ``(cpu_quota: int, memory_quota: str)`` or
                   ``(None, None)`` to fall back to the K8s node query.
        """
        project, location, cluster = _get_gke_cluster_info(kube_context)
        if not project:
            logging.debug(
                "Could not identify a GKE cluster for context '%s'; "
                "falling back to K8s node query",
                kube_context,
            )
            return None, None

        try:
            result = subprocess.run(
                [
                    "gcloud", "container", "clusters", "describe", cluster,
                    "--project", project,
                    "--location", location,
                    "--format=json",
                ],
                capture_output=True, text=True, timeout=60, check=False,
            )
            if result.returncode != 0:
                logging.warning(
                    "gcloud container clusters describe failed: %s. "
                    "Falling back to K8s node query.",
                    result.stderr.strip(),
                )
                return None, None

            cluster_info = json.loads(result.stdout)
        except Exception as exc:
            logging.warning(
                "Failed to describe GKE cluster '%s': %s. "
                "Falling back to K8s node query.",
                cluster, exc,
            )
            return None, None

        # Determine a usable zone for machine-type lookups.
        # Regional clusters have a 'nodeLocations' list; zonal clusters
        # expose the zone in 'location' itself.
        node_locations = cluster_info.get("nodeLocations") or []
        if location.count("-") >= 2:
            # 'us-central1-a' style — already a zone
            zone = location
        elif node_locations:
            zone = node_locations[0]
        else:
            zone = location + "-a"  # safe first-zone assumption

        machine_type_cache = {}  # {machine_type: (cpu_count, memory_mb)}

        def _get_machine_type_info(machine_type):
            """Return (cpu_count, memory_mib) for a GCP machine type."""
            if machine_type in machine_type_cache:
                return machine_type_cache[machine_type]
            try:
                r = subprocess.run(
                    [
                        "gcloud", "compute", "machine-types", "describe",
                        machine_type,
                        "--zone", zone,
                        "--project", project,
                        "--format=value(guestCpus,memoryMb)",
                    ],
                    capture_output=True, text=True, timeout=30, check=False,
                )
                if r.returncode == 0:
                    parts = r.stdout.strip().split()
                    if len(parts) >= 2:
                        result = int(parts[0]), int(parts[1])
                        machine_type_cache[machine_type] = result
                        return result
            except Exception:
                pass
            machine_type_cache[machine_type] = (None, None)
            return None, None

        total_max_cpu = 0
        total_max_mem_mib = 0
        usable_pools = 0

        for pool in cluster_info.get("nodePools", []):
            machine_type = pool.get("config", {}).get("machineType", "")
            autoscaling = pool.get("autoscaling", {})

            if autoscaling.get("enabled"):
                # Prefer totalMaxNodeCount (node-auto-provisioning) over
                # per-zone maxNodeCount.
                max_nodes = (
                    autoscaling.get("totalMaxNodeCount")
                    or autoscaling.get("maxNodeCount")
                    or 0
                )
            else:
                max_nodes = pool.get("initialNodeCount", 0)

            if max_nodes <= 0 or not machine_type:
                continue

            cpu, mem_mib = _get_machine_type_info(machine_type)
            if cpu is None:
                logging.warning(
                    "Could not determine resource info for machine type '%s'; "
                    "skipping pool '%s'.",
                    machine_type, pool.get("name", "?"),
                )
                continue

            total_max_cpu += max_nodes * cpu
            total_max_mem_mib += max_nodes * mem_mib
            usable_pools += 1

        if total_max_cpu <= 0:
            logging.warning(
                "Could not determine GKE autoscaler max capacity for cluster '%s'; "
                "falling back to K8s node query.",
                cluster,
            )
            return None, None

        memory_gib = max(1, total_max_mem_mib // 1024)
        memory_quota = f"{memory_gib}Gi"
        logging.info(
            "GKE autoscaler max capacity for cluster '%s': %d CPU(s), %s "
            "(from %d node pool(s))",
            cluster, total_max_cpu, memory_quota, usable_pools,
        )
        return total_max_cpu, memory_quota
