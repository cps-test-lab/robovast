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
import socket
import subprocess
import sys
import time

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

    def __init__(self, namespace="default", endpoint=None, access_key="minioadmin", secret_key="minioadmin"):
        """Create a ClusterS3Client.

        Args:
            namespace (str): Kubernetes namespace of the robovast pod.
            endpoint (str): Override S3 endpoint URL. If None, a kubectl port-forward is used.
            access_key (str): S3 access key.
            secret_key (str): S3 secret key.
        """
        self.namespace = namespace
        self.access_key = access_key
        self.secret_key = secret_key
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
        cmd = [
            "kubectl", "port-forward",
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

    def list_run_buckets(self) -> list:
        """List all buckets whose names start with 'run-'.

        Returns:
            list[str]: Sorted list of matching bucket names.
        """
        response = self._s3.list_buckets()
        buckets = [b["Name"] for b in response.get("Buckets", []) if b["Name"].startswith("run-")]
        return sorted(buckets)

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

    def download_bucket(self, bucket_name: str, local_dir: str, force: bool = False):
        """Download all objects from a bucket into local_dir/{bucket_name}/.

        Args:
            bucket_name (str): Source bucket.
            local_dir (str): Local base directory. Files are placed in local_dir/bucket_name/.
            force (bool): Re-download files that already exist locally.

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
            downloaded += 1
            size_str = _format_size(size_bytes)
            logger.info(f"  [{downloaded}/{total}] {key} ({size_str})")
            self._s3.download_file(bucket_name, key, local_path)

        logger.debug(f"Downloaded {downloaded} file(s) from s3://{bucket_name}")
        return downloaded
