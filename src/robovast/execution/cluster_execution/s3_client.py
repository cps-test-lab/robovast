#!/usr/bin/env python3
# Copyright (C) 2026 Frederik Pasch
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

import logging
import os
import signal
import sys
import socket
import subprocess
import tempfile
import time
from typing import Optional

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

S3_PORT = 9000


def _format_size(num_bytes: int) -> str:
    """Return a human-readable file size string."""
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


def _find_available_port(start_port=18080, max_attempts=100):
    """Find an available local port."""
    for port in range(start_port, start_port + max_attempts):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(('localhost', port))
                return port
        except OSError:
            continue
    raise RuntimeError(f"Could not find an available port in range {start_port}-{start_port + max_attempts}")


class ClusterS3Client:
    """Manages a kubectl port-forward to the robovast MinIO pod and exposes boto3-backed S3 operations.

    This class is intended for use on the host side (outside the cluster).
    Within the cluster, containers reach MinIO directly via the service DNS name.

    Future extension: pass a custom endpoint/credentials to connect to an external S3 service
    instead of port-forwarding to the embedded MinIO pod.
    """

    def __init__(self, namespace="default", endpoint=None, access_key="minioadmin", secret_key="minioadmin", context=None):
        """Create a ClusterS3Client.

        Args:
            namespace (str): Kubernetes namespace of the robovast pod.
            endpoint (str): Override S3 endpoint URL. If None, a kubectl port-forward is used.
            access_key (str): S3 access key.
            secret_key (str): S3 secret key.
            context (str): Kubernetes context to use. None uses the active context.
        """
        self.namespace = namespace
        self.access_key = access_key
        self.secret_key = secret_key
        self.context = context
        self._port_forward_process = None
        self._s3 = None

        if endpoint:
            self._endpoint_url = endpoint
            self._owns_port_forward = False
        else:
            self._local_port = _find_available_port()
            self._endpoint_url = f"http://localhost:{self._local_port}"
            self._owns_port_forward = True

        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.close()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect(self):
        """Start port-forward (if needed) and create the boto3 S3 client."""
        if self._owns_port_forward:
            self._start_port_forward()
        self._s3 = boto3.client(
            "s3",
            endpoint_url=self._endpoint_url,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            config=Config(signature_version="s3v4"),
            region_name="us-east-1",
        )
        logger.debug(f"Connected to S3 at {self._endpoint_url}")

    def close(self):
        """Terminate port-forward process if owned by this client."""
        if self._port_forward_process:
            logger.debug("Terminating port-forward...")
            self._port_forward_process.terminate()
            try:
                self._port_forward_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._port_forward_process.kill()
            self._port_forward_process = None

    def _signal_handler(self, signum, frame):
        logger.info("\nCleaning up S3 client...")
        self.close()
        sys.exit(0)

    def _start_port_forward(self):
        """Start kubectl port-forward to robovast:9000."""
        logger.debug(
            f"Starting port-forward robovast:{S3_PORT} -> localhost:{self._local_port}"
        )
        ctx_args = ["--context", self.context] if self.context else []
        cmd = [
            "kubectl",
        ] + ctx_args + [
            "port-forward",
            "-n", self.namespace,
            "pod/robovast",
            f"{self._local_port}:{S3_PORT}",
        ]
        self._port_forward_process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        # Wait for port-forward to be ready
        for _ in range(20):
            time.sleep(0.5)
            try:
                with socket.create_connection(("localhost", self._local_port), timeout=1):
                    break
            except OSError:
                continue
        else:
            raise RuntimeError(
                f"kubectl port-forward to robovast:{S3_PORT} did not become ready in time"
            )
        logger.debug("Port-forward established.")

    # ------------------------------------------------------------------
    # Bucket operations
    # ------------------------------------------------------------------

    def create_bucket(self, bucket_name: str):
        """Create an S3 bucket.

        Args:
            bucket_name (str): Name of the bucket to create.
        """
        try:
            self._s3.create_bucket(Bucket=bucket_name)
            logger.debug(f"Created bucket: {bucket_name}")
        except ClientError as e:
            if e.response["Error"]["Code"] in ("BucketAlreadyExists", "BucketAlreadyOwnedByYou"):
                logger.debug(f"Bucket already exists: {bucket_name}")
            else:
                raise

    def bucket_exists(self, bucket_name: str) -> bool:
        """Return True if the named bucket already exists."""
        try:
            self._s3.head_bucket(Bucket=bucket_name)
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchBucket"):
                return False
            raise

    def list_run_buckets(self) -> list:
        """List all buckets whose names start with 'run-'.

        Returns:
            list[str]: Sorted list of matching bucket names.
        """
        response = self._s3.list_buckets()
        buckets = [b["Name"] for b in response.get("Buckets", []) if b["Name"].startswith("run-")]
        return sorted(buckets)

    def cleanup_run_buckets(self, run_id: Optional[str] = None) -> int:
        """Remove run buckets from S3 without downloading them.

        Args:
            run_id: If provided, only the bucket with this exact name is removed.
                    If None, all run buckets (run-*) are removed.

        Returns:
            int: Number of buckets successfully removed.
        """
        all_runs = self.list_run_buckets()
        if run_id:
            buckets_to_remove = [b for b in all_runs if b == run_id]
            if not buckets_to_remove:
                logger.info(f"No bucket matching '{run_id}' found.")
                return 0
        else:
            buckets_to_remove = all_runs

        if not buckets_to_remove:
            logger.info("No run buckets found to remove.")
            return 0

        removed_count = 0
        for bucket_name in buckets_to_remove:
            try:
                self.delete_bucket(bucket_name)
                removed_count += 1
                logger.info(f"Removed '{bucket_name}' from S3")
            except Exception as e:
                logger.error(f"Failed to remove '{bucket_name}': {e}")

        return removed_count

    # ------------------------------------------------------------------
    # Upload / download
    # ------------------------------------------------------------------

    def upload_directory(self, bucket_name: str, local_dir: str, s3_prefix: str = ""):
        """Recursively upload all files in local_dir to the bucket under s3_prefix.

        Args:
            bucket_name (str): Target bucket.
            local_dir (str): Local directory to upload.
            s3_prefix (str): S3 key prefix (no leading slash). Empty string uploads to bucket root.
        """
        if not os.path.isdir(local_dir):
            raise FileNotFoundError(f"Directory not found: {local_dir}")

        uploaded = 0
        for root, _dirs, files in os.walk(local_dir):
            for filename in files:
                local_path = os.path.join(root, filename)
                relative = os.path.relpath(local_path, local_dir)
                s3_key = os.path.join(s3_prefix, relative).lstrip("/") if s3_prefix else relative
                # Always use forward slashes in S3 keys
                s3_key = s3_key.replace(os.sep, "/")
                logger.debug(f"Uploading {local_path} -> s3://{bucket_name}/{s3_key}")
                self._s3.upload_file(local_path, bucket_name, s3_key)
                uploaded += 1

        logger.debug(f"Uploaded {uploaded} file(s) to s3://{bucket_name}/{s3_prefix}")

    def delete_bucket(self, bucket_name: str):
        """Delete all objects in a bucket, then delete the bucket itself.

        Args:
            bucket_name (str): Bucket to remove.
        """
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket_name):
            objects = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
            if objects:
                self._s3.delete_objects(Bucket=bucket_name, Delete={"Objects": objects})
        self._s3.delete_bucket(Bucket=bucket_name)
        logger.debug(f"Deleted bucket: {bucket_name}")

    def download_bucket(self, bucket_name: str, local_dir: str, force: bool = False,
                        progress_callback=None):
        """Download all objects from a bucket into local_dir/{bucket_name}/.

        Args:
            bucket_name (str): Source bucket.
            local_dir (str): Local base directory. Files are placed in local_dir/bucket_name/.
            force (bool): Re-download files that already exist locally.
            progress_callback: Optional callable ``(current, total, size_bytes)`` invoked after
                each file is downloaded. When *None* each file is logged via ``logger.info``.

        Returns:
            int: Number of files downloaded.
        """
        dest_dir = os.path.join(local_dir, bucket_name)
        os.makedirs(dest_dir, exist_ok=True)

        paginator = self._s3.get_paginator("list_objects_v2")

        objects = []
        for page in paginator.paginate(Bucket=bucket_name):
            objects.extend(page.get("Contents", []))

        total = len(objects)
        downloaded = 0

        for obj in objects:
            key = obj["Key"]
            size_bytes = obj.get("Size", 0)
            local_path = os.path.join(dest_dir, key.replace("/", os.sep))

            if not force and os.path.exists(local_path):
                logger.debug(f"Skipping existing file: {local_path}")
                continue

            os.makedirs(os.path.dirname(local_path), exist_ok=True)

            if progress_callback is None:
                downloaded += 1
                size_str = _format_size(size_bytes)
                logger.info(f"  [{downloaded}/{total}] {key} ({size_str})")
                self._s3.download_file(bucket_name, key, local_path)
            else:
                self._s3.download_file(bucket_name, key, local_path)
                downloaded += 1
                progress_callback(downloaded, total, size_bytes)

        logger.debug(f"Downloaded {downloaded} file(s) from s3://{bucket_name}")
        return downloaded


