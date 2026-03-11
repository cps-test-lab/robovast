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
"""GCP cluster configuration using Google Cloud Storage S3-compatible API.

Instead of running an embedded MinIO server, this config uses a user-provided
GCS bucket via the S3-compatible ``https://storage.googleapis.com`` endpoint
with HMAC credentials.  A ``robovast`` helper pod (nginx + archiver sidecar)
is still deployed for archive/download workflows.

Required ``-o`` options at ``setup`` time::

    vast execution cluster setup gcp \\
        -o gcs_bucket=<BUCKET_NAME> \\
        -o gcs_access_key=<HMAC_ACCESS_KEY> \\
        -o gcs_secret_key=<HMAC_SECRET_KEY> \\
        [-o storage_size=50Gi] [-o disk_type=pd-ssd]
"""
import io
import logging
import os
from typing import Optional

import yaml
from kubernetes import client, config

from ..cluster_execution.kubernetes import apply_manifests, delete_manifests
from .base_config import BaseConfig

GCS_S3_ENDPOINT = "https://storage.googleapis.com"

# ---------------------------------------------------------------------------
# Kubernetes manifest for the ``robovast`` helper pod on GCP.
#
# No MinIO container — the archiver and HTTP server connect to GCS directly.
# The archiver sidecar receives GCS credentials via environment variables so
# that ``s3_to_targz.py`` / ``targz_to_s3.py`` can reach the external bucket.
# ---------------------------------------------------------------------------
ROBOVAST_POD_MANIFEST_GCP = """---
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: robovast-storage
provisioner: pd.csi.storage.gke.io
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
  - name: http-server
    image: nginx:alpine
    ports:
      - name: http
        containerPort: 80
    volumeMounts:
      - mountPath: /usr/share/nginx/html
        name: robovast-storage
        readOnly: true
    resources:
      limits:
        cpu: "500m"
        memory: "1Gi"
  - name: archiver
    image: ghcr.io/cps-test-lab/robovast-sidecar:latest
    command: ["sleep", "infinity"]
    env:
    - name: S3_ENDPOINT
      value: "{s3_endpoint}"
    - name: S3_ACCESS_KEY
      value: "{gcs_access_key}"
    - name: S3_SECRET_KEY
      value: "{gcs_secret_key}"
    - name: S3_BUCKET
      value: "{gcs_bucket}"
    volumeMounts:
      - mountPath: /data
        name: robovast-storage
    resources:
      limits:
        cpu: "4000m"
        memory: "1Gi"
  volumes:
  - name: robovast-storage
    ephemeral:
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
  - name: http
    port: 9998
    targetPort: 80
    protocol: TCP
  selector:
    role: robovast
"""


