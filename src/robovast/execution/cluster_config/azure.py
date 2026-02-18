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
import time

import yaml
from kubernetes import client, config

from ..cluster_execution.kubernetes import apply_manifests, delete_manifests
from .base_config import BaseConfig

NFS_MANIFEST_AZURE = """---
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
  storageClassName: managed-csi
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
    image: itsthenetwork/nfs-server-alpine:latest
    env:
    - name: SHARED_DIRECTORY
      value: /exports
    ports:
      - name: nfs
        containerPort: 2049
    securityContext:
      privileged: true
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


class AzureClusterConfig(BaseConfig):

    def setup_cluster(self, storage_size="10Gi", **kwargs):
        """Set up transfer mechanism for Azure cluster.

        Args:
            storage_size (str): Size of the persistent volume (default: "10Gi")
            **kwargs: Additional cluster-specific options (ignored)
        """
        logging.info("Setting up RoboVAST in Azure cluster...")
        logging.info(f"Storage size: {storage_size}")

        # Load Kubernetes configuration
        config.load_kube_config()

        # Initialize API clients
        k8s_client = client.ApiClient()

        logging.debug("Applying RoboVAST manifest to Kubernetes cluster...")
        try:
            try:
                yaml_objects = yaml.safe_load_all(io.StringIO(NFS_MANIFEST_AZURE.format(storage_size=storage_size)))
            except yaml.YAMLError as e:
                raise RuntimeError(f"Failed to parse RoboVAST manifest YAML: {str(e)}") from e
            apply_manifests(k8s_client, yaml_objects)

        except Exception as e:
            raise RuntimeError(f"Error applying RoboVAST manifest: {str(e)}") from e

        # Wait for service to be ready and get its ClusterIP
        core_v1 = client.CoreV1Api()
        try:
            max_retries = 30
            for i in range(max_retries):
                try:
                    service = core_v1.read_namespaced_service(
                        name="robovast",
                        namespace="default"
                    )
                    if service.spec.cluster_ip:
                        nfs_server_ip = service.spec.cluster_ip
                        logging.info(f"NFS server available at: {nfs_server_ip}")
                        break
                except client.exceptions.ApiException:
                    if i < max_retries - 1:
                        logging.info(f"Waiting for robovast service... ({i+1}s)")
                        time.sleep(1)
                    else:
                        logging.warning("Service not ready yet, but continuing...")
        except Exception as e:
            logging.warning(f"Could not retrieve NFS server IP: {e}")

    def cleanup_cluster(self, storage_size="10Gi", **kwargs):
        """Clean up transfer mechanism for Azure cluster.

        Args:
            storage_size (str): Size of the persistent volume (default: "10Gi")
            **kwargs: Additional cluster-specific options (ignored)
        """
        logging.debug("Cleaning up RoboVAST in Azure cluster...")
        # Load Kubernetes configuration
        config.load_kube_config()

        # Initialize API client
        core_v1 = client.CoreV1Api()

        try:
            yaml_objects = yaml.safe_load_all(io.StringIO(NFS_MANIFEST_AZURE.format(storage_size=storage_size)))
        except yaml.YAMLError as e:
            raise RuntimeError(f"Failed to parse PVC manifest YAML: {str(e)}") from e

        delete_manifests(core_v1, yaml_objects)
        logging.debug("NFS manifest deleted successfully!")

    def get_job_volumes(self):
        """Get job volumes for Azure cluster."""
        # Load Kubernetes configuration
        config.load_kube_config()

        # Initialize API client
        core_v1 = client.CoreV1Api()

        # Get the robovast service to retrieve its ClusterIP
        try:
            service = core_v1.read_namespaced_service(
                name="robovast",
                namespace="default"
            )
            nfs_server = service.spec.cluster_ip
            logging.debug(f"Retrieved NFS server IP: {nfs_server}")
        except Exception as e:
            logging.warning(f"Failed to retrieve robovast service IP: {e}. Falling back to DNS name.")
            nfs_server = "robovast.default.svc.cluster.local"

        return [
            {
                "name": "data-storage",
                "nfs": {
                    "server": nfs_server,
                    "path": "/"
                }
            }
        ]

    def prepare_setup_cluster(self, output_dir, storage_size="10Gi", **kwargs):
        """Prepare any prerequisites before setting up the cluster.

        Args:
            output_dir (str): Directory where setup files will be written
            storage_size (str): Size of the persistent volume (default: "10Gi")
            **kwargs: Additional cluster-specific options (ignored)
        """
        with open(f"{output_dir}/robovast-manifest.yaml", "w") as f:
            f.write(NFS_MANIFEST_AZURE.format(storage_size=storage_size))

        # Create README with setup instructions
        readme_content = f"""# Azure Cluster Setup Instructions

## Setup Steps

### 1. Apply the RoboVAST Server Manifest

This manifest creates a PersistentVolumeClaim with {storage_size} of storage.

Apply the RoboVAST server manifest:

```bash
kubectl apply -f robovast-manifest.yaml
```

"""
        with open(f"{output_dir}/README_azure.md", "w") as f:
            f.write(readme_content)

    def get_instance_type_command(self):
      """Get command to retrieve instance type of the current node."""
      return """
INSTANCE_TYPE=$(curl -s -H "Metadata: true" "http://169.254.169.254/metadata/instance/compute/vmSize?api-version=2021-02-01&format=text")
"""