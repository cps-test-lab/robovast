#!/usr/bin/env python3
"""
Script to download all result files from the transfer PVC using HTTP server port-forwarding.
The script checks for the existence of transfer-pvc and transfer-pod, creates a port-forward
to the HTTP server sidecar, and downloads compressed archives via HTTP.

Usage: python download_results.py <output_directory> [run_id]

This script uses port-forwarding to an HTTP server sidecar for downloading files.
"""
import argparse
import os
import signal
import socket
import subprocess
import sys
import tarfile
import time

import requests
from kubernetes import client, config


class ResultDownloader:
    def __init__(self):
        self.transfer_pvc_name = "transfer-pvc"
        self.transfer_pod_name = "nfs-server"
        self.port_forward_process = None
        self.local_port = self._find_available_port()
        self.remote_port = 80  # HTTP server port in sidecar

        # Initialize Kubernetes client
        config.load_kube_config()
        self.k8s_client = client.CoreV1Api()

        # Check if transfer-pvc exists
        self.check_transfer_pvc_exists()

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
                    print(f"### Found available port: {port}")
                    return port
            except OSError:
                continue
        raise RuntimeError(f"Could not find an available port in range {start_port}-{start_port + max_attempts}")

    def _signal_handler(self, signum, frame):
        """Handle signals for graceful cleanup"""
        print("\n### Cleaning up...")
        self.cleanup()
        sys.exit(0)

    def cleanup(self):
        """Clean up port-forward process"""
        if self.port_forward_process:
            print("### Terminating port-forward...")
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

        print(f"### Starting port-forward to {self.transfer_pod_name}:{self.remote_port} -> localhost:{self.local_port}")

        cmd = [
            "kubectl", "port-forward",
            "-n", "default",
            f"pod/{self.transfer_pod_name}",
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

    def check_transfer_pvc_exists(self):
        """Check if transfer-pvc exists, exit if not found"""
        try:
            self.k8s_client.read_namespaced_persistent_volume_claim(
                name=self.transfer_pvc_name,
                namespace="default"
            )
            print(f"### Found existing transfer PVC: {self.transfer_pvc_name}")
        except client.exceptions.ApiException as e:
            if e.status == 404:
                print(f"### ERROR: Required PVC '{self.transfer_pvc_name}' does not exist!")
                sys.exit(1)
            else:
                raise

    def check_transfer_pod_exists(self):
        """Check if transfer-pod exists, exit if not found"""
        try:
            pod = self.k8s_client.read_namespaced_pod(
                name=self.transfer_pod_name,
                namespace="default"
            )
            # Check if pod is running
            if pod.status.phase not in ["Running", "Pending"]:
                print(f"### ERROR: Transfer pod '{self.transfer_pod_name}' exists but is not running (status: {pod.status.phase})!")
                sys.exit(1)
            print(f"### Found existing transfer pod: {self.transfer_pod_name}")
        except client.exceptions.ApiException as e:
            if e.status == 404:
                print(f"### ERROR: Required pod '{self.transfer_pod_name}' does not exist!")
                sys.exit(1)
            else:
                raise

    def list_available_runs(self):
        """List all available run IDs by checking filesystem via kubectl"""
        try:
            # Use kubectl to list directories in /exports/out/
            list_runs_cmd = [
                "kubectl", "exec", "-n", "default", self.transfer_pod_name,
                "-c", "nfs-server",
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
                print(f"### Available runs:")
                for run_id in run_ids:
                    print(f"  - {run_id}")

            return run_ids

        except subprocess.CalledProcessError as e:
            print(f"### ERROR: Failed to list runs via kubectl: {e}")
            print(f"### stderr: {e.stderr}")
            return []
        except Exception as e:
            print(f"### ERROR: Unexpected error listing runs: {e}")
            return []

    def list_scenarios_in_run(self, run_id):
        """List all scenario IDs for a specific run by checking filesystem via kubectl"""
        try:
            # Use kubectl to list directories in /exports/out/<run_id>/
            list_scenarios_cmd = [
                "kubectl", "exec", "-n", "default", self.transfer_pod_name,
                "-c", "nfs-server",
                "--",
                "find", f"/exports/out/{run_id}", "-maxdepth", "1", "-type", "d"
            ]

            result = subprocess.run(list_scenarios_cmd, capture_output=True, text=True, check=True)

            # Extract scenario IDs from the full paths
            scenario_ids = []
            for line in result.stdout.strip().split('\n'):
                if line and line != f"/exports/out/{run_id}":
                    scenario_id = os.path.basename(line.strip())
                    if scenario_id:  # Skip empty names
                        scenario_ids.append(scenario_id)

            if scenario_ids:
                print(f"### Available scenarios in run '{run_id}':")
                for scenario_id in scenario_ids:
                    print(f"  - {scenario_id}")

            return scenario_ids

        except subprocess.CalledProcessError as e:
            print(f"### ERROR: Failed to list scenarios for run {run_id} via kubectl: {e}")
            print(f"### stderr: {e.stderr}")
            return []
        except Exception as e:
            print(f"### ERROR: Unexpected error listing scenarios: {e}")
            return []

    def download_results(self, output_directory, force=False):
        """
        Download all result files from transfer PVC using HTTP server port-forwarding

        Args:
            output_directory (str): Local directory where files will be downloaded
            force (bool): Force re-download even if files already exist
        """

        # Create output directory
        os.makedirs(output_directory, exist_ok=True)
        print(f"### Output directory: {output_directory}")

        # Get available runs using kubectl (no port-forward needed for listing)
        available_runs = self.list_available_runs()
        if not available_runs:
            print("### No runs found to download.")
            return

        # Start port-forward for downloading
        self.start_port_forward()

        try:
            print(f"### Downloading {len(available_runs)} runs...")
            for current_run_id in available_runs:
                self._download_run(output_directory, current_run_id, force)
        finally:
            # Clean up port-forward
            self.cleanup()

    def _download_run(self, output_directory, run_id, force=False):
        """Download a specific run via HTTP."""
        try:
            print(f"### Downloading entire run: {run_id}")

            # Check if run directory already exists and is complete
            run_output_dir = os.path.join(output_directory, run_id)
            if not force and os.path.exists(run_output_dir) and os.listdir(run_output_dir):
                print(f"### Run {run_id} already exists and appears complete, skipping...")
                print(f"### Use --force to re-download existing runs")
                return

            # Create compressed archive on remote pod using kubectl
            archive_name = f"{run_id}.tar.gz"
            remote_archive_path = f"/exports/{archive_name}"

            # Check if remote archive already exists
            check_archive_cmd = [
                "kubectl", "exec", "-n", "default", self.transfer_pod_name,
                "--",
                "test", "-f", remote_archive_path
            ]

            archive_exists = subprocess.run(check_archive_cmd, capture_output=True, text=True).returncode == 0

            if not archive_exists:
                print(f"### Creating compressed archive on remote pod using kubectl...")
                create_archive_cmd = [
                    "kubectl", "exec", "-n", "default", self.transfer_pod_name,
                    "--",
                    "tar", "-czf", remote_archive_path, "-C", "/exports/out", run_id
                ]

                result = subprocess.run(create_archive_cmd, capture_output=True, text=True, check=True)
                print(f"### Archive created successfully at {remote_archive_path}")
            else:
                print(f"### Archive already exists at {remote_archive_path}, reusing...")

            # Now download the pre-created archive via HTTP
            download_url = f"/{archive_name}"

            # Download the archive with resume support
            local_archive_path = os.path.join(output_directory, archive_name)

            # Check if partial file exists and get its size
            initial_pos = 0
            if os.path.exists(local_archive_path):
                initial_pos = os.path.getsize(local_archive_path)
                print(f"### Found partial download ({initial_pos} bytes), attempting to resume...")

            # Set up headers for resume if partial file exists
            headers = {}
            if initial_pos > 0:
                headers['Range'] = f'bytes={initial_pos}-'

            print(f"### Downloading archive from http://localhost:{self.local_port}{download_url} ...")

            with requests.get(f"http://localhost:{self.local_port}{download_url}",
                              headers=headers, stream=True, timeout=600) as response:

                # Handle different response codes
                if response.status_code == 206:  # Partial content (resume)
                    print(f"### Resuming download from byte {initial_pos}")
                    total_size = int(response.headers.get('content-range', '0').split('/')[-1])
                    downloaded = initial_pos
                elif response.status_code == 200:  # Full content
                    if initial_pos > 0:
                        print(f"### Server doesn't support resume, restarting download...")
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
                                print(f"\r### Progress: {progress:.1f}% ({downloaded}/{total_size} bytes)", end='', flush=True)

            print(f"\n### Archive downloaded successfully")

            # Validate the downloaded archive
            print(f"### Validating downloaded archive...")
            try:
                with tarfile.open(local_archive_path, 'r:gz') as tar:
                    # Try to list contents to validate the archive
                    tar.getnames()
                print(f"### Archive validation successful")
            except (tarfile.TarError, OSError) as e:
                print(f"### Archive validation failed: {e}")
                print(f"### Removing corrupted archive and retrying...")
                os.remove(local_archive_path)
                raise Exception("Archive validation failed, retry needed")

            # Extract the archive locally
            print(f"### Extracting archive...")

            # Remove existing extraction directory if force mode or incomplete
            if force and os.path.exists(run_output_dir):
                print(f"### Removing existing run directory for clean extraction...")
                import shutil
                shutil.rmtree(run_output_dir)

            with tarfile.open(local_archive_path, 'r:gz') as tar:
                tar.extractall(path=output_directory)

            # Keep local archive file (not removing it)
            # os.remove(local_archive_path)

            # Clean up remote archive using kubectl
            print(f"### Cleaning up remote archive...")
            cleanup_cmd = [
                "kubectl", "exec", "-n", "default", self.transfer_pod_name,
                "--",
                "rm", "-f", remote_archive_path
            ]
            subprocess.run(cleanup_cmd, capture_output=True, text=True)

            # Delete the run directory from remote pod after successful download
            print(f"### Deleting run directory from remote pod...")
            delete_run_cmd = [
                "kubectl", "exec", "-n", "default", self.transfer_pod_name,
                "--",
                "rm", "-rf", f"/exports/out/{run_id}"
            ]
            result = subprocess.run(delete_run_cmd, capture_output=True, text=True, check=True)
            print(f"### Successfully deleted run {run_id} from remote pod")

            print(f"### Successfully downloaded, extracted, and cleaned up run {run_id}")

        except subprocess.CalledProcessError as e:
            print(f"### ERROR: Failed to create archive for run {run_id}: {e}")
            print(f"### stdout: {e.stdout}")
            print(f"### stderr: {e.stderr}")

            # Clean up remote archive on error
            cleanup_cmd = [
                "kubectl", "exec", "-n", "default", self.transfer_pod_name,
                "--",
                "rm", "-f", remote_archive_path
            ]
            subprocess.run(cleanup_cmd, capture_output=True, text=True)
        except requests.RequestException as e:
            print(f"### ERROR: Failed to download run {run_id} via HTTP: {e}")

            # Clean up remote archive on error
            cleanup_cmd = [
                "kubectl", "exec", "-n", "default", self.transfer_pod_name,
                "--",
                "rm", "-f", remote_archive_path
            ]
            subprocess.run(cleanup_cmd, capture_output=True, text=True)
        except Exception as e:
            print(f"### ERROR: Unexpected error downloading run {run_id}: {e}")

            # Clean up remote archive on error
            cleanup_cmd = [
                "kubectl", "exec", "-n", "default", self.transfer_pod_name,
                "--",
                "rm", "-f", remote_archive_path
            ]
            subprocess.run(cleanup_cmd, capture_output=True, text=True)


def parse_arguments():
    """Parse command line arguments using argparse."""
    parser = argparse.ArgumentParser(
        description="Download result files from transfer PVC using HTTP server port-forwarding.",
        epilog="""
Examples:
  %(prog)s ./downloaded_results                           # Download all runs (resume if interrupted)
  %(prog)s ./downloaded_results --force                   # Force re-download all runs
  %(prog)s --list                                         # List all available runs
  %(prog)s --list --run-id run-2025-01-13-120000          # List scenarios in specific run

Note: The script checks for transfer-pvc and transfer-pod availability before proceeding.
      Files are downloaded via HTTP server port-forwarding for better handling of large and binary files.
      Archives are automatically extracted after download.
      Downloads can be resumed if interrupted - partial files will be continued from where they left off.
      Use --force to re-download files that already exist locally.
      Only downloading all runs is supported - no individual run or scenario selection.
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    # Main action: list or download (mutually exclusive)
    parser.add_argument(
        '--list',
        action='store_true',
        help='List available runs and scenarios instead of downloading'
    )

    parser.add_argument(
        'output_directory',
        nargs='?',
        help='Directory where all runs will be downloaded (required for download mode)'
    )

    # Optional arguments
    parser.add_argument(
        '--run-id',
        help='Specific run ID to list scenarios for (only used with --list)'
    )

    parser.add_argument(
        '--force',
        action='store_true',
        help='Force re-download even if files already exist locally'
    )

    return parser.parse_args()


def main():
    args = parse_arguments()

    # Validate arguments
    if not args.list and not args.output_directory:
        print("Error: Output directory is required for download mode")
        print("Use --help for usage information")
        sys.exit(1)

    try:
        downloader = ResultDownloader()

        if args.list:
            # List mode - use kubectl directly, no port-forward needed
            if args.run_id:
                # List scenarios in specific run
                scenarios = downloader.list_scenarios_in_run(args.run_id)
                if scenarios:
                    print(f"### Scenarios in run '{args.run_id}':")
                    for scenario in scenarios:
                        print(f"  - {scenario}")
                else:
                    print(f"### No scenarios found in run '{args.run_id}'")
            else:
                # List all runs
                runs = downloader.list_available_runs()
                if runs:
                    print(f"### Available runs:")
                    for run in runs:
                        print(f"  - {run}")
                else:
                    print("### No runs found")
        else:
            # Download mode - download all runs
            downloader.download_results(args.output_directory, args.force)
            print("### Download completed successfully!")

    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
