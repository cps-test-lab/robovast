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
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import time

import requests
from kubernetes import client, config

logger = logging.getLogger(__name__)


class ResultDownloader:
    def __init__(self):
        self.port_forward_process = None
        self.local_port = self._find_available_port()
        self.remote_port = 80  # HTTP server port in sidecar

        # Initialize Kubernetes client
        config.load_kube_config()
        self.k8s_client = client.CoreV1Api()

        # Check if transfer-pod exists
        self.check_transfer_pod_exists()

        # Set up signal handler for cleanup
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _find_available_port(self, start_port=8080, max_attempts=100):
        """Find an available port starting from start_port"""
        for port in range(start_port, start_port + max_attempts):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.bind(('localhost', port))
                    logger.debug(f"Found available port: {port}")
                    return port
            except OSError:
                continue
        raise RuntimeError(f"Could not find an available port in range {start_port}-{start_port + max_attempts}")

    def _signal_handler(self, signum, frame):
        """Handle signals for graceful cleanup"""
        logger.info("\nCleaning up...")
        self.cleanup()
        sys.exit(0)

    def cleanup(self):
        """Clean up port-forward process"""
        if self.port_forward_process:
            logger.debug("Terminating port-forward...")
            self.port_forward_process.terminate()
            try:
                self.port_forward_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.port_forward_process.kill()
            self.port_forward_process = None

    def start_port_forward(self):
        """Start port-forwarding to the HTTP server sidecar"""
        if self.port_forward_process:
            return  # Already running

        logger.debug(f"Starting port-forward to robovast:{self.remote_port} -> localhost:{self.local_port}")

        cmd = [
            "kubectl", "port-forward",
            "-n", "default",
            f"pod/robovast",
            f"{self.local_port}:{self.remote_port}"
        ]

        self.port_forward_process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        # Wait a moment for port-forward to establish
        time.sleep(2)

    def check_transfer_pod_exists(self):
        """Check if transfer-pod exists, exit if not found"""
        try:
            pod = self.k8s_client.read_namespaced_pod(
                name="robovast",
                namespace="default"
            )
            # Check if pod is running
            if pod.status.phase not in ["Running", "Pending"]:
                logger.error(f"Transfer pod 'robovast' exists but is not running (status: {pod.status.phase})!")
                sys.exit(1)
            logger.debug(f"Found existing transfer pod: robovast")
        except client.exceptions.ApiException as e:
            if e.status == 404:
                logger.error(f"Required pod 'robovast' does not exist!")
                sys.exit(1)
            else:
                raise

    def list_available_runs(self):
        """List all available run IDs by checking filesystem via kubectl"""
        try:
            # Use kubectl to list directories in /exports/out/
            list_runs_cmd = [
                "kubectl", "exec", "-n", "default", "robovast",
                "-c", "robovast",
                "--",
                "find", "/exports/out", "-maxdepth", "1", "-type", "d", "-name", "run-*"
            ]

            result = subprocess.run(list_runs_cmd, capture_output=True, text=True, check=True)

            # Extract run IDs from the full paths
            run_ids = []
            for line in result.stdout.strip().split('\n'):
                if line and 'run-' in line:
                    run_id = os.path.basename(line.strip())
                    if run_id.startswith('run-'):
                        run_ids.append(run_id)

            if run_ids:
                logger.debug(f"Available runs:")
                for run_id in run_ids:
                    logger.debug(f"  - {run_id}")

            return run_ids

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to list runs via kubectl: {e}")
            logger.error(f"stderr: {e.stderr}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error listing runs: {e}")
            return []

    def download_results(self, output_directory, force=False):
        """
        Download all result files from transfer PVC using HTTP server port-forwarding

        Args:
            output_directory (str): Local directory where files will be downloaded
            force (bool): Force re-download even if files already exist

        Returns:
            int: Number of successfully downloaded runs
        """

        # Create output directory
        os.makedirs(output_directory, exist_ok=True)

        # Get available runs using kubectl (no port-forward needed for listing)
        available_runs = self.list_available_runs()
        if not available_runs:
            logger.info("No runs found to download.")
            return 0

        logger.info(f"Downloading {len(available_runs)} run results to directory '{output_directory}'...")

        # Start port-forward for downloading
        self.start_port_forward()

        downloaded_count = 0
        try:
            for current_run_id in available_runs:
                if self._download_run(output_directory, current_run_id, force):
                    downloaded_count += 1
        finally:
            # Clean up port-forward
            self.cleanup()

        return downloaded_count

    def _download_run(self, output_directory, run_id, force=False):
        """
        Download a specific run via HTTP.

        Returns:
            bool: True if download was successful or skipped (already exists), False on failure
        """
        try:
            logger.info(f"Downloading {run_id}...")

            # Check if run directory already exists and is complete
            run_output_dir = os.path.join(output_directory, run_id)
            if not force and os.path.exists(run_output_dir) and os.listdir(run_output_dir):
                logger.info(f"Run {run_id} already exists and appears complete, skipping...")
                logger.info(f"Use --force to re-download existing runs")
                return True

            # Create compressed archive on remote pod using kubectl
            archive_name = f"{run_id}.tar.gz"
            remote_archive_path = f"/exports/{archive_name}"

            # Check if remote archive already exists
            check_archive_cmd = [
                "kubectl", "exec", "-n", "default", "robovast",
                "--",
                "test", "-f", remote_archive_path
            ]

            archive_exists = subprocess.run(check_archive_cmd, capture_output=True, text=True, check=False).returncode == 0

            if not archive_exists:
                logger.debug(f"Creating compressed archive on remote pod using kubectl...")
                
                # Ensure /exports/out directory exists
                ensure_dir_cmd = [
                    "kubectl", "exec", "-n", "default", "robovast",
                    "--",
                    "mkdir", "-p", "/exports/out"
                ]
                subprocess.run(ensure_dir_cmd, capture_output=True, text=True, check=False)
                
                # Check if run_id directory exists before creating archive
                check_run_dir_cmd = [
                    "kubectl", "exec", "-n", "default", "robovast",
                    "--",
                    "test", "-d", f"/exports/out/{run_id}"
                ]
                run_dir_exists = subprocess.run(check_run_dir_cmd, capture_output=True, text=True, check=False).returncode == 0
                
                if not run_dir_exists:
                    logger.warning(f"Run directory /exports/out/{run_id} does not exist, skipping archive creation")
                    return False
                
                create_archive_cmd = [
                    "kubectl", "exec", "-n", "default", "robovast",
                    "--",
                    "tar", "-czf", remote_archive_path, "-C", "/exports/out", run_id
                ]

                subprocess.run(create_archive_cmd, capture_output=True, text=True, check=True)
                logger.debug(f"Archive created successfully at {remote_archive_path}")
            else:
                logger.debug(f"Archive already exists at {remote_archive_path}, reusing...")

            # Now download the pre-created archive via HTTP
            download_url = f"/{archive_name}"

            # Download the archive with resume support
            local_archive_path = os.path.join(output_directory, archive_name)

            # Check if partial file exists and get its size
            initial_pos = 0
            if os.path.exists(local_archive_path):
                initial_pos = os.path.getsize(local_archive_path)
                logger.info(f"Found partial download ({initial_pos} bytes), attempting to resume...")

            # Set up headers for resume if partial file exists
            headers = {}
            if initial_pos > 0:
                headers['Range'] = f'bytes={initial_pos}-'

            logger.debug(f"Downloading archive from http://localhost:{self.local_port}{download_url} ...")

            with requests.get(f"http://localhost:{self.local_port}{download_url}",
                              headers=headers, stream=True, timeout=600) as response:
                total_size = 0
                # Handle different response codes
                if response.status_code == 206:  # Partial content (resume)
                    logger.info(f"Resuming download from byte {initial_pos}")
                    total_size = int(response.headers.get('content-range', '0').split('/')[-1])
                    downloaded = initial_pos
                elif response.status_code == 200:  # Full content
                    if initial_pos > 0:
                        logger.info(f"Server doesn't support resume, restarting download...")
                        # Remove partial file to start fresh
                        os.remove(local_archive_path)
                        initial_pos = 0
                    total_size = int(response.headers.get('content-length', 0))
                    downloaded = 0
                else:
                    response.raise_for_status()

                # Open file in append mode if resuming, otherwise write mode
                file_mode = 'ab' if initial_pos > 0 and response.status_code == 206 else 'wb'

                with open(local_archive_path, file_mode) as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            if total_size > 0:
                                progress = (downloaded / total_size) * 100
                                logger.info(f"Progress: {progress:.1f}% ({downloaded}/{total_size} bytes)")

            # Validate the downloaded archive
            logger.debug(f"Validating downloaded archive...")
            try:
                with tarfile.open(local_archive_path, 'r:gz') as tar:
                    # Try to list contents to validate the archive
                    tar.getnames()
                logger.debug(f"Archive validation successful")
            except (tarfile.TarError, OSError) as e:
                logger.error(f"Archive validation failed: {e}")
                logger.info(f"Removing corrupted archive and retrying...")
                os.remove(local_archive_path)
                raise RuntimeError("Archive validation failed, retry needed") from e

            # Extract the archive locally
            logger.debug(f"Extracting archive...")

            # Remove existing extraction directory if force mode or incomplete
            if force and os.path.exists(run_output_dir):
                logger.debug(f"Removing existing run directory for clean extraction...")
                shutil.rmtree(run_output_dir)

            with tarfile.open(local_archive_path, 'r:gz') as tar:
                tar.extractall(path=output_directory)

            # Keep local archive file (not removing it)
            # os.remove(local_archive_path)

            # Clean up remote archive using kubectl
            logger.debug(f"Cleaning up remote archive...")
            cleanup_cmd = [
                "kubectl", "exec", "-n", "default", "robovast",
                "--",
                "rm", "-f", remote_archive_path
            ]
            subprocess.run(cleanup_cmd, capture_output=True, text=True, check=False)

            # Delete the run directory from remote pod after successful download
            logger.debug(f"Deleting run directory from remote pod...")
            delete_run_cmd = [
                "kubectl", "exec", "-n", "default", "robovast",
                "--",
                "rm", "-rf", f"/exports/out/{run_id}"
            ]
            subprocess.run(delete_run_cmd, capture_output=True, text=True, check=True)

            logger.info(f"Successfully downloaded run {run_id}")
            return True

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to create archive for run {run_id}: {e}")
            logger.error(f"stdout: {e.stdout}")
            logger.error(f"stderr: {e.stderr}")

            # Clean up remote archive on error
            cleanup_cmd = [
                "kubectl", "exec", "-n", "default", "robovast",
                "--",
                "rm", "-f", remote_archive_path
            ]
            subprocess.run(cleanup_cmd, capture_output=True, text=True, check=False)
            return False
        except requests.RequestException as e:
            logger.error(f"Failed to download run {run_id} via HTTP: {e}")

            # Clean up remote archive on error
            cleanup_cmd = [
                "kubectl", "exec", "-n", "default", "robovast",
                "--",
                "rm", "-f", remote_archive_path
            ]
            subprocess.run(cleanup_cmd, capture_output=True, text=True, check=False)
            return False
        except Exception as e:
            logger.error(f"Unexpected error downloading run {run_id}: {e}")

            # Clean up remote archive on error
            cleanup_cmd = [
                "kubectl", "exec", "-n", "default", "robovast",
                "--",
                "rm", "-f", remote_archive_path
            ]
            subprocess.run(cleanup_cmd, capture_output=True, text=True, check=False)
            return False