class GcpClusterConfig(BaseConfig):
    """GCP cluster config using Google Cloud Storage (S3 interface).

    Instead of deploying MinIO, this config stores all campaign data in a
    single user-provided GCS bucket.  Campaigns are isolated by key prefix
    (``<campaign-id>/``).  HMAC credentials are passed via ``-o`` CLI flags
    and persisted in the cluster flag file between ``setup`` and ``cleanup``.
    """

    def __init__(self):
        super().__init__()
        self._gcs_bucket: Optional[str] = None
        self._gcs_access_key: Optional[str] = None
        self._gcs_secret_key: Optional[str] = None
        self._gcs_key_file: Optional[str] = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _extract_gcs_params(self, kwargs: dict) -> tuple:
        """Extract and validate GCS parameters from *kwargs*.

        Returns:
            tuple: (gcs_bucket, gcs_access_key, gcs_secret_key, gcs_key_file)

        Raises:
            ValueError: If any required GCS parameter is missing.
        """
        gcs_bucket = kwargs.get("gcs_bucket") or self._gcs_bucket or os.environ.get("ROBOVAST_GCS_BUCKET")
        gcs_access_key = kwargs.get("gcs_access_key") or self._gcs_access_key or os.environ.get("ROBOVAST_GCS_ACCESS_KEY")
        gcs_secret_key = kwargs.get("gcs_secret_key") or self._gcs_secret_key or os.environ.get("ROBOVAST_GCS_SECRET_KEY")
        gcs_key_file = kwargs.get("gcs_key_file") or self._gcs_key_file or os.environ.get("ROBOVAST_GCS_KEY_FILE")

        missing = []
        if not gcs_bucket:
            missing.append("gcs_bucket")
        # HMAC credentials are only required when no service-account key file is provided.
        if not gcs_key_file:
            if not gcs_access_key:
                missing.append("gcs_access_key")
            if not gcs_secret_key:
                missing.append("gcs_secret_key")
        if missing:
            raise ValueError(
                f"Missing required GCP options: {', '.join(missing)}. "
                f"Pass them via -o, e.g. -o gcs_bucket=my-bucket -o gcs_access_key=GOOG... -o gcs_secret_key=..."
            )
        return gcs_bucket, gcs_access_key, gcs_secret_key, gcs_key_file

    def _render_manifest(self, storage_size, disk_type, gcs_bucket, gcs_access_key, gcs_secret_key):
        """Render the Kubernetes manifest YAML with the given parameters."""
        return ROBOVAST_POD_MANIFEST_GCP.format(
            storage_size=storage_size,
            disk_type=disk_type,
            s3_endpoint=GCS_S3_ENDPOINT,
            gcs_bucket=gcs_bucket,
            gcs_access_key=gcs_access_key,
            gcs_secret_key=gcs_secret_key,
        )

    def _store_gcs_params(self, gcs_bucket, gcs_access_key, gcs_secret_key, gcs_key_file=None):
        """Cache GCS params on the instance so subsequent method calls can use them."""
        self._gcs_bucket = gcs_bucket
        self._gcs_access_key = gcs_access_key
        self._gcs_secret_key = gcs_secret_key
        if gcs_key_file is not None:
            self._gcs_key_file = gcs_key_file

    def restore_from_setup_kwargs(self, kwargs: dict) -> None:
        """Restore GCS credentials from the kwargs saved during ``setup_cluster``.

        Called automatically after a fresh :class:`GcpClusterConfig` instance
        is created so that :meth:`get_s3_credentials` and
        :meth:`get_s3_bucket` return the correct values without requiring
        the credentials to be passed again via ``-o`` flags.

        Args:
            kwargs: The ``setup_kwargs`` dict from the cluster flag file.
        """
        gcs_bucket = kwargs.get("gcs_bucket")
        gcs_access_key = kwargs.get("gcs_access_key")
        gcs_secret_key = kwargs.get("gcs_secret_key")
        gcs_key_file = kwargs.get("gcs_key_file")
        if gcs_bucket or gcs_access_key or gcs_secret_key or gcs_key_file:
            self._store_gcs_params(gcs_bucket, gcs_access_key, gcs_secret_key, gcs_key_file)

    # ------------------------------------------------------------------
    # BaseConfig overrides – S3 configuration
    # ------------------------------------------------------------------

    def uses_embedded_s3(self) -> bool:
        return False

    def get_s3_endpoint(self) -> str:
        """Cluster-internal S3 endpoint — GCS is reachable from GKE pods."""
        return GCS_S3_ENDPOINT

    def get_host_s3_endpoint(self) -> Optional[str]:
        """Host-side S3 endpoint — GCS is reachable directly, no port-forward."""
        return GCS_S3_ENDPOINT

    def get_s3_credentials(self) -> tuple:
        if self._gcs_access_key and self._gcs_secret_key:
            return (self._gcs_access_key, self._gcs_secret_key)
        raise RuntimeError(
            "GCS credentials not configured. "
            "Call setup_cluster() first or pass gcs_access_key/gcs_secret_key via -o."
        )

    def get_s3_bucket(self) -> Optional[str]:
        return self._gcs_bucket

    def get_s3_region(self) -> str:
        return "auto"

    # ------------------------------------------------------------------
    # BaseConfig overrides – cluster lifecycle
    # ------------------------------------------------------------------

    def setup_cluster(self, storage_size="10Gi", disk_type="pd-standard", **kwargs):
        """Deploy the robovast helper pod (archiver + nginx) for GCP.

        The GCS bucket must already exist.  This method validates that the
        bucket is reachable with the provided HMAC credentials.

        Args:
            storage_size (str): Size of the ephemeral PVC for archive storage.
            disk_type (str): GCP PD type for the StorageClass.
            **kwargs: Must include ``gcs_bucket``, ``gcs_access_key``,
                ``gcs_secret_key``.  Optional: ``kube_context``, ``namespace``.
        """
        gcs_bucket, gcs_access_key, gcs_secret_key, gcs_key_file = self._extract_gcs_params(kwargs)
        self._store_gcs_params(gcs_bucket, gcs_access_key, gcs_secret_key, gcs_key_file)

        logging.info("Setting up RoboVAST helper pod for GCP (GCS S3 interface)...")
        logging.info(f"GCS bucket: {gcs_bucket}")
        logging.info(f"Storage size: {storage_size}  |  Disk type: {disk_type}")

        # Validate GCS connectivity — only possible with HMAC credentials;
        # when using a service-account key file the caller is responsible for
        # ensuring the bucket exists.
        if gcs_access_key and gcs_secret_key:
            self._validate_gcs_bucket(gcs_bucket, gcs_access_key, gcs_secret_key)

        config.load_kube_config(context=kwargs.get('kube_context'))
        k8s_client = client.ApiClient()

        manifest_yaml = self._render_manifest(
            storage_size, disk_type, gcs_bucket, gcs_access_key, gcs_secret_key,
        )

        try:
            yaml_objects = yaml.safe_load_all(io.StringIO(manifest_yaml))
        except yaml.YAMLError as e:
            raise RuntimeError(f"Failed to parse manifest YAML: {str(e)}") from e

        namespace = kwargs.get('namespace', 'default')
        try:
            apply_manifests(k8s_client, yaml_objects, namespace=namespace)
        except Exception as e:
            raise RuntimeError(f"Error applying manifest: {str(e)}") from e

    def cleanup_cluster(self, storage_size="10Gi", disk_type="pd-standard", **kwargs):
        """Remove the robovast helper pod.  The GCS bucket is **not** deleted.

        Args:
            storage_size (str): Must match the value used during ``setup_cluster``.
            disk_type (str): Must match the value used during ``setup_cluster``.
            **kwargs: Must include GCS options (restored from the flag file).
        """
        gcs_bucket, gcs_access_key, gcs_secret_key, gcs_key_file = self._extract_gcs_params(kwargs)
        self._store_gcs_params(gcs_bucket, gcs_access_key, gcs_secret_key, gcs_key_file)

        logging.debug("Cleaning up RoboVAST helper pod in GCP cluster...")
        config.load_kube_config(context=kwargs.get('kube_context'))
        core_v1 = client.CoreV1Api()

        manifest_yaml = self._render_manifest(
            storage_size, disk_type, gcs_bucket, gcs_access_key, gcs_secret_key,
        )

        try:
            yaml_objects = yaml.safe_load_all(io.StringIO(manifest_yaml))
        except yaml.YAMLError as e:
            raise RuntimeError(f"Failed to parse manifest YAML: {str(e)}") from e

        namespace = kwargs.get('namespace', 'default')
        delete_manifests(core_v1, yaml_objects, namespace=namespace)
        logging.debug("Manifest deleted successfully!")
        logging.info("-----")
        logging.info("Note: The GCS bucket '%s' was NOT deleted (user-managed).", gcs_bucket)
        logging.info("Warning: Persistent volumes may need to be deleted manually in GCP console.")
        logging.info("-----")

    def prepare_setup_cluster(self, output_dir, storage_size="10Gi", disk_type="pd-standard", **kwargs):
        """Write the pod manifest and a README for manual deployment.

        Args:
            output_dir (str): Directory where setup files will be written.
            storage_size (str): Size of the ephemeral PVC.
            disk_type (str): GCP PD type for the StorageClass.
            **kwargs: Must include ``gcs_bucket``, ``gcs_access_key``,
                ``gcs_secret_key``.
        """
        storage_size = kwargs.get("storage_size", storage_size)
        disk_type = kwargs.get("disk_type", disk_type)
        gcs_bucket, gcs_access_key, gcs_secret_key, gcs_key_file = self._extract_gcs_params(kwargs)
        self._store_gcs_params(gcs_bucket, gcs_access_key, gcs_secret_key, gcs_key_file)

        manifest_yaml = self._render_manifest(
            storage_size, disk_type, gcs_bucket, gcs_access_key, gcs_secret_key,
        )
        with open(f"{output_dir}/robovast-manifest.yaml", "w") as f:
            f.write(manifest_yaml)

        readme_content = f"""# GCP Cluster Setup Instructions

Uses **Google Cloud Storage** (S3-compatible interface) for campaign data.
No embedded MinIO server is deployed.

- **GCS bucket:** `{gcs_bucket}`
- **S3 endpoint:** `{GCS_S3_ENDPOINT}`
- **Storage PVC:** {storage_size}, type `{disk_type}` (for archiver/nginx scratch space)

## Setup Steps

### 1. Apply the RoboVAST Manifest

```bash
kubectl apply -f robovast-manifest.yaml
```

### 2. Wait for the pod to be ready

```bash
kubectl wait --for=condition=ready pod/robovast --timeout=120s
```

The HTTP server (nginx) serves archive downloads on port 9998.
The archiver sidecar streams GCS bucket contents to tar.gz in /data.
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

    # ------------------------------------------------------------------
    # GCS-specific helpers
    # ------------------------------------------------------------------

    def get_storage_backend(self) -> str:
        """Return ``'gcs'`` to indicate native GCS storage (not the S3-compat path)."""
        return "gcs"

    def get_gcs_key_file(self) -> Optional[str]:
        """Return the path to the GCS service-account key JSON file, or ``None``."""
        return self._gcs_key_file

    def get_gcs_key_json(self) -> str:
        """Return the contents of the GCS service-account key JSON file.

        Resolution order:

        1. ``ROBOVAST_GCS_KEY_JSON`` environment variable (inline JSON string — can be
           set in a ``.env`` file next to the project config).
        2. The path stored in ``self._gcs_key_file`` (set via ``-o
           gcs_key_file=`` or ``ROBOVAST_GCS_KEY_FILE`` env / ``.env`` entry).

        Returns:
            str: JSON string of the service-account key.

        Raises:
            RuntimeError: If neither source is configured.
        """
        # 1. Inline JSON from environment / .env
        key_json_env = os.environ.get("ROBOVAST_GCS_KEY_JSON")
        if key_json_env:
            return key_json_env

        # 2. Path to key file (instance attr or ROBOVAST_GCS_KEY_FILE env / .env)
        key_file = self._gcs_key_file or os.environ.get("ROBOVAST_GCS_KEY_FILE")
        if key_file:
            with open(key_file, encoding="utf-8") as fh:
                return fh.read()

        raise RuntimeError(
            "GCS key JSON not configured. "
            "Set ROBOVAST_GCS_KEY_JSON (inline JSON) or ROBOVAST_GCS_KEY_FILE (path) in your .env file, "
            "or pass gcs_key_file via -o."
        )

    @staticmethod
    def _validate_gcs_bucket(bucket_name, access_key, secret_key):
        """Validate that the GCS bucket exists and is accessible.

        Raises:
            RuntimeError: If the bucket is not reachable.
        """
        import boto3  # pylint: disable=import-outside-toplevel
        from botocore.client import Config as BotoConfig  # pylint: disable=import-outside-toplevel
        from botocore.exceptions import ClientError  # pylint: disable=import-outside-toplevel

        s3 = boto3.client(
            "s3",
            endpoint_url=GCS_S3_ENDPOINT,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=BotoConfig(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
            region_name="auto",
        )
        try:
            s3.head_bucket(Bucket=bucket_name)
            logging.info("GCS bucket '%s' is accessible.", bucket_name)
        except ClientError as e:
            code = e.response["Error"]["Code"]
            raise RuntimeError(
                f"Cannot access GCS bucket '{bucket_name}' (error {code}). "
                f"Ensure the bucket exists and the HMAC credentials are correct."
            ) from e
