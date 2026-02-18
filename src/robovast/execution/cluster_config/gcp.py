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

NFS_MANIFEST_GCP = """---
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
kind: PersistentVolumeClaim
metadata:
  name: robovast-pvc
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: {storage_size}
  storageClassName: robovast-storage
---
apiVersion: v1
kind: Pod
metadata:
  name: robovast
  labels:
    role: robovast
spec:
  containers:
  - name: robovast
    image: adnanhodzic/nfs-server-k8s
    ports:
      - name: nfs
        containerPort: 2049
      - name: mountd
        containerPort: 20048
      - name: rpcbind
        containerPort: 111
    securityContext:
      privileged: true
      capabilities:
        add:
        - SYS_ADMIN
        - SETPCAP
    volumeMounts:
      - mountPath: /exports
        name: mypvc
  - name: http-server
    image: nginx:alpine
    ports:
      - name: http
        containerPort: 80
    volumeMounts:
      - mountPath: /usr/share/nginx/html
        name: mypvc
        readOnly: true
  volumes:
    - name: mypvc
      persistentVolumeClaim:
        claimName: robovast-pvc
---
apiVersion: v1
kind: Service
metadata:
  name: robovast
spec:
  ports:
    - name: nfs
      port: 2049
    - name: mountd
      port: 20048
    - name: rpcbind
      port: 111
    - name: http
      port: 9998
  selector:
    role: robovast
"""


class GcpClusterConfig(BaseConfig):

    def setup_cluster(self, storage_size="10Gi", disk_type="pd-standard", **kwargs):
        """Set up transfer mechanism for GCP cluster.

        Args:
            storage_size (str): Size of the persistent volume (default: "10Gi")
            disk_type (str): GCP PD type for StorageClass (default: "pd-standard")
            **kwargs: Additional cluster-specific options (ignored)
        """
        logging.info("Setting up RoboVAST in GCP cluster...")
        logging.info(f"Storage size: {storage_size}")
        logging.info(f"Disk type: {disk_type}")

        # Load Kubernetes configuration
        config.load_kube_config()

        # Initialize API clients
        k8s_client = client.ApiClient()

        logging.debug("Applying RoboVAST manifest to Kubernetes cluster...")
        try:
            try:
                yaml_objects = yaml.safe_load_all(
                    io.StringIO(
                        NFS_MANIFEST_GCP.format(storage_size=storage_size, disk_type=disk_type)
                    )
                )
            except yaml.YAMLError as e:
                raise RuntimeError(f"Failed to parse RoboVAST manifest YAML: {str(e)}") from e
            apply_manifests(k8s_client, yaml_objects)

        except Exception as e:
            raise RuntimeError(f"Error applying RoboVAST manifest: {str(e)}") from e

    def cleanup_cluster(self, storage_size="10Gi", disk_type="pd-standard", **kwargs):
        """Clean up transfer mechanism for GCP cluster.

        Args:
            storage_size (str): Size of the persistent volume (default: "10Gi")
            disk_type (str): GCP PD type for StorageClass (default: "pd-standard")
            **kwargs: Additional cluster-specific options (ignored)
        """
        logging.debug("Cleaning up RoboVAST in GCP cluster...")
        # Load Kubernetes configuration
        config.load_kube_config()

        # Initialize API client
        core_v1 = client.CoreV1Api()

        try:
            yaml_objects = yaml.safe_load_all(
                io.StringIO(
                    NFS_MANIFEST_GCP.format(storage_size=storage_size, disk_type=disk_type)
                )
            )
        except yaml.YAMLError as e:
            raise RuntimeError(f"Failed to parse PVC manifest YAML: {str(e)}") from e

        delete_manifests(core_v1, yaml_objects)
        logging.debug("NFS manifest deleted successfully!")
        logging.info("-----")
        logging.info("Warning: Persistent volumes may need to be deleted manually in GCP console.")
        logging.info("-----")

    def get_job_volumes(self):
        """Get job volumes for GCP cluster."""
        return [
            {
                "name": "data-storage",
                "nfs": {
                    "server": "robovast.default.svc.cluster.local",
                    "path": "/"
                }
            }
        ]

    def prepare_setup_cluster(self, output_dir, storage_size="10Gi", disk_type="pd-standard", **kwargs):
        """Prepare any prerequisites before setting up the cluster.

        Args:
            output_dir (str): Directory where setup files will be written
            storage_size (str): Size of the persistent volume (default: "10Gi")
            disk_type (str): GCP PD type for StorageClass (default: "pd-standard")
            **kwargs: Additional cluster-specific options (ignored)
        """
        with open(f"{output_dir}/robovast-manifest.yaml", "w") as f:
            f.write(NFS_MANIFEST_GCP.format(storage_size=storage_size, disk_type=disk_type))

        # Create README with setup instructions
        readme_content = f"""# GCP Cluster Setup Instructions

## Setup Steps

### 1. Apply the RoboVAST Server Manifest

This manifest creates a PersistentVolumeClaim with {storage_size} of storage.

The StorageClass uses GCP Persistent Disk type `{disk_type}`.

Apply the RoboVAST server manifest:

```bash
kubectl apply -f robovast-manifest.yaml
```

"""
        with open(f"{output_dir}/README_gcp.md", "w") as f:
            f.write(readme_content)

    def get_instance_type_command(self):
      """Get command to retrieve instance type of the current node."""
      return """
INSTANCE_TYPE=$(curl -s -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/machine-type | awk -F'/' '{print $NF}')
"""