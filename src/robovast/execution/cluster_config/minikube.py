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

NFS_MANIFEST_MINIKUBE = """apiVersion: v1
kind: Pod
metadata:
  name: robovast
  labels:
    role: robovast
spec:
  containers:
  - name: robovast
    image: busybox
    command: [ "sleep", "infinity" ]
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
      hostPath:
        path: /data
        type: Directory
"""


class MinikubeClusterConfig(BaseConfig):

    def setup_cluster(self, **kwargs):
        """Set up transfer mechanism for Minikube cluster.

        Args:
            **kwargs: Cluster-specific options (ignored for Minikube)
        """
        logging.info("Setting up RoboVAST in minikube cluster...")

        # Load Kubernetes configuration
        config.load_kube_config()

        # Initialize API clients
        k8s_client = client.ApiClient()

        logging.debug("Applying RoboVAST manifest to Kubernetes cluster...")
        try:
            try:
                yaml_objects = yaml.safe_load_all(io.StringIO(NFS_MANIFEST_MINIKUBE))
            except yaml.YAMLError as e:
                raise RuntimeError(f"Failed to parse NFS manifest YAML: {str(e)}") from e
            apply_manifests(k8s_client, yaml_objects)
        except Exception as e:
            raise RuntimeError(f"Error applying NFS manifest: {str(e)}") from e

    def cleanup_cluster(self, **kwargs):
        """Clean up transfer mechanism for Minikube cluster.
        
        Args:
            **kwargs: Additional cluster-specific options (ignored)
        """
        logging.debug("Cleaning up RoboVAST in minikube cluster...")
        # Load Kubernetes configuration
        config.load_kube_config()

        # Initialize API client
        core_v1 = client.CoreV1Api()

        try:
            yaml_objects = yaml.safe_load_all(io.StringIO(NFS_MANIFEST_MINIKUBE))
        except yaml.YAMLError as e:
            raise RuntimeError(f"Failed to parse PVC manifest YAML: {str(e)}") from e

        delete_manifests(core_v1, yaml_objects)
        logging.debug("RoboVAST manifest deleted successfully!")

    def get_job_volumes(self):
        """Get job volumes for Minikube cluster."""
        return [
            {
                "name": "data-storage",
                "hostPath": {
                    "path": "/data",
                    "type": "Directory"
                }
            }
        ]

    def prepare_setup_cluster(self, output_dir, **kwargs):
        """Prepare any prerequisites before setting up the cluster.

        Args:
            output_dir (str): Directory where setup files will be written
            **kwargs: Cluster-specific options (ignored for Minikube)
        """
        with open(f"{output_dir}/robovast-manifest.yaml", "w") as f:
            f.write(NFS_MANIFEST_MINIKUBE)
        readme_content = """# Minikube Cluster Setup Instructions

## Setup Steps

### 1. Apply the NFS Server Manifest

Apply the NFS server manifest to create the NFS server pod and service:

```bash
kubectl apply -f robovast-manifest.yaml
```

"""
        with open(f"{output_dir}/README_minikube.md", "w") as f:
            f.write(readme_content)