# ---------------------------------------------------------------------------
# Higher-level helpers
# ---------------------------------------------------------------------------

def upload_configs_to_s3(config_dir: str, bucket_name: str, cluster_config, namespace: str = "default", context: str = None) -> None:
    """Upload a prepared config directory to an S3 bucket.

    Creates the bucket (if absent) and uploads the entire *config_dir* tree to the
    bucket root, preserving relative paths.

    Args:
        config_dir: Local directory containing generated config files.
        bucket_name: S3 bucket name (e.g. ``'run-20260220-123456'``).
        cluster_config: BaseConfig instance providing S3 endpoint/credentials.
        namespace: Kubernetes namespace used for port-forwarding.
        context: Kubernetes context to use. None uses the active context.
    """
    if not os.path.isdir(config_dir):
        raise FileNotFoundError(f"Config directory does not exist: {config_dir}")

    access_key, secret_key = cluster_config.get_s3_credentials()
    logger.debug(f"Uploading config files to s3://{bucket_name}/ ...")
    try:
        with ClusterS3Client(namespace=namespace, access_key=access_key, secret_key=secret_key, context=context) as s3:
            s3.create_bucket(bucket_name)
            s3.upload_directory(bucket_name, config_dir)
        logger.debug(f"Successfully uploaded all config files to s3://{bucket_name}/")
    except Exception as e:
        logger.error(f"Failed to upload config files to S3: {e}")
        sys.exit(1)


