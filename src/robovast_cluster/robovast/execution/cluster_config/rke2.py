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
import io
import logging

import yaml
from kubernetes import client

from robovast.execution.cluster_execution.kube_client import load_kube_config

from ..cluster_execution import store_pod
from ..cluster_execution.kubernetes import apply_manifests, check_pod_running, delete_manifests
from .base_config import BaseConfig

#: The embedded MinIO pod this config deploys, and the volume its ``/data`` mounts.
#: Named here rather than inline in the manifest because ``get_store_usage`` has to find
#: exactly this pod and volume in the kubelet's stats -- a manifest and a reader that spell
#: the same name twice drift apart on the first rename.
MINIO_POD_NAME = "robovast"
MINIO_VOLUME_NAME = "minio-storage"

MINIO_MANIFEST_RKE2 = f"""---
apiVersion: v1
kind: Pod
metadata:
  name: {MINIO_POD_NAME}
  namespace: default
  labels:
    role: robovast
spec:
  containers:
  - name: minio
    image: minio/minio:latest
    args: ["server", "/data", "--console-address", ":9001"]
    env:
    - name: MINIO_ROOT_USER
      value: "minioadmin"
    - name: MINIO_ROOT_PASSWORD
      value: "minioadmin"
    ports:
    - name: s3
      containerPort: 9000
    - name: console
      containerPort: 9001
    volumeMounts:
    - mountPath: /data
      name: {MINIO_VOLUME_NAME}
    resources:
      # A floor, not an estimate: every milli-core and byte requested here is capacity that
      # campaign jobs cannot be admitted against, so the request covers an IDLE server. That
      # is what the scheduler places on, and what keeps the store off the top of the kubelet's
      # eviction list when a node does run short.
      #
      # NEITHER RESOURCE IS CAPPED, for one reason: the store's demand is set by how much
      # campaign work is in flight, and this manifest cannot know that. A batch mirrors dozens
      # of multi-part transfers at once, so the peak follows a concurrency declared in a
      # campaign somewhere, and any ceiling written here is a guess against a number that
      # moves. Capping CPU would throttle every upload and download with no visible cause;
      # capping MEMORY is worse, because exceeding that cap is an OOM kill rather than a
      # slowdown -- and it kills setup-lifetime infrastructure the whole deployment depends
      # on, taking the running campaign's transfers with it and failing that campaign
      # somewhere that says nothing about the store.
      #
      # This is not "no ceiling at all": a genuine leak still surfaces, as node memory
      # pressure the kubelet reports and acts on, which is diagnosable in a way a periodic
      # exit 137 on the store is not.
      requests:
        cpu: "50m"
        memory: "1Gi"
    readinessProbe:
      httpGet:
        path: /minio/health/ready
        port: 9000
      initialDelaySeconds: 5
      periodSeconds: 5
  volumes:
  - name: {MINIO_VOLUME_NAME}
    emptyDir: {{}}
---
apiVersion: v1
kind: Service
metadata:
  name: robovast
  namespace: default
spec:
  type: ClusterIP
  ports:
  - name: s3
    port: 9000
    targetPort: 9000
    protocol: TCP
  - name: console
    port: 9001
    targetPort: 9001
    protocol: TCP
  selector:
    role: robovast
"""


