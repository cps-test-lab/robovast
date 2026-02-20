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

from kubernetes import client, config

from .s3_client import ClusterS3Client

logger = logging.getLogger(__name__)


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
            self.s3_endpoint = cluster_config.get_s3_endpoint()
            self.access_key, self.secret_key = cluster_config.get_s3_credentials()
        else:
            self.s3_endpoint = None  # ClusterS3Client will port-forward
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

    def download_results(self, output_directory: str, force: bool = False) -> int:
        """Download all run results from MinIO to output_directory.

        Each run bucket ('run-*') is downloaded into a subdirectory named after
        the bucket inside output_directory.

        Args:
            output_directory (str): Local directory where results are written.
            force (bool): Re-download files that already exist locally.

        Returns:
            int: Number of buckets successfully downloaded.
        """
        os.makedirs(output_directory, exist_ok=True)

        with ClusterS3Client(
            namespace=self.namespace,
            endpoint=self.s3_endpoint,
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
            for bucket_name in available_runs:
                run_dir = os.path.join(output_directory, bucket_name)
                if not force and os.path.exists(run_dir) and os.listdir(run_dir):
                    logger.info(
                        f"Run '{bucket_name}' already exists locally, skipping "
                        "(use --force to re-download)"
                    )
                    downloaded_count += 1
                    continue

                try:
                    logger.info(f"Downloading '{bucket_name}'...")
                    count = s3.download_bucket(bucket_name, output_directory, force=force)
                    logger.info(f"Downloaded {count} file(s) for '{bucket_name}'")
                    downloaded_count += 1
                except Exception as e:
                    logger.error(f"Failed to download '{bucket_name}': {e}")

        return downloaded_count