def upload_run_configs(run_id: str, run_data: dict, num_runs: int, cluster_config, namespace: str = "default", context: str = None) -> None:
    """Prepare run config files and upload them to an S3 bucket.

    Opens a **single** port-forward for both the existence check and the upload.
    Raises :class:`RuntimeError` if the bucket derived from *run_id* already exists
    rather than silently appending a numeric suffix.

    Args:
        run_id: Run identifier (e.g. ``'run-2026-03-01-120000'``).
        run_data: Scenario variation data produced by ``generate_scenario_variations``.
        num_runs: Number of runs (used by ``create_execution_yaml``).
        cluster_config: BaseConfig instance providing S3 credentials and optional
                        ``get_instance_type_command()``.
        namespace: Kubernetes namespace.
        context: Kubernetes context to use. None uses the active context.

    Raises:
        RuntimeError: If an S3 bucket for *run_id* already exists.
    """
    # Inline imports to avoid circular dependencies at module load time.
    from robovast.common import create_execution_yaml, prepare_run_configs  # pylint: disable=import-outside-toplevel

    access_key, secret_key = cluster_config.get_s3_credentials()

    with ClusterS3Client(namespace=namespace, access_key=access_key, secret_key=secret_key, context=context) as s3:
        bucket_name = run_id.lower().replace("_", "-")
        if s3.bucket_exists(bucket_name):
            raise RuntimeError(
                f"S3 bucket '{bucket_name}' already exists. "
                f"A run with ID '{run_id}' is already in progress or was not cleaned up. "
                f"Clean up the existing run first or wait until the next second."
            )

        # Prepare config files and upload inside the same port-forward session.
        with tempfile.TemporaryDirectory() as temp_dir:
            out_dir = os.path.join(temp_dir, "out_template")
            prepare_run_configs(out_dir, run_data, cluster=True)

            # Inject instance-type detection command into entrypoint.sh when supported.
            entrypoint_path = os.path.join(out_dir, "entrypoint.sh")
            try:
                instance_type_cmd = None
                if hasattr(cluster_config, "get_instance_type_command"):
                    instance_type_cmd = cluster_config.get_instance_type_command()
                if instance_type_cmd and os.path.exists(entrypoint_path):
                    with open(entrypoint_path, "r", encoding="utf-8") as f:
                        content = f.read()
                    placeholder = 'INSTANCE_TYPE=""'
                    if placeholder in content:
                        content = content.replace(placeholder, instance_type_cmd, 1)
                        with open(entrypoint_path, "w", encoding="utf-8") as f:
                            f.write(content)
            except Exception as exc:  # pragma: no cover – best-effort, non-fatal
                logger.warning(f"Could not inject instance type command into entrypoint.sh: {exc}")

            create_execution_yaml(num_runs, out_dir, execution_params=run_data.get("execution", {}))

            logger.info(f"Uploading config files to S3 bucket '{bucket_name}'...")
            s3.create_bucket(bucket_name)
            s3.upload_directory(bucket_name, out_dir)
