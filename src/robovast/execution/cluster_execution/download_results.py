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

from .cluster_execution import get_cluster_run_job_counts_per_run
from .s3_client import ClusterS3Client

logger = logging.getLogger(__name__)

# Progress bar constants (match cluster monitor style)
BAR_WIDTH = 20
CLEAR_LINE = "\033[2K"


def _format_size(n):
    """Format bytes as human-readable string (MiB)."""
    return f"{n / 1024 / 1024:.1f} MiB"


def _format_rate(bytes_per_sec):
    """Format bytes/sec as human-readable string."""
    if bytes_per_sec >= 1024 * 1024:
        return f"{bytes_per_sec / 1024 / 1024:.1f} MiB/s"
    if bytes_per_sec >= 1024:
        return f"{bytes_per_sec / 1024:.1f} KiB/s"
    return f"{bytes_per_sec:.0f} B/s"


class ResultDownloader:
    def __init__(self, namespace="default", cluster_config=None, context=None):
        self.namespace = namespace
        self.cluster_config = cluster_config
        self.context = context
        self.port_forward_process = None
        self.local_port = self._find_available_port()
        self.remote_port = 80  # HTTP server port in sidecar

        # Initialize Kubernetes client
        config.load_kube_config(context=context)
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

        ctx_args = ["--context", self.context] if self.context else []
        cmd = ["kubectl"] + ctx_args + [
            "port-forward",
            "-n", self.namespace,
            "pod/robovast",
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

    def list_remote_archives(self):
        """Return a list of .tar.gz filenames present in /data/ on the pod."""
        try:
            ctx_args = ["--context", self.context] if self.context else []
            result = subprocess.run(
                ["kubectl"] + ctx_args + [
                    "exec", "-n", self.namespace, "robovast",
                    "-c", "archiver",
                    "--",
                    "sh", "-c", "ls /data/*.tar.gz 2>/dev/null || true",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            files = []
            for line in result.stdout.splitlines():
                line = line.strip()
                if line:
                    files.append(os.path.basename(line))
            return files
        except Exception as e:
            logger.warning(f"Could not list remote archives: {e}")
            return []

    def port_forward_only(self):
        """Start a port-forward to the HTTP sidecar and print downloadable URLs.

        Keeps the tunnel open until the user presses Ctrl+C.
        """
        self.start_port_forward()
        base_url = f"http://localhost:{self.local_port}"

        archives = self.list_remote_archives()
        if archives:
            print(f"Port-forward active — {base_url}")
            print(f"Downloadable archives ({len(archives)}):")
            for name in sorted(archives):
                print(f"  {base_url}/{name}")
            print()
            print("Example:  curl -O <url>")
        else:
            print(f"Port-forward active — {base_url}")
            print("No .tar.gz archives found on the pod yet.")

        print("Press Ctrl+C to stop the port-forward.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nStopping port-forward...")
        finally:
            self.cleanup()

    def remote_compress_only(self, force=False, verbose=False):
        """Create compressed archives on the remote pod for all available runs.

        Streams S3 bucket contents to tar.gz on the pod. Does not start
        port-forward or download. Use ``port_forward_only`` afterwards to
        download via HTTP, or run full ``download`` later.

        Args:
            force (bool): Overwrite existing remote archives
            verbose (bool): Print detailed progress

        Returns:
            int: Number of runs for which archives were created or already existed
        """
        available_runs, excluded_runs = self.list_available_runs()

        if excluded_runs:
            for rid, running, pending in excluded_runs:
                logger.info(
                    "Run %s not ready (jobs still running: %d, pending: %d)",
                    rid, running, pending,
                )

        if not available_runs:
            if excluded_runs:
                logger.info("No runs ready. Wait for jobs to finish and try again.")
            else:
                logger.info("No runs found.")
            return 0

        logger.info(f"Compressing {len(available_runs)} runs on remote pod...")
        count = 0
        for run_id in available_runs:
            if self._create_remote_archive(run_id, force=force, verbose=verbose):
                count += 1
        return count

    def _create_remote_archive(self, run_id, force=False, verbose=False):
        """Create compressed archive on remote pod from S3 bucket.

        Returns:
            bool: True if archive exists or was created successfully
        """
        archive_name = f"{run_id}.tar.gz"
        remote_archive_path = f"/data/{archive_name}"
        unfinished_flag_path = f"{remote_archive_path}_unfinished"

        ctx_args = ["--context", self.context] if self.context else []
        check_archive_cmd = ["kubectl"] + ctx_args + [
            "exec", "-n", self.namespace, "robovast",
            "-c", "archiver",
            "--",
            "test", "-f", remote_archive_path
        ]
        archive_exists = subprocess.run(
            check_archive_cmd, capture_output=True, text=True, check=False
        ).returncode == 0

        if archive_exists:
            # Check if unfinished flag is present (incomplete previous run)
            check_flag_cmd = ["kubectl"] + ctx_args + [
                "exec", "-n", self.namespace, "robovast",
                "-c", "archiver",
                "--",
                "test", "-f", unfinished_flag_path
            ]
            flag_exists = subprocess.run(
                check_flag_cmd, capture_output=True, text=True, check=False
            ).returncode == 0

            if flag_exists:
                logger.info(f"Run {run_id}: incomplete archive found (unfinished flag present), recreating...")
                for path in (remote_archive_path, unfinished_flag_path):
                    subprocess.run(
                        ["kubectl"] + ctx_args + [
                            "exec", "-n", self.namespace, "robovast",
                            "-c", "archiver", "--", "rm", "-f", path
                        ],
                        capture_output=True, text=True, check=False,
                    )
                archive_exists = False
            elif not force:
                if verbose:
                    logger.info(f"Run {run_id}: archive already exists at {remote_archive_path}, skipping")
                else:
                    sys.stdout.write("\r" + CLEAR_LINE + f"{run_id}  skipped (archive exists)\n")
                    sys.stdout.flush()
                return True
            else:
                # force=True and no unfinished flag — remove the complete archive
                subprocess.run(
                    ["kubectl"] + ctx_args + [
                        "exec", "-n", self.namespace, "robovast",
                        "-c", "archiver", "--", "rm", "-f", remote_archive_path
                    ],
                    capture_output=True, text=True, check=False,
                )
                archive_exists = False

        try:
            if not verbose:
                sys.stdout.write("\r" + CLEAR_LINE + f"{run_id}  streaming S3 to tar.gz...")
                sys.stdout.flush()
            logger.debug(f"Streaming S3 bucket {run_id} to tar.gz via archiver container...")

            # Mark archive creation as in progress
            subprocess.run(
                ["kubectl"] + ctx_args + [
                    "exec", "-n", self.namespace, "robovast",
                    "-c", "archiver", "--", "touch", unfinished_flag_path
                ],
                capture_output=True, text=True, check=False,
            )

            script_path = os.path.join(os.path.dirname(__file__), "s3_to_targz.py")
            with open(script_path, encoding="utf-8") as f:
                script_content = f.read()

            create_archive_cmd = ["kubectl"] + ctx_args + [
                "exec", "-i", "-n", self.namespace, "robovast",
                "-c", "archiver",
                "--",
                "python", "-", run_id
            ]
            subprocess.run(
                create_archive_cmd,
                input=script_content,
                capture_output=True,
                text=True,
                check=True,
            )

            # Archive created successfully — remove the unfinished flag
            subprocess.run(
                ["kubectl"] + ctx_args + [
                    "exec", "-n", self.namespace, "robovast",
                    "-c", "archiver", "--", "rm", "-f", unfinished_flag_path
                ],
                capture_output=True, text=True, check=False,
            )

            if verbose:
                logger.info(f"Created archive at {remote_archive_path}")
            else:
                sys.stdout.write("\r" + CLEAR_LINE + f"{run_id}  compressed to {archive_name}\n")
                sys.stdout.flush()
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to create archive for run {run_id}: {e}")
            return False

    def check_transfer_pod_exists(self):
        """Check if transfer-pod exists, exit if not found"""
        try:
            pod = self.k8s_client.read_namespaced_pod(
                name="robovast",
                namespace=self.namespace
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
        """List all available run IDs by listing S3 buckets (run-*).

        Excludes runs that still have running or pending jobs.

        Returns:
            tuple: (available_run_ids, excluded_runs) where excluded_runs is a list of
                   (run_id, running_count, pending_count) for runs not yet downloadable.
        """
        try:
            if self.cluster_config:
                access_key, secret_key = self.cluster_config.get_s3_credentials()
            else:
                access_key, secret_key = "minioadmin", "minioadmin"

            with ClusterS3Client(
                namespace=self.namespace,
                access_key=access_key,
                secret_key=secret_key,
                context=self.context,
            ) as s3:
                all_run_ids = s3.list_run_buckets()

            # Exclude runs with running or pending jobs
            job_counts = get_cluster_run_job_counts_per_run(namespace=self.namespace, context=self.context)
            available = []
            excluded = []
            for rid in all_run_ids:
                counts = job_counts.get(rid, {})
                running = counts.get("running", 0)
                pending = counts.get("pending", 0)
                if running == 0 and pending == 0:
                    available.append(rid)
                else:
                    excluded.append((rid, running, pending))

            if available:
                logger.debug(f"Available runs (finished only): {available}")
            return available, excluded

        except Exception as e:
            logger.error(f"Unexpected error listing runs: {e}")
            return [], []

    def download_results(self, output_directory, force=False, verbose=False, skip_removal=True,
                        keep_archive=True):
        """
        Download all result files from transfer PVC using HTTP server port-forwarding

        Args:
            output_directory (str): Local directory where files will be downloaded
            force (bool): Force re-download even if files already exist
            verbose (bool): If True, emit more detailed logging
            skip_removal (bool): If True, do not remove remote archive or delete S3 bucket after download (default: True)
            keep_archive (bool): If True, keep the local .tar.gz file after extraction (default: True)

        Returns:
            int: Number of successfully downloaded runs
        """

        # Create output directory
        os.makedirs(output_directory, exist_ok=True)

        # Get available runs (excludes runs with running/pending jobs)
        available_runs, excluded_runs = self.list_available_runs()

        if excluded_runs:
            for rid, running, pending in excluded_runs:
                logger.info(
                    "Run %s not downloadable (jobs still running: %d, pending: %d)",
                    rid, running, pending,
                )

        if not available_runs:
            if excluded_runs:
                logger.info("No runs ready to download. Wait for jobs to finish and try again.")
            else:
                logger.info("No runs found to download.")
            return 0

        logger.info(f"Downloading {len(available_runs)} run results to directory '{output_directory}'...")

        # Start port-forward for downloading
        self.start_port_forward()

        downloaded_count = 0
        try:
            for current_run_id in available_runs:
                if self._download_run(output_directory, current_run_id, force, verbose, skip_removal,
                                     keep_archive):
                    downloaded_count += 1
        finally:
            # Clean up port-forward
            self.cleanup()

        return downloaded_count

    def _download_run(self, output_directory, run_id, force=False, verbose=False, skip_removal=True,
                     keep_archive=True):
        """
        Download a specific run via HTTP.

        Returns:
            bool: True if download was successful or skipped (already exists), False on failure
        """
        try:
            if verbose:
                logger.info(f"Downloading {run_id}...")

            # Check if run directory already exists and is complete
            run_output_dir = os.path.join(output_directory, run_id)
            if not force and os.path.exists(run_output_dir) and os.listdir(run_output_dir):
                if verbose:
                    logger.info(f"Run {run_id} already exists and appears complete, skipping...")
                    logger.info(f"Use --force to re-download existing runs")
                else:
                    sys.stdout.write("\r" + CLEAR_LINE + f"{run_id}  skipped (already exists)\n")
                    sys.stdout.flush()
                return True

            # Create compressed archive on remote pod using archiver container (has tar/gzip)
            archive_name = f"{run_id}.tar.gz"
            remote_archive_path = f"/data/{archive_name}"
            unfinished_flag_path = f"{remote_archive_path}_unfinished"

            # Check if remote archive already exists
            ctx_args = ["--context", self.context] if self.context else []
            check_archive_cmd = ["kubectl"] + ctx_args + [
                "exec", "-n", self.namespace, "robovast",
                "-c", "archiver",
                "--",
                "test", "-f", remote_archive_path
            ]

            archive_exists = subprocess.run(check_archive_cmd, capture_output=True, text=True, check=False).returncode == 0

            if archive_exists:
                # Check whether a previous creation was interrupted
                check_flag_cmd = ["kubectl"] + ctx_args + [
                    "exec", "-n", self.namespace, "robovast",
                    "-c", "archiver",
                    "--",
                    "test", "-f", unfinished_flag_path
                ]
                flag_exists = subprocess.run(
                    check_flag_cmd, capture_output=True, text=True, check=False
                ).returncode == 0

                if flag_exists:
                    logger.info(
                        f"Run {run_id}: incomplete remote archive detected (unfinished flag present), recreating..."
                    )
                    for path in (remote_archive_path, unfinished_flag_path):
                        subprocess.run(
                            ["kubectl"] + ctx_args + [
                                "exec", "-n", self.namespace, "robovast",
                                "-c", "archiver", "--", "rm", "-f", path
                            ],
                            capture_output=True, text=True, check=False,
                        )
                    archive_exists = False
                else:
                    sys.stdout.write(f"{run_id}  Remote archive already exists at {remote_archive_path}. Overwrite? [y/N] ")
                    sys.stdout.flush()
                    answer = sys.stdin.readline().strip().lower()
                    if answer in ("y", "yes"):
                        logger.debug(f"Removing existing remote archive {remote_archive_path}...")
                        subprocess.run(
                            ["kubectl"] + ctx_args + [
                                "exec", "-n", self.namespace, "robovast",
                                "-c", "archiver", "--", "rm", "-f", remote_archive_path
                            ],
                            capture_output=True, text=True, check=False,
                        )
                        archive_exists = False
                    else:
                        logger.debug(f"Reusing existing remote archive {remote_archive_path}.")

            if not archive_exists:
                if not verbose:
                    sys.stdout.write("\r" + CLEAR_LINE + f"{run_id}  streaming S3 to tar.gz...")
                    sys.stdout.flush()
                logger.debug(f"Streaming S3 bucket {run_id} to tar.gz via archiver container...")

                # Mark archive creation as in progress
                subprocess.run(
                    ["kubectl"] + ctx_args + [
                        "exec", "-n", self.namespace, "robovast",
                        "-c", "archiver", "--", "touch", unfinished_flag_path
                    ],
                    capture_output=True, text=True, check=False,
                )

                script_path = os.path.join(os.path.dirname(__file__), "s3_to_targz.py")
                with open(script_path, encoding="utf-8") as f:
                    script_content = f.read()

                create_archive_cmd = ["kubectl"] + ctx_args + [
                    "exec", "-i", "-n", self.namespace, "robovast",
                    "-c", "archiver",
                    "--",
                    "python", "-", run_id
                ]
                subprocess.run(
                    create_archive_cmd,
                    input=script_content,
                    capture_output=True,
                    text=True,
                    check=True,
                )

                # Archive created successfully — remove the unfinished flag
                subprocess.run(
                    ["kubectl"] + ctx_args + [
                        "exec", "-n", self.namespace, "robovast",
                        "-c", "archiver", "--", "rm", "-f", unfinished_flag_path
                    ],
                    capture_output=True, text=True, check=False,
                )

                logger.debug(f"Archive created successfully at {remote_archive_path}")
            else:
                logger.debug(f"Archive already exists at {remote_archive_path}, reusing...")

            # Now download the pre-created archive via HTTP
            download_url = f"/{archive_name}"

            # Download the archive with resume support
            local_archive_path = os.path.join(output_directory, archive_name)

            # When we just created a fresh archive, any local partial is stale - remove it
            if not archive_exists and os.path.exists(local_archive_path):
                os.remove(local_archive_path)
                logger.debug(f"Removed stale partial file (remote archive was just recreated)")

            # Check if partial file exists and get its size (for resume)
            initial_pos = 0
            if os.path.exists(local_archive_path):
                initial_pos = os.path.getsize(local_archive_path)
                logger.info(f"Found partial download ({initial_pos} bytes), attempting to resume...")

            # Set up headers for resume if partial file exists
            headers = {}
            if initial_pos > 0:
                headers['Range'] = f'bytes={initial_pos}-'

            logger.debug(f"Downloading archive from http://localhost:{self.local_port}{download_url} ...")

            response = requests.get(
                f"http://localhost:{self.local_port}{download_url}",
                headers=headers, stream=True, timeout=600
            )

            # 416 = Range Not Satisfiable (partial is stale or server doesn't support resume)
            if response.status_code == 416:
                response.close()
                if initial_pos > 0:
                    logger.info(f"Resume not supported or stale partial, restarting full download...")
                    os.remove(local_archive_path)
                    initial_pos = 0
                    headers = {}
                    response = requests.get(
                        f"http://localhost:{self.local_port}{download_url}",
                        headers=headers, stream=True, timeout=600
                    )

            with response:
                total_size = 0
                # Handle different response codes
                if response.status_code == 206:  # Partial content (resume)
                    logger.info(f"Resuming download from byte {initial_pos}")
                    total_size = int(response.headers.get('content-range', '0').split('/')[-1])
                    downloaded = initial_pos
                elif response.status_code == 200:  # Full content
                    if initial_pos > 0:
                        logger.info(f"Server doesn't support resume, restarting download...")
                        os.remove(local_archive_path)
                        initial_pos = 0
                    total_size = int(response.headers.get('content-length', 0))
                    downloaded = 0
                else:
                    response.raise_for_status()

                # Open file in append mode if resuming, otherwise write mode
                file_mode = 'ab' if initial_pos > 0 and response.status_code == 206 else 'wb'

                last_pct = [-1]  # mutable to allow update in loop
                last_size_shown = [-1]  # throttle when total_size unknown
                start_time = time.time()

                def _show_progress():
                    elapsed = time.time() - start_time
                    rate = (downloaded / elapsed) if elapsed > 0 else 0
                    if verbose:
                        if total_size > 0:
                            pct = (downloaded / total_size) * 100
                            logger.info(f"Progress: {pct:.1f}% ({downloaded}/{total_size} bytes) {_format_rate(rate)}")
                    else:
                        # Single-line progress bar (cluster monitor style)
                        if total_size > 0:
                            pct = (downloaded / total_size) * 100
                            filled = int(BAR_WIDTH * downloaded / total_size)
                            progress_bar = "█" * filled + "░" * (BAR_WIDTH - filled)
                            size_str = f"{_format_size(downloaded)}/{_format_size(total_size)}"
                            rate_str = _format_rate(rate)
                            line = f"{run_id}  [{progress_bar}]  {pct:5.1f}%  {size_str}  {rate_str}"
                            # Throttle: update only when pct changes by >= 1
                            if int(pct) > last_pct[0]:
                                last_pct[0] = int(pct)
                                sys.stdout.write("\r" + CLEAR_LINE + line)
                                sys.stdout.flush()
                        else:
                            # Throttle: update every ~1 MiB when size unknown (or first chunk)
                            if last_size_shown[0] < 0 or downloaded - last_size_shown[0] >= 1024 * 1024:
                                last_size_shown[0] = downloaded
                                size_str = _format_size(downloaded)
                                sys.stdout.write("\r" + CLEAR_LINE + f"{run_id}  downloading...  {size_str}  {_format_rate(rate)}")
                                sys.stdout.flush()

                with open(local_archive_path, file_mode) as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            _show_progress()

                if not verbose:
                    # Final update and newline when done
                    elapsed = time.time() - start_time
                    rate = (downloaded / elapsed) if elapsed > 0 else 0
                    if total_size > 0:
                        filled = BAR_WIDTH
                        progress_bar = "█" * filled + "░" * 0
                        size_str = f"{_format_size(total_size)}/{_format_size(total_size)}"
                        sys.stdout.write("\r" + CLEAR_LINE + f"{run_id}  [{progress_bar}]  100.0%  {size_str}  {_format_rate(rate)}\n")
                    else:
                        sys.stdout.write("\r" + CLEAR_LINE + f"{run_id}  downloaded  {_format_size(downloaded)}  {_format_rate(rate)}\n")
                    sys.stdout.flush()

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
                members = tar.getmembers()
                total = len(members)
                if not verbose and total > 0:
                    for i, member in enumerate(members, 1):
                        tar.extract(member, path=output_directory)
                        pct = (i / total) * 100
                        filled = int(BAR_WIDTH * i / total)
                        progress_bar = "█" * filled + "░" * (BAR_WIDTH - filled)
                        sys.stdout.write(
                            "\r" + CLEAR_LINE +
                            f"{run_id}  extracting  [{progress_bar}]  {pct:5.1f}%  {i}/{total} files"
                        )
                        sys.stdout.flush()
                    sys.stdout.write("\r" + CLEAR_LINE + f"{run_id}  extracted  {total} files\n")
                    sys.stdout.flush()
                else:
                    tar.extractall(path=output_directory)

            if not keep_archive:
                os.remove(local_archive_path)

            if not skip_removal:
                # Clean up remote archive using kubectl
                logger.debug(f"Cleaning up remote archive...")
                cleanup_cmd = ["kubectl"] + ctx_args + [
                    "exec", "-n", self.namespace, "robovast",
                    "-c", "archiver",
                    "--",
                    "rm", "-f", remote_archive_path
                ]
                subprocess.run(cleanup_cmd, capture_output=True, text=True, check=False)

                # Delete the S3 bucket after successful download
                logger.debug(f"Deleting S3 bucket {run_id}...")
                if self.cluster_config:
                    access_key, secret_key = self.cluster_config.get_s3_credentials()
                else:
                    access_key, secret_key = "minioadmin", "minioadmin"
                with ClusterS3Client(
                    namespace=self.namespace,
                    access_key=access_key,
                    secret_key=secret_key,
                    context=self.context,
                ) as s3:
                    s3.delete_bucket(run_id)

            if verbose:
                logger.info(f"Successfully downloaded run {run_id}")
            return True

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to create archive for run {run_id}: {e}")
            logger.error(f"stdout: {e.stdout}")
            logger.error(f"stderr: {e.stderr}")
            logger.info(f"Remote archive kept at {remote_archive_path} for inspection")
            return False
        except requests.RequestException as e:
            logger.error(f"Failed to download run {run_id} via HTTP: {e}")
            logger.info(f"Remote archive kept at {remote_archive_path} for inspection")
            return False
        except Exception as e:
            logger.error(f"Unexpected error downloading run {run_id}: {e}")
            logger.info(f"Remote archive kept at {remote_archive_path} for inspection")
            return False
