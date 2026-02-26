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

import logging
import os
import sys
import time
from typing import Callable, Optional, Tuple

from kubernetes import client, config

from .s3_client import ClusterS3Client

logger = logging.getLogger(__name__)

_BAR_WIDTH = 20
_FILE_COUNT_WIDTH = 6
_PCT_WIDTH = 7     # e.g. " 34.2%"
_SIZE_WIDTH = 11   # e.g. "  156.23 MB"
_RATE_WIDTH = 12   # e.g. "   8.12 MB/s"


def _format_size(num_bytes: int) -> str:
    if num_bytes == 0:
        return "0 B".rjust(_SIZE_WIDTH)
    val = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if val < 1024:
            return f"{val:.2f} {unit}".rjust(_SIZE_WIDTH)
        val /= 1024
    return f"{val:.2f} TB".rjust(_SIZE_WIDTH)


def _format_rate(bytes_per_sec: float) -> str:
    """Return a human-readable throughput string (e.g. '  8.12 MB/s')."""
    if bytes_per_sec <= 0:
        return "0 B/s".rjust(_RATE_WIDTH)
    val = bytes_per_sec
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if val < 1024:
            return f"{val:.2f} {unit}".rjust(_RATE_WIDTH)
        val /= 1024
    return f"{val:.2f} TB/s".rjust(_RATE_WIDTH)


def _make_progress_callback(prefix: str, bucket_name: str) -> Tuple[Callable, Callable, Callable]:
    """Return ``(callback, get_total_bytes, get_elapsed)`` for single-line progress display.

    The callback signature is ``(current, total, file_size_bytes)`` and updates
    the current terminal line in place using ``\\r``.  ``get_total_bytes()``
    returns the cumulative downloaded byte count.  ``get_elapsed()`` returns
    elapsed seconds since the first callback (or 0 if none yet).
    """
    state: dict = {"total_bytes": 0, "start_time": None}

    def callback(current: int, total: int, size_bytes: int) -> None:
        if state["start_time"] is None:
            state["start_time"] = time.monotonic()
        state["total_bytes"] += size_bytes
        filled = int(_BAR_WIDTH * current / total) if total > 0 else _BAR_WIDTH
        progress_bar = "█" * filled + "░" * (_BAR_WIDTH - filled)
        size_str = _format_size(state["total_bytes"])
        elapsed = time.monotonic() - state["start_time"]
        rate_str = _format_rate(state["total_bytes"] / elapsed) if elapsed > 0 else _format_rate(0)
        files_str = f"{current:0{_FILE_COUNT_WIDTH}d}/{total:0{_FILE_COUNT_WIDTH}d}"
        pct = 100.0 * current / total if total > 0 else 100.0
        pct_str = f"{pct:.1f}%".rjust(_PCT_WIDTH)
        sys.stdout.write(
            f"\r{prefix} {bucket_name}  [{progress_bar}]  {pct_str}  {files_str}  {size_str}  {rate_str}   "
        )
        sys.stdout.flush()

    def get_total_bytes() -> int:
        return state["total_bytes"]

    def get_elapsed() -> float:
        if state["start_time"] is None:
            return 0.0
        return time.monotonic() - state["start_time"]

    return callback, get_total_bytes, get_elapsed


