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
from kubernetes import client, config

from ..cluster_execution.kubernetes import apply_manifests, delete_manifests
from .base_config import BaseConfig

MINIO_MANIFEST_GCP = """---
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: robovast-storage
provisioner: kubernetes.io/gce-pd
parameters:
  type: {disk_type}
reclaimPolicy: Delete
volumeBindingMode: WaitForFirstConsumer
---
apiVersion: v1
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
    readinessProbe:
      httpGet:
        path: /minio/health/ready
        port: 9000
      initialDelaySeconds: 10
      periodSeconds: 5
  volumes:
  - name: minio-storage
    volumeClaimTemplate:
      spec:
        accessModes: [ "ReadWriteOnce" ]
        storageClassName: "robovast-storage"
        resources:
          requests:
            storage: {storage_size}
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


class GcpClusterConfig(BaseConfig):

    def setup_cluster(self, storage_size="10Gi", disk_type="pd-standard", **kwargs):
        """Set up MinIO S3 server for GCP cluster.

        Args:
            storage_size (str): Size of the persistent volume (default: "10Gi")
            disk_type (str): GCP PD type for StorageClass (default: "pd-standard")
            **kwargs: Additional cluster-specific options (ignored)
        """
        logging.info("Setting up RoboVAST MinIO S3 server in GCP cluster...")
        logging.info(f"Storage size: {storage_size}")
        logging.info(f"Disk type: {disk_type}")

        config.load_kube_config()
        k8s_client = client.ApiClient()

        try:
            yaml_objects = yaml.safe_load_all(
                io.StringIO(MINIO_MANIFEST_GCP.format(storage_size=storage_size, disk_type=disk_type))
            )
        except yaml.YAMLError as e:
            raise RuntimeError(f"Failed to parse MinIO manifest YAML: {str(e)}") from e

        namespace = kwargs.get('namespace', 'default')
        try:
            apply_manifests(k8s_client, yaml_objects, namespace=namespace)
        except Exception as e:
            raise RuntimeError(f"Error applying MinIO manifest: {str(e)}") from e

    def cleanup_cluster(self, storage_size="10Gi", disk_type="pd-standard", **kwargs):
        """Clean up MinIO S3 server for GCP cluster.

        Args:
            storage_size (str): Size of the persistent volume (default: "10Gi")
            disk_type (str): GCP PD type for StorageClass (default: "pd-standard")
            **kwargs: Additional cluster-specific options (ignored)
        """
        logging.debug("Cleaning up RoboVAST MinIO in GCP cluster...")
        config.load_kube_config()
        core_v1 = client.CoreV1Api()

        try:
            yaml_objects = yaml.safe_load_all(
                io.StringIO(MINIO_MANIFEST_GCP.format(storage_size=storage_size, disk_type=disk_type))
            )
        except yaml.YAMLError as e:
            raise RuntimeError(f"Failed to parse MinIO manifest YAML: {str(e)}") from e

        namespace = kwargs.get('namespace', 'default')
        delete_manifests(core_v1, yaml_objects, namespace=namespace)
        logging.debug("MinIO manifest deleted successfully!")
        logging.info("-----")
        logging.info("Warning: Persistent volumes may need to be deleted manually in GCP console.")
        logging.info("-----")

    def prepare_setup_cluster(self, output_dir, storage_size="10Gi", disk_type="pd-standard", **kwargs):
        """Prepare any prerequisites before setting up the cluster.

        Args:
            output_dir (str): Directory where setup files will be written
            storage_size (str): Size of the persistent volume (default: "10Gi")
            disk_type (str): GCP PD type for StorageClass (default: "pd-standard")
            **kwargs: Cluster-specific options (ignored)
        """
        with open(f"{output_dir}/robovast-manifest.yaml", "w") as f:
            f.write(MINIO_MANIFEST_GCP.format(storage_size=storage_size, disk_type=disk_type))

        readme_content = f"""# GCP Cluster Setup Instructions

Uses MinIO backed by a GCP Persistent Disk PVC ({storage_size}, type `{disk_type}`).

## Setup Steps

### 1. Apply the RoboVAST MinIO Manifest

```bash
kubectl apply -f robovast-manifest.yaml
```

### 2. Wait for the pod to be ready

```bash
kubectl wait --for=condition=ready pod/robovast --timeout=120s
```

MinIO S3 API is available at `http://robovast:9000` (cluster-internal).
MinIO console is available at port 9001.
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
