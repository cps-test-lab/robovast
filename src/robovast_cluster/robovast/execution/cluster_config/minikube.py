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

from ..cluster_execution import data_paths, store_pod
from ..cluster_execution.kubernetes import apply_manifests, delete_manifests
from . import minio_store
from .base_config import BaseConfig

MINIO_MANIFEST_MINIKUBE = """apiVersion: v1
kind: Pod
metadata:
  name: robovast
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
      name: minio-storage
    resources:
      # A floor, not an estimate: every milli-core requested here is capacity campaign jobs
      # cannot be admitted against, so the request covers an idle server. The LIMIT is a
      # different quantity and has to be sized against the peak: MinIO streams objects, but
      # a batch mirrors dozens of multi-part transfers at once, and each carries buffers the
      # request knows nothing about. Sized well above idle on purpose -- a store that is
      # OOM-killed mid-batch takes that batch's bag conversion with it, and the campaign then
      # fails in postprocessing with no conversion log to explain it. No CPU limit --
      # throttling the store slows every upload and download with no visible cause.
      requests:
        cpu: "50m"
        memory: "256Mi"
      limits:
        memory: "4Gi"
    readinessProbe:
      httpGet:
        path: /minio/health/ready
        port: 9000
      initialDelaySeconds: 5
      periodSeconds: 5
  volumes:
  - name: minio-storage
    emptyDir: {}
---
apiVersion: v1
kind: Service
metadata:
  name: robovast
spec:
  ports:
  - name: s3
    port: 9000
    targetPort: 9000
  - name: console
    port: 9001
    targetPort: 9001
  selector:
    role: robovast
"""


class MinikubeClusterConfig(BaseConfig):

    store_is_placeable = True

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
        """Set up MinIO S3 server for Minikube cluster.

        Args:
            **kwargs: Cluster-specific options (namespace, kube_context,
                control_node_labels)
        """
        control_node_labels = kwargs.pop('control_node_labels', None)
        logging.info("Setting up RoboVAST MinIO S3 server in minikube cluster...")

        load_kube_config(context=kwargs.get('kube_context'))
        k8s_client = client.ApiClient()

        try:
            yaml_objects = list(yaml.safe_load_all(io.StringIO(MINIO_MANIFEST_MINIKUBE)))
        except yaml.YAMLError as e:
            raise RuntimeError(f"Failed to parse MinIO manifest YAML: {str(e)}") from e

        namespace = kwargs.get('namespace', 'default')
        # The registry and the campaign index ride in this pod: setup-lifetime
        # infrastructure, created once here rather than in the service Deployment that
        # every upgrade rolls. See `cluster_execution.store_pod`.
        # The store's own volume, before the pod is refused or applied. A campaign's whole
        # directory is published into it, so this is the volume that decides whether a
        # finished campaign survives its pod.
        store_path = kwargs.get('store_storage_path', '') or data_paths.DEFAULT_STORE_HOST_PATH
        store_class = kwargs.get('store_storage_class', '')
        store_volume = minio_store.store_volume(store_path, store_class)
        yaml_objects = minio_store.apply_store_volume(
            yaml_objects, store_volume,
            minio_store.store_pvc_manifest(namespace, store_class,
                                           kwargs.get('store_storage_size', '')))
        yaml_objects = store_pod.attach_infrastructure(
            yaml_objects, namespace,
            index_storage_path=kwargs.get('index_storage_path', ''),
            index_storage_class=store_class,
            registry_storage_path=kwargs.get('registry_storage_path', ''),
            registry_storage_class=kwargs.get('registry_storage_class', ''),
            ingress_class=kwargs.get('ingress_class', ''))
        yaml_objects = self._apply_pod_node_selector(yaml_objects, control_node_labels)
        self._refuse_a_store_pod_on_the_wrong_node(namespace, 'robovast', control_node_labels)
        minio_store.refuse_a_store_the_manifest_cannot_change(namespace, store_volume)
        try:
            apply_manifests(k8s_client, iter(yaml_objects), namespace=namespace)
        except Exception as e:
            raise RuntimeError(f"Error applying MinIO manifest: {str(e)}") from e

        logging.info(f"MinIO S3 server available at: {self.get_s3_endpoint()}")

    def cleanup_cluster(self, **kwargs):
        """Clean up MinIO S3 server for Minikube cluster.

        Args:
            **kwargs: Additional cluster-specific options (ignored)
        """
        logging.debug("Cleaning up RoboVAST MinIO in minikube cluster...")
        load_kube_config(context=kwargs.get('kube_context'))
        core_v1 = client.CoreV1Api()

        try:
            yaml_objects = yaml.safe_load_all(io.StringIO(MINIO_MANIFEST_MINIKUBE))
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
        namespace = kwargs.get('namespace', 'default')
        # The same store volume `setup_cluster` would apply: a manifest written for a manual
        # `kubectl apply` must describe the store this deployment actually uses, or following
        # these instructions produces a different cluster from running the command.
        store_path = kwargs.get('store_storage_path', '') or data_paths.DEFAULT_STORE_HOST_PATH
        store_class = kwargs.get('store_storage_class', '')
        docs = minio_store.apply_store_volume(
            list(yaml.safe_load_all(io.StringIO(MINIO_MANIFEST_MINIKUBE))),
            minio_store.store_volume(store_path, store_class),
            minio_store.store_pvc_manifest(namespace, store_class,
                                           kwargs.get('store_storage_size', '')))
        docs = store_pod.attach_infrastructure(
            docs, namespace,
            index_storage_path=kwargs.get('index_storage_path', ''),
            index_storage_class=store_class)
        docs = self._apply_pod_node_selector(docs, control_node_labels)
        manifest_to_write = "---\n".join(
            yaml.dump(d, default_flow_style=False) for d in docs if d is not None
        )
        with open(f"{output_dir}/robovast-manifest.yaml", "w") as f:
            f.write(manifest_to_write)

        readme_content = """# Minikube Cluster Setup Instructions

Uses MinIO backed by a directory on the node: finished campaigns live in it and
survive the pod. Suitable for development and short-lived runs; archive anything
that must outlive the machine with `vast share`, and empty the directory with
`vast cluster cleanup --delete-data`.

## Setup Steps

### 1. Apply the RoboVAST MinIO Manifest

```bash
kubectl apply -f robovast-manifest.yaml
```

### 2. Wait for the pod to be ready

```bash
kubectl wait --for=condition=ready pod/robovast --timeout=60s
```

MinIO S3 API is available at `http://robovast:9000` (cluster-internal).
MinIO console is available at port 9001.
"""
        with open(f"{output_dir}/README_minikube.md", "w") as f:
            f.write(readme_content)

    def get_instance_type_command(self):
        """Get command to retrieve instance type of the current node."""
        return "INSTANCE_TYPE=$(uname -m)"