class ResultDownloader:
    """Downloads run results from the MinIO S3 server embedded in the robovast pod."""

    def __init__(self, namespace="default", cluster_config=None):
        """Create a ResultDownloader.

        Args:
            namespace (str): Kubernetes namespace of the robovast pod.
            cluster_config: BaseConfig instance providing S3 endpoint/credentials.
                            If None, default embedded MinIO credentials are used.
        """
        self.namespace = namespace

        if cluster_config is not None:
            self.access_key, self.secret_key = cluster_config.get_s3_credentials()
        else:
            self.access_key = "minioadmin"
            self.secret_key = "minioadmin"

        # Verify the robovast pod is accessible
        self._check_robovast_pod()

    def _check_robovast_pod(self):
        """Exit with an informative error if the robovast pod is not running."""
        try:
            config.load_kube_config()
            core_v1 = client.CoreV1Api()
            pod = core_v1.read_namespaced_pod(name="robovast", namespace=self.namespace)
            if pod.status.phase not in ("Running", "Pending"):
                logger.error(
                    f"Pod 'robovast' exists but is not running (phase: {pod.status.phase})"
                )
                sys.exit(1)
        except client.exceptions.ApiException as e:
            if e.status == 404:
                logger.error("Required pod 'robovast' does not exist. Run 'vast execution cluster setup' first.")
                sys.exit(1)
            raise

    def list_available_runs(self, s3_client: ClusterS3Client) -> list:
        """Return sorted list of bucket names that represent completed runs.

        Args:
            s3_client: Connected ClusterS3Client instance.

        Returns:
            list[str]: Bucket names matching 'run-*'.
        """
        buckets = s3_client.list_run_buckets()
        if buckets:
            logger.debug(f"Available runs: {buckets}")
        return buckets

    def download_results(self, output_directory: str, force: bool = False,
                         verbose: bool = False) -> int:
        """Download all run results from MinIO to output_directory.

        Each run bucket ('run-*') is downloaded into a subdirectory named after
        the bucket inside output_directory.

        Args:
            output_directory (str): Local directory where results are written.
            force (bool): Re-download files that already exist locally.
            verbose (bool): Print per-file progress instead of a single-line bar.

        Returns:
            int: Number of buckets successfully downloaded.
        """
        os.makedirs(output_directory, exist_ok=True)

        with ClusterS3Client(
            namespace=self.namespace,
            access_key=self.access_key,
            secret_key=self.secret_key,
        ) as s3:
            available_runs = self.list_available_runs(s3)
            if not available_runs:
                logger.info("No runs found to download.")
                return 0

            logger.info(
                f"Downloading {len(available_runs)} run(s) to '{output_directory}'..."
            )

            downloaded_count = 0
            total_runs = len(available_runs)
            for idx, bucket_name in enumerate(available_runs, start=1):
                run_dir = os.path.join(output_directory, bucket_name)
                prefix = f"[{idx}/{total_runs}]"

                if not force and os.path.exists(run_dir) and os.listdir(run_dir):
                    if verbose:
                        logger.info(
                            f"{prefix} '{bucket_name}' already exists locally, skipping "
                            "(use --force to re-download)"
                        )
                    else:
                        print(f"{prefix} {bucket_name}  skipped (already exists locally)")
                    downloaded_count += 1
                    continue

                try:
                    if verbose:
                        logger.info(f"{prefix} Downloading '{bucket_name}'...")
                        progress_callback = None
                        get_total_bytes: Optional[Callable] = None
                    else:
                        sys.stdout.write(f"\r{prefix} {bucket_name}  downloading...   ")
                        sys.stdout.flush()
                        progress_callback, get_total_bytes, get_elapsed = _make_progress_callback(
                            prefix, bucket_name
                        )

                    count = s3.download_bucket(
                        bucket_name, output_directory, force=force,
                        progress_callback=progress_callback,
                    )

                    if verbose:
                        logger.info(f"{prefix} Downloaded {count} file(s) for '{bucket_name}'")
                    else:
                        progressbar = "█" * _BAR_WIDTH
                        total_bytes = get_total_bytes()
                        size_str = _format_size(total_bytes)
                        elapsed = get_elapsed()
                        rate_str = (
                            _format_rate(total_bytes / elapsed) if elapsed > 0 else _format_rate(0)
                        )
                        files_str = f"{count:0{_FILE_COUNT_WIDTH}d}"
                        pct_str = "100.0%".rjust(_PCT_WIDTH)
                        sys.stdout.write(
                            f"\r{prefix} {bucket_name}  [{progressbar}]  {pct_str}  {files_str} files  "
                            f"{size_str}  {rate_str}  done\n"
                        )
                        sys.stdout.flush()

                    downloaded_count += 1
                    s3.delete_bucket(bucket_name)
                    if verbose:
                        logger.info(f"{prefix} Removed '{bucket_name}' from S3")

                except Exception as e:
                    if verbose:
                        logger.error(f"{prefix} Failed to download '{bucket_name}': {e}")
                    else:
                        sys.stdout.write(f"\r{prefix} {bucket_name}  ERROR: {e}\n")
                        sys.stdout.flush()

        return downloaded_count
