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

import yaml
from kubernetes import client, config

from robovast.common.kubernetes import apply_manifests, delete_manifests

from .base_config import BaseConfig

NFS_MANIFEST_RKE2 = """---
apiVersion: v1
kind: Pod
metadata:
  name: robovast
  namespace: default
  labels:
    role: robovast
spec:
  containers:
  - name: robovast
    image: itsthenetwork/nfs-server-alpine:latest
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
        - SYS_MODULE
    volumeMounts:
      - mountPath: /exports
        name: mypvc
    env:
      - name: SHARED_DIRECTORY
        value: "/exports"
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
        path: /transfer
        type: Directory
---
apiVersion: v1
kind: Service
metadata:
  name: robovast
  namespace: default
spec:
  type: ClusterIP
  ports:
    - name: nfs
      port: 2049
      targetPort: 2049
      protocol: TCP
    - name: mountd
      port: 20048
      targetPort: 20048
      protocol: TCP
    - name: rpcbind
      port: 111
      targetPort: 111
      protocol: TCP
    - name: http
      port: 9998
      targetPort: 80
      protocol: TCP
  selector:
    role: robovast
"""

PVC_MANIFEST_RKE2 = """---
apiVersion: v1
kind: PersistentVolume
metadata:
  name: nfs-data-pv
spec:
  capacity:
    storage: 100Gi
  accessModes:
    - ReadWriteMany
  nfs:
    server: {server_ip}
    path: /
  mountOptions:
    - nfsvers=4.2
    - hard
    - tcp
    - rsize=1048576
    - wsize=1048576
    - timeo=600
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: nfs-data-pvc
  namespace: default
spec:
  accessModes:
    - ReadWriteMany
  resources:
    requests:
      storage: 100Gi
  volumeName: nfs-data-pv
  storageClassName: ""
"""


class Rke2ClusterConfig(BaseConfig):

    def setup_cluster(self, **kwargs):
        """Set up transfer mechanism for RKE2 cluster.

        Args:
            **kwargs: Cluster-specific options (ignored for RKE2)
        """
        print("Setting up RoboVAST in RKE2 cluster...")

        # Load Kubernetes configuration
        config.load_kube_config()

        # Initialize API clients
        k8s_client = client.ApiClient()

        print("Applying RoboVAST manifest to Kubernetes cluster...")
        try:
            try:
                yaml_objects = yaml.safe_load_all(io.StringIO(NFS_MANIFEST_RKE2))
            except yaml.YAMLError as e:
                raise RuntimeError(f"Failed to parse NFS manifest YAML: {str(e)}") from e
            apply_manifests(k8s_client, yaml_objects)

            # Get NFS server ClusterIP
            core_v1 = client.CoreV1Api()
            service = core_v1.read_namespaced_service(
                name="robovast",
                namespace="default"
            )
            server_ip = service.spec.cluster_ip
            print(f"### RoboVAST ClusterIP: {server_ip}")
            try:
                yaml_objects = yaml.safe_load_all(io.StringIO(PVC_MANIFEST_RKE2.format(server_ip=server_ip)))
            except yaml.YAMLError as e:
                raise RuntimeError(f"Failed to parse PVC manifest YAML: {str(e)}") from e
            apply_manifests(k8s_client, yaml_objects)
        except Exception as e:
            raise RuntimeError(f"Error applying NFS manifest: {str(e)}") from e

    def cleanup_cluster(self):
        """Clean up transfer mechanism for RKE2 cluster."""
        print("Cleaning up RoboVAST in RKE2 cluster...")
        # Load Kubernetes configuration
        config.load_kube_config()

        # Initialize API client
        core_v1 = client.CoreV1Api()

        try:
            yaml_objects = yaml.safe_load_all(io.StringIO(NFS_MANIFEST_RKE2))
        except yaml.YAMLError as e:
            raise RuntimeError(f"Failed to parse PVC manifest YAML: {str(e)}") from e

        delete_manifests(core_v1, yaml_objects)
        print("### RoboVAST manifest deleted successfully!")

        yaml_objects = yaml.safe_load_all(io.StringIO(PVC_MANIFEST_RKE2))
        delete_manifests(core_v1, yaml_objects)
        print("### PVC manifest deleted successfully!")

    def get_job_volumes(self):
        """Get job volumes for Minikube cluster."""
        return [
            {
                "name": "data-storage",
                "persistentVolumeClaim": {
                    "claimName": "nfs-data-pvc"
                }
            }
        ]

    def prepare_setup_cluster(self, output_dir, **kwargs):
        """Prepare any prerequisites before setting up the cluster.

        Args:
            output_dir (str): Directory where setup files will be written
            **kwargs: Cluster-specific options (ignored for RKE2)
        """
        with open(f"{output_dir}/1-robovast-manifest.yaml", "w") as f:
            f.write(NFS_MANIFEST_RKE2)
        with open(f"{output_dir}/2-pvc-manifest.yaml.tmpl", "w") as f:
            f.write(PVC_MANIFEST_RKE2.format(server_ip="<NFS_SERVER_IP>"))

        # Create README with setup instructions
        readme_content = """# RKE2 Cluster Setup Instructions

## Setup Steps

### 1. Apply the RoboVAST Manifest

First, apply the NFS server manifest to create the NFS server pod and service:

```bash
kubectl apply -f 1-robovast-manifest.yaml
```

Wait for the RoboVAST pod to be ready:

```bash
kubectl wait --for=condition=ready pod/robovast -n default --timeout=60s
```

### 2. Get the RoboVAST ClusterIP

Retrieve the ClusterIP of the RoboVAST service:

```bash
kubectl get service robovast -n default -o jsonpath='{.spec.clusterIP}'
```

This will output an IP address (e.g., `10.43.123.45`).

### 3. Update the PVC Manifest Template

Edit the `2-pvc-manifest.yaml.tmpl` file and replace `<ROBO_VAST_IP>` with the actual ClusterIP from step 2:

```bash
# Replace <ROBO_VAST_IP> with the actual IP address
sed 's/<ROBO_VAST_IP>/10.43.123.45/g' 2-pvc-manifest.yaml.tmpl > 2-pvc-manifest.yaml
```

Or manually edit the file and replace `<ROBO_VAST_IP>` with the ClusterIP.

### 4. Apply the PVC Manifest

Apply the updated PVC manifest:

```bash
kubectl apply -f 2-pvc-manifest.yaml
```
"""
        with open(f"{output_dir}/README_rke2.md", "w") as f:
            f.write(readme_content)
