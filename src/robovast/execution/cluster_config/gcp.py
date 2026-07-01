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
with HMAC credentials.  A ``robovast`` helper pod (archiver sidecar)
is still deployed for archive workflows.

Required ``-o`` options at ``setup`` time::

    vast execution cluster setup gcp \\
        -o gcs_bucket=<BUCKET_NAME> \\
        -o gcs_access_key=<HMAC_ACCESS_KEY> \\
        -o gcs_secret_key=<HMAC_SECRET_KEY> \\
        [-o storage_size=50Gi] [-o disk_type=pd-ssd]
"""
import concurrent.futures
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from kubernetes import client, config

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
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config(context=kube_context)

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


GCS_S3_ENDPOINT = "https://storage.googleapis.com"

# No Kubernetes helper pod is deployed on GCP. Campaign data lives directly in
# the user's GCS bucket; the controller pod compresses and uploads it in-process
# (see :mod:`..cluster_execution.in_pod_upload`), so the former archiver sidecar
# pod (and its ephemeral PVC / StorageClass) is no longer needed.


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
        """Validate the GCS bucket for a GCP cluster. No helper pod is deployed.

        Campaign data lives directly in the user's GCS bucket, and the
        controller pod compresses + uploads it in-process, so GCP setup only
        validates that the bucket is reachable with the provided HMAC
        credentials (when given).

        Args:
            storage_size (str): Unused (kept for CLI compatibility).
            disk_type (str): Unused (kept for CLI compatibility).
            **kwargs: Must include ``gcs_bucket`` and either HMAC credentials
                (``gcs_access_key`` / ``gcs_secret_key``) or ``gcs_key_file``.
        """
        del storage_size, disk_type  # no PVC / StorageClass any more
        gcs_bucket, gcs_access_key, gcs_secret_key, gcs_key_file = self._extract_gcs_params(kwargs)
        self._store_gcs_params(gcs_bucket, gcs_access_key, gcs_secret_key, gcs_key_file)

        logging.info("Setting up RoboVAST for GCP (GCS S3 interface)...")
        logging.info(f"GCS bucket: {gcs_bucket}")

        # Validate GCS connectivity — only possible with HMAC credentials;
        # when using a service-account key file the caller is responsible for
        # ensuring the bucket exists.
        if gcs_access_key and gcs_secret_key:
            self._validate_gcs_bucket(gcs_bucket, gcs_access_key, gcs_secret_key)
        logging.info("GCP cluster ready (no helper pod required).")

    def cleanup_cluster(self, storage_size="10Gi", disk_type="pd-standard", **kwargs):
        """No-op for GCP: there is no helper pod to remove; the bucket is kept.

        Args:
            storage_size (str): Unused (kept for CLI compatibility).
            disk_type (str): Unused (kept for CLI compatibility).
            **kwargs: GCS options (restored from the flag file).
        """
        del storage_size, disk_type
        gcs_bucket, gcs_access_key, gcs_secret_key, gcs_key_file = self._extract_gcs_params(kwargs)
        self._store_gcs_params(gcs_bucket, gcs_access_key, gcs_secret_key, gcs_key_file)
        logging.info("Nothing to clean up for GCP (no helper pod is deployed).")
        logging.info("Note: The GCS bucket '%s' was NOT deleted (user-managed).", gcs_bucket)

    def prepare_setup_cluster(self, output_dir, storage_size="10Gi", disk_type="pd-standard", **kwargs):
        """Write a README for GCP. No manifest is generated (no helper pod).

        Args:
            output_dir (str): Directory where setup files will be written.
            storage_size (str): Unused (kept for CLI compatibility).
            disk_type (str): Unused (kept for CLI compatibility).
            **kwargs: Must include ``gcs_bucket`` and credentials.
        """
        del storage_size, disk_type
        kwargs.pop('control_node_labels', None)
        gcs_bucket, gcs_access_key, gcs_secret_key, gcs_key_file = self._extract_gcs_params(kwargs)
        self._store_gcs_params(gcs_bucket, gcs_access_key, gcs_secret_key, gcs_key_file)

        readme_content = f"""# GCP Cluster Setup Instructions

Uses **Google Cloud Storage** (S3-compatible interface) for campaign data.
No embedded MinIO server and no helper pod are deployed — the in-cluster
controller pod compresses and uploads campaigns directly.

- **GCS bucket:** `{gcs_bucket}`
- **S3 endpoint:** `{GCS_S3_ENDPOINT}`