class Rke2ClusterConfig(BaseConfig):

    @staticmethod
    def _refuse_a_store_pod_on_the_wrong_node(namespace, pod_name, node_labels):
        """Raise when a live store Pod sits somewhere the resolved placement does not want.

        ``apply_manifests`` tolerates a 409 and **keeps** the existing object -- deliberately,
        because recreating a running MinIO pod on every setup would be a far worse default.
        The cost is that a changed ``nodeSelector`` does not take effect, and setup then prints
        "completed successfully" over a store still on the old node, with the disk and store
        meters reporting a machine nobody chose. Checked here rather than reported afterwards,
        because a placement that is announced and not applied is the failure this whole
        mechanism exists to remove.
        """
        if not node_labels:
            return
        try:
            pod = client.CoreV1Api().read_namespaced_pod(pod_name, namespace)
        except client.exceptions.ApiException as e:
            if e.status == 404:
                return          # nothing live; the manifest will simply be created
            raise
        selector = pod.spec.node_selector or {}
        if all(selector.get(k) == v for k, v in node_labels.items()):
            return
        raise RuntimeError(
            f"the results store pod is already running on node {pod.spec.node_name} and "
            f"cannot be moved by re-applying its manifest -- an existing pod is kept as it "
            f"is, so the new placement would be reported but never take effect. Delete it "
            f"(`kubectl delete pod {pod_name} -n {namespace}`) or run "
            f"`vast cluster cleanup` first -- both DISCARD every campaign the store holds, "
            f"so archive what matters first with `vast share <campaign>` or "
            f"`vast campaign download <campaign>`.")

    def setup_cluster(self, **kwargs):
        """Set up MinIO S3 server for RKE2 cluster.

        Args:
            **kwargs: Cluster-specific options (namespace, kube_context,
                control_node_labels)
        """
        control_node_labels = kwargs.pop('control_node_labels', None)
        logging.info("Setting up RoboVAST MinIO S3 server in RKE2 cluster...")
        logging.info("")

        load_kube_config(context=kwargs.get('kube_context'))
        k8s_client = client.ApiClient()

        try:
            yaml_objects = list(yaml.safe_load_all(io.StringIO(MINIO_MANIFEST_RKE2)))
        except yaml.YAMLError as e:
            raise RuntimeError(f"Failed to parse MinIO manifest YAML: {str(e)}") from e

        namespace = kwargs.get('namespace', 'default')
        # The registry and the campaign index ride in this pod: setup-lifetime
        # infrastructure, created once here rather than in the service Deployment that
        # every upgrade rolls. See `cluster_execution.store_pod`.
        yaml_objects = store_pod.attach_infrastructure(
            yaml_objects, namespace,
            index_storage_path=kwargs.get('index_storage_path', ''),
            registry_storage_path=kwargs.get('registry_storage_path', ''),
            registry_storage_class=kwargs.get('registry_storage_class', ''))
        yaml_objects = self._apply_pod_node_selector(yaml_objects, control_node_labels)
        self._refuse_a_store_pod_on_the_wrong_node(namespace, MINIO_POD_NAME, control_node_labels)
        try:
            apply_manifests(k8s_client, iter(yaml_objects), namespace=namespace)
        except Exception as e:
            raise RuntimeError(f"Error applying MinIO manifest: {str(e)}") from e

        logging.info(f"MinIO S3 server available at: {self.get_s3_endpoint()}")

    def cleanup_cluster(self, **kwargs):
        """Clean up MinIO S3 server for RKE2 cluster.

        Args:
            **kwargs: Additional cluster-specific options (ignored)
        """
        logging.debug("Cleaning up RoboVAST MinIO in RKE2 cluster...")
        load_kube_config(context=kwargs.get('kube_context'))
        core_v1 = client.CoreV1Api()

        try:
            yaml_objects = yaml.safe_load_all(io.StringIO(MINIO_MANIFEST_RKE2))
        except yaml.YAMLError as e:
            raise RuntimeError(f"Failed to parse MinIO manifest YAML: {str(e)}") from e

        namespace = kwargs.get('namespace', 'default')
        yaml_objects = store_pod.attach_infrastructure(list(yaml_objects), namespace)
        delete_manifests(core_v1, store_pod.infrastructure_claims(namespace) + yaml_objects,
                         namespace=namespace)
        logging.debug("MinIO manifest deleted successfully!")

    def prepare_setup_cluster(self, output_dir, **kwargs):
        """Prepare any prerequisites before setting up the cluster.

        Args:
            output_dir (str): Directory where setup files will be written
            **kwargs: Cluster-specific options (control_node_labels)
        """
        control_node_labels = kwargs.pop('control_node_labels', None)
        docs = store_pod.attach_infrastructure(
            list(yaml.safe_load_all(io.StringIO(MINIO_MANIFEST_RKE2))),
            kwargs.get('namespace', 'default'))
        docs = self._apply_pod_node_selector(docs, control_node_labels)
        manifest_to_write = "---\n".join(
            yaml.dump(d, default_flow_style=False) for d in docs if d is not None
        )
        with open(f"{output_dir}/robovast-manifest.yaml", "w") as f:
            f.write(manifest_to_write)

        readme_content = """# RKE2 Cluster Setup Instructions

Uses MinIO backed by an `emptyDir`, so it needs no storage class and no
preparation on the nodes.

The store is where finished campaigns live: a campaign's full directory is
published into it, and downloads, re-postprocessing and the campaign index all
read from there. Because the volume is an `emptyDir`, that ends when the MinIO pod
is restarted or rescheduled -- so archive anything worth keeping with `vast share`
or pull it with `vast campaign download`. The volume declares no size limit, so it
draws from the node filesystem, and the web UI's **Store** meter reports how full
it is.

## Setup Steps

### 1. Apply the RoboVAST MinIO Manifest

```bash
kubectl apply -f robovast-manifest.yaml
```

### 2. Wait for the pod to be ready

```bash
kubectl wait --for=condition=ready pod/robovast -n default --timeout=60s
```

MinIO S3 API is available at `http://robovast:9000` (cluster-internal).
MinIO console is available at port 9001.
"""
        with open(f"{output_dir}/README_rke2.md", "w") as f:
            f.write(readme_content)

    def get_store_usage(self, node_summaries, namespace="default"):
        """The embedded MinIO pod's ``/data`` volume, from the kubelet's volume stats.

        This config mounts ``/data`` as an ``emptyDir`` (see ``MINIO_MANIFEST_RKE2``), which
        the kubelet therefore reports as a volume of the MinIO pod; because the ``emptyDir``
        declares no ``sizeLimit`` it has no bound of its own. The meter matters because this
        volume holds every campaign the deployment has finished -- a full one stalls
        campaigns, and what it holds is not scratch.

        THE DENOMINATOR IS ``used + available``, NOT ``capacityBytes``. For an unbounded
        ``emptyDir`` the volume's ``capacityBytes`` is the whole node filesystem, which the
        store shares with the images, the containers and every campaign directory -- so it
        reported 29 GiB of 460 on a disk already 314 GiB full, inviting the reading that
        there were 430 GiB of store headroom when there were about 146. ``availableBytes``
        is what the filesystem will actually still take, so ``used + available`` is the
        ceiling this store can really reach.

        That makes the denominator move as the rest of the node fills, which is a feature
        and not an artefact: the number an operator needs before a sweep is how much more
        this buffer can hold *now*, and it genuinely shrinks when something else grows. A
        fixed 460 was never that number for any reading.

        ``(None, None)`` when the pod is not in the stats -- it may live on a node whose
        kubelet was not read, and a store the caller cannot see is not a store of size zero.
        Also ``(None, None)`` when the kubelet gave no ``availableBytes``: the honest
        answer is no meter, rather than falling back to the capacity that caused this.
        """
        del namespace
        for summary in (node_summaries or {}).values():
            for pod in (summary.get("pods") or []):
                if (pod.get("podRef") or {}).get("name") != MINIO_POD_NAME:
                    continue
                for volume in (pod.get("volume") or []):
                    if volume.get("name") != MINIO_VOLUME_NAME:
                        continue
                    used = volume.get("usedBytes")
                    available = volume.get("availableBytes")
                    if used is None or available is None:
                        return None, None
                    return int(used), int(used) + int(available)
        return None, None

    def verify_cluster_ready(self, k8s_client=None, namespace="default", kube_context=None):
        """Ensure the embedded MinIO (``robovast``) pod is running before a run.

        RKE2 stores campaign data in the in-cluster MinIO server hosted by the
        ``robovast`` pod, so the run cannot proceed without it.
        """
        del kube_context
        if k8s_client is None:
            load_kube_config()
            k8s_client = client.CoreV1Api()
        pod_ok, pod_msg = check_pod_running(k8s_client, "robovast", namespace)
        if not pod_ok:
            raise RuntimeError(
                f"{pod_msg}. The RKE2 MinIO storage pod is required. "
                "Set it up with: vast cluster setup rke2"
            )
        logging.debug(pod_msg)

    def get_instance_type_command(self):
        """Get command to retrieve instance type of the current node."""
        return "INSTANCE_TYPE=$(uname -m)"