## Setup Steps

No Kubernetes resources need to be applied for storage. Ensure the GCS bucket
`{gcs_bucket}` exists and the credentials (HMAC keys or a service-account key
file) have read/write access to it.
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

    def compress_campaign(self, campaign_id: str, archive_dir: str) -> str:
        """Compress the campaign from GCS into ``<archive_dir>/<campaign_id>.tar.gz``.

        Overrides the S3 default with the native GCS download+compress path
        (parallel download, then ``tar | pigz``). See :func:`_gcs_compress`.
        """
        return _gcs_compress(
            campaign_id, self.get_s3_bucket(), self.get_gcs_key_json(), archive_dir)

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

    # ------------------------------------------------------------------
    # Autoscaling-aware resource quota
    # ------------------------------------------------------------------

    def get_cluster_allocatable_resources(self, kube_context=None):
        """Return GKE autoscaler **max** capacity for Kueue quota.

        Queries ``gcloud container clusters describe`` to obtain each node
        pool's autoscaling *maxNodeCount* and machine type, then multiplies
        by the vCPU / memory figures from ``gcloud compute machine-types
        describe``.  This gives the true upper bound even when the cluster
        is currently scaled down.

        Falls back to ``(None, None)`` (K8s node API query in
        ``kubernetes_kueue``) when:

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


# ---------------------------------------------------------------------------
# Native GCS download + compress, used by GcpClusterConfig.compress_campaign for
# the controller's upload-to-share step. Kept here (GCS is only used by this
# config) rather than in a shared module. Parallel download then ``tar | pigz``.
# ---------------------------------------------------------------------------

_GCS_DEFAULT_WORKERS = int(os.environ.get("ROBOVAST_GCS_WORKERS", "16"))
_GCS_CHUNK = 4 * 1024 * 1024  # 4 MiB per read chunk


def _gcs_get_access_token(key_json: dict) -> str:
    """Exchange a service-account key dict for a short-lived Bearer token."""
    try:
        import google.auth.transport.requests  # pylint: disable=import-outside-toplevel
        import google.oauth2.service_account  # pylint: disable=import-outside-toplevel
    except ImportError as exc:
        raise RuntimeError(f"google-auth is not installed: {exc}") from exc
    scopes = ["https://www.googleapis.com/auth/devstorage.read_only"]
    creds = google.oauth2.service_account.Credentials.from_service_account_info(
        key_json, scopes=scopes)
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


def _gcs_list_blobs(bucket: str, prefix: str, token: str) -> list:
    """Return ``(name, size)`` tuples for every object under *prefix*."""
    blobs = []
    page_token = None
    while True:
        params: dict = {"prefix": prefix}
        if page_token:
            params["pageToken"] = page_token
        url = (
            f"https://storage.googleapis.com/storage/v1/b/"
            f"{urllib.parse.quote(bucket, safe='')}/o"
            f"?{urllib.parse.urlencode(params)}"
        )
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310 - https GCS API
                data = json.load(resp)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"GCS list failed for bucket '{bucket}' prefix '{prefix}': "
                f"HTTP {exc.code} {exc.reason}"
            ) from exc
        for item in data.get("items", []):
            blobs.append((item["name"], int(item.get("size", 0))))
        next_page = data.get("nextPageToken")
        if not next_page:
            break
        page_token = next_page
    return blobs


def _gcs_download_blob(bucket: str, blob_name: str, dest_path: str, token: str) -> None:
    """Download one GCS object to *dest_path*, streaming in 4 MiB chunks."""
    url = (
        f"https://storage.googleapis.com/storage/v1/b/"
        f"{urllib.parse.quote(bucket, safe='')}/o/"
        f"{urllib.parse.quote(blob_name, safe='')}"
        f"?alt=media"
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    with urllib.request.urlopen(req, timeout=120) as resp:  # nosec B310 - https GCS API
        with open(dest_path, "wb") as fh:
            while True:
                chunk = resp.read(_GCS_CHUNK)
                if not chunk:
                    break
                fh.write(chunk)


def _gcs_create_job_links(campaign_dir: str) -> None:
    """Materialise ``<config>/<run>/job`` symlinks from the link manifest.

    Reads ``<campaign_dir>/_transient/job_links.yaml`` (a ``{link: target}`` map
    written by robovast for packed campaigns) and creates each relative symlink
    so the archived tree is navigable. No-op when the manifest is absent.
    """
    import yaml  # pylint: disable=import-outside-toplevel
    manifest = os.path.join(campaign_dir, "_transient", "job_links.yaml")
    if not os.path.isfile(manifest):
        return
    with open(manifest) as fh:
        links = yaml.safe_load(fh) or {}
    for link_rel, target in links.items():
        link_path = os.path.join(campaign_dir, link_rel)
        os.makedirs(os.path.dirname(link_path), exist_ok=True)
        if os.path.islink(link_path) or os.path.exists(link_path):
            try:
                os.remove(link_path)
            except OSError:
                pass
        try:
            os.symlink(target, link_path)
        except OSError as exc:
            sys.stderr.write(f"WARNING: could not create job link {link_rel}: {exc}\n")


def _gcs_compress(campaign: str, bucket: str, key_json: str, archive_dir: str,
                  *, workers: int = _GCS_DEFAULT_WORKERS) -> str:
    """Download ``<campaign>/`` from *bucket* and compress it to a tar.gz.

    Args:
        campaign: Campaign id (GCS key prefix and the tar's top-level folder).
        bucket: GCS bucket name.
        key_json: Service-account key as a JSON string.
        archive_dir: Directory to write ``<campaign>.tar.gz`` (and scratch) into.
        workers: Parallel download threads.

    Returns the output tar.gz path.
    """
    key_data = json.loads(key_json)
    prefix = f"{campaign}/"
    output_path = os.path.join(archive_dir, f"{campaign}.tar.gz")

    token = _gcs_get_access_token(key_data)
    blobs = [(name, size) for name, size in _gcs_list_blobs(bucket, prefix, token)
             if name != prefix]
    if not blobs:
        raise RuntimeError(
            f"No objects found under prefix '{prefix}' in bucket '{bucket}'.")

    total = len(blobs)
    total_bytes = sum(size for _, size in blobs)
    sys.stdout.write(
        f"{campaign}: {total} object(s)  {total_bytes / 1024 / 1024:.1f} MiB"
        f"  ({workers} parallel workers)\n")
    sys.stdout.flush()

    # Phase 1: parallel download into a temp directory under the archive dir.
    tmpdir = tempfile.mkdtemp(dir=archive_dir, prefix=f".gcs_dl_{campaign}_")
    try:
        campaign_dir = os.path.join(tmpdir, campaign)
        os.makedirs(campaign_dir, exist_ok=True)

        done_count = 0
        lock = threading.Lock()

        def _download_one(blob_name_size):
            nonlocal done_count
            blob_name, _size = blob_name_size
            relative = blob_name[len(prefix):]
            if not relative:
                return
            _gcs_download_blob(bucket, blob_name, os.path.join(campaign_dir, relative), token)
            with lock:
                done_count += 1
                n = done_count
            sys.stdout.write(f"\r{campaign}  downloading {n}/{total}...")
            sys.stdout.flush()

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_download_one, b): b for b in blobs}
            for fut in concurrent.futures.as_completed(futures):
                fut.result()  # re-raise any download exception immediately

        sys.stdout.write(f"\r{campaign}  downloaded {total} file(s), compressing...\n")
        sys.stdout.flush()

        # Materialise per-job artifact symlinks before archiving so the tar.gz
        # carries them (tar stores symlinks as links — no -h/--dereference).
        _gcs_create_job_links(campaign_dir)

        # Phase 2: tar + pigz running in parallel via OS pipe.
        with open(output_path, "wb") as out_f:
            tar_proc = subprocess.Popen(  # nosec B607 B603 - fixed binary, no shell
                ["tar", "cf", "-", "-C", tmpdir, campaign],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            pigz_proc = subprocess.Popen(  # nosec B607 B603 - fixed binary, no shell
                ["pigz", "-c"], stdin=tar_proc.stdout, stdout=out_f,
                stderr=subprocess.PIPE)
            tar_proc.stdout.close()  # let tar receive SIGPIPE if pigz exits early
            _pigz_stderr = pigz_proc.communicate()[1]
            tar_proc.wait()

        if tar_proc.returncode != 0:
            raise RuntimeError(f"tar exited with code {tar_proc.returncode}")
        if pigz_proc.returncode != 0:
            msg = _pigz_stderr.decode(errors="replace").strip()
            raise RuntimeError(f"pigz exited with code {pigz_proc.returncode}: {msg}")

        sys.stdout.write(f"{campaign}: wrote {output_path}\n")
        sys.stdout.flush()
        return output_path
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
