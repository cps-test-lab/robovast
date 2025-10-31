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

import argparse
import copy
import datetime
import os
import subprocess
import sys
import tempfile
import time
import yaml

from kubernetes import client, config
from robovast.common import (get_execution_env_variables,
                             get_execution_variants, load_config,
                             prepare_run_configs)

class JobRunner:
    def __init__(self, variation_config, single_variant=None):
        self.single_variant = single_variant
        self.transfer_pvc_name = "transfer-pvc"
        self.transfer_pod_name = "nfs-server"
        
        # Generate unique run ID
        self.run_id = f"run-{datetime.datetime.now().strftime('%Y-%m-%d-%H%M%S')}"

        parameters = load_config(variation_config, subsection="execution")

        self.num_runs = 1
        if "runs" in parameters:
            self.num_runs = parameters["runs"]
        self.manifest_file_path = os.path.join(os.path.dirname(variation_config), parameters["kubernetes_manifest"])

        self.scenarios = get_execution_variants(variation_config)

        config.load_kube_config()
        self.k8s_client = client.CoreV1Api()
        self.k8s_batch_client = client.BatchV1Api()
        self.k8s_api_client = client.ApiClient()

        # Check if transfer-pvc exists
        # self.check_transfer_pvc_exists()

        # Check if transfer-pod exists
        self.check_transfer_pod_exists()

        # Initialize statistics tracking
        self.run_start_time = None
        self.run_end_time = None
        self.job_statistics = {}

    def replace_template(self, elem, tmpl, idx):
        if isinstance(elem, dict):
            for key, value in elem.items():
                elem[key] = self.replace_template(value, tmpl, idx)
        elif isinstance(elem, list):
            for i, item in enumerate(elem):
                elem[i] = self.replace_template(item, tmpl, idx)
        elif isinstance(elem, str):
            elem = elem.replace(tmpl, str(idx))
        return elem

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

    def get_remaining_jobs(self, job_names):
        running_jobs = []
        for job_name in job_names:
            job_status = self.k8s_batch_client.read_namespaced_job_status(name=job_name, namespace="default")

            # Check if job is still active/running
            if job_status.status.active is not None and job_status.status.active >= 1:
                running_jobs.append(job_name)
            # Check if job has not completed yet (no completion_time and no failure)
            elif job_status.status.completion_time is None and (job_status.status.failed is None or job_status.status.failed == 0):
                running_jobs.append(job_name)
        return running_jobs

    def collect_job_statistics(self, job_names):
        """Collect statistics for completed jobs"""
        for job_name in job_names:
            try:
                job_status = self.k8s_batch_client.read_namespaced_job_status(name=job_name, namespace="default")

                start_time = job_status.status.start_time
                completion_time = job_status.status.completion_time

                # Only collect stats for jobs that have both start and completion times
                if start_time and completion_time:
                    duration = (completion_time - start_time).total_seconds()
                    succeeded = job_status.status.succeeded or 0
                    failed = job_status.status.failed or 0

                    self.job_statistics[job_name] = {
                        'start_time': start_time,
                        'completion_time': completion_time,
                        'duration_seconds': duration,
                        'succeeded': succeeded,
                        'failed': failed,
                        'status': 'completed' if succeeded > 0 else 'failed' if failed > 0 else 'unknown'
                    }

            except Exception as e:
                print(f"### Warning: Could not collect statistics for job {job_name}: {e}")

    def print_run_statistics(self):
        """Print comprehensive statistics about the run"""
        if not self.job_statistics:
            print("### No job statistics available")
            return

        print("\n" + "=" * 80)
        print("RUN STATISTICS")
        print("=" * 80)

        # Overall run duration
        if self.run_start_time and self.run_end_time:
            total_run_duration = (self.run_end_time - self.run_start_time).total_seconds()
            print(f"Total run duration: {self.format_duration(total_run_duration)}")
            print(f"Run started: {self.run_start_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
            print(f"Run ended: {self.run_end_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
            print()

        # Job statistics
        completed_jobs = [job for job, stats in self.job_statistics.items() if stats['status'] == 'completed']
        failed_jobs = [job for job, stats in self.job_statistics.items() if stats['status'] == 'failed']

        print(f"Total jobs: {len(self.job_statistics)}")
        print(f"Completed successfully: {len(completed_jobs)}")
        print(f"Failed: {len(failed_jobs)}")
        print()

        if completed_jobs:
            # Duration statistics for completed jobs
            durations = [self.job_statistics[job]['duration_seconds'] for job in completed_jobs]

            avg_duration = sum(durations) / len(durations)
            min_duration = min(durations)
            max_duration = max(durations)

            # Calculate median
            sorted_durations = sorted(durations)
            n = len(sorted_durations)
            if n % 2 == 0:
                median_duration = (sorted_durations[n//2-1] + sorted_durations[n//2]) / 2
            else:
                median_duration = sorted_durations[n//2]

            print("Job Duration Statistics (completed jobs only):")
            print(f"  Average: {self.format_duration(avg_duration)}")
            print(f"  Median:  {self.format_duration(median_duration)}")
            print(f"  Minimum: {self.format_duration(min_duration)}")
            print(f"  Maximum: {self.format_duration(max_duration)}")
            print()

            # Find fastest and slowest jobs
            fastest_job = min(completed_jobs, key=lambda job: self.job_statistics[job]['duration_seconds'])
            slowest_job = max(completed_jobs, key=lambda job: self.job_statistics[job]['duration_seconds'])

            print(f"Fastest job: {fastest_job} ({self.format_duration(self.job_statistics[fastest_job]['duration_seconds'])})")
            print(f"Slowest job: {slowest_job} ({self.format_duration(self.job_statistics[slowest_job]['duration_seconds'])})")
            print()

        if failed_jobs:
            print("Failed jobs:")
            for job in failed_jobs[:10]:  # Show first 10 failed jobs
                stats = self.job_statistics[job]
                if stats['start_time']:
                    duration = self.format_duration(stats['duration_seconds']) if stats['duration_seconds'] > 0 else "Unknown"
                    print(f"  {job} (duration: {duration})")
                else:
                    print(f"  {job} (never started)")

            if len(failed_jobs) > 10:
                print(f"  ... and {len(failed_jobs) - 10} more failed jobs")
            print()

        print("=" * 80)

    def format_duration(self, seconds):
        """Format duration in a human-readable way"""
        if seconds < 60:
            return f"{seconds:.1f}s"
        elif seconds < 3600:
            minutes = int(seconds // 60)
            secs = seconds % 60
            return f"{minutes}m {secs:.1f}s"
        else:
            hours = int(seconds // 3600)
            minutes = int((seconds % 3600) // 60)
            secs = seconds % 60
            return f"{hours}h {minutes}m {secs:.1f}s"

    def cleanup_jobs(self):
        # cleanup all jobs with label jobgroup: scenario-runs in a single call
        try:
            print(f"### Deleting all jobs with label 'jobgroup=scenario-runs'")
            self.k8s_batch_client.delete_collection_namespaced_job(
                namespace="default",
                label_selector="jobgroup=scenario-runs",
                body=client.V1DeleteOptions(grace_period_seconds=0)
            )
            print(f"### Successfully deleted all scenario-runs jobs")
        except client.rest.ApiException as e:
            print(f"### Error deleting jobs with label selector: {e}")

    def run(self):
        # check if k8s element names have "$ITEM" template
        with open(self.manifest_file_path, 'r') as f:
            manifest_data = list(yaml.safe_load_all(f))
            if len(manifest_data) > 1:
                raise ValueError("Manifest file should contain only one YAML document")
            manifest_data = manifest_data[0]
            if '$SCENARIO_ID' not in manifest_data['metadata']['name']:
                raise ValueError("Manifest element names need to contain '$SCENARIO_ID' template")

        # Cleaning up previous k8s elements
        # Get the job name prefix by replacing templates
        job_prefix = manifest_data['metadata']['name'].replace("$SCENARIO_ID", "").replace("$ITEM", "")

        # Clean up existing jobs that match our naming pattern
        job_list = self.k8s_batch_client.list_namespaced_job(namespace="default")
        jobs_to_cleanup = []
        for job in job_list.items:
            if job.metadata.name.startswith(job_prefix):
                jobs_to_cleanup.append(job.metadata.name)
                print(f"### Found existing job to cleanup: {job.metadata.name}")

        if jobs_to_cleanup:
            print(f"### Cleaning up {len(jobs_to_cleanup)} existing jobs")
            self.cleanup_jobs()

        # Clean up existing pods with label jobgroup: scenario-runs
        self.cleanup_pods()

        # upload all config files to transfer PVC via transfer pod
        print(f"### Uploading task config files for {len(self.scenarios)} scenarios to transfer PVC...")
        self.upload_tasks_to_transfer_pod()

        # Create all jobs for all runs before executing any
        all_jobs = []

        print(f"### Creating {self.num_runs} runs with {len(self.scenarios)} scenarios each (ID: {self.run_id})...")
        for run_number in range(self.num_runs):
            print(f"### Creating jobs for run {run_number + 1}/{self.num_runs}")

            for scenario_idx in range(len(self.scenarios)):
                scenario_key = list(self.scenarios.keys())[scenario_idx]
                with open(self.manifest_file_path, 'r') as f:
                    manifest_data = yaml.safe_load_all(f)
                    for elem in manifest_data:
                        # Create a deep copy to avoid modifying the original
                        elem_copy = copy.deepcopy(elem)

                        self.replace_template(elem_copy, "$ITEM", f"{scenario_key.replace('/', '-').replace('_', '-')}-{run_number}")
                        self.replace_template(elem_copy, "$RUN_ID", self.run_id)
                        self.replace_template(elem_copy, "$SCENARIO_CONFIG", f"{scenario_key}")
                        self.replace_template(elem_copy, "$RUN_NUM", f"{run_number}")
                        self.replace_template(elem_copy, "$SCENARIO_ID", f"{scenario_key}-{run_number}")

                        # Add environment variables to the container
                        containers = elem_copy['spec']['template']['spec']['containers']
                        if containers:
                            if 'env' not in containers[0]:
                                containers[0]['env'] = []
                            
                            # Add the required environment variables
                            env_vars = get_execution_env_variables(run_number, scenario_key)
                    
                            for name, val in env_vars.items():
                                containers[0]['env'].append({'name': str(name), 'value': "" if val is None else str(val)})
                            
                            # Add volume mounts to the container
                            if 'volumeMounts' not in containers[0]:
                                containers[0]['volumeMounts'] = []
                            
                            volume_mounts = [
                                {
                                    'name': 'transfer-storage',
                                    'mountPath': '/config',
                                    'subPath': f'config/{self.run_id}/{scenario_key}',
                                    'readOnly': True
                                },
                                {
                                    'name': 'transfer-storage',
                                    'mountPath': '/out',
                                    'subPath': f'out/{self.run_id}/{scenario_key}/{run_number}',
                                    'readOnly': False
                                }
                            ]
                            
                            containers[0]['volumeMounts'].extend(volume_mounts)

                        job_name = elem_copy['metadata']['name']
                        all_jobs.append(job_name)
                        self.k8s_batch_client.create_namespaced_job(namespace="default", body=elem_copy)
                        print(f"### Created job {job_name} for run {run_number + 1}")

        print(f"### All {len(all_jobs)} jobs created. Starting execution...")

        # Track run start time
        self.run_start_time = datetime.datetime.now(datetime.timezone.utc)

        # Wait for all jobs to complete
        try:
            while True:
                job_status = self.get_remaining_jobs(all_jobs)

                if job_status:
                    print(f"### Waiting for {len(job_status)} out of {len(all_jobs)} jobs to finish...")
                    time.sleep(1)
                else:
                    break
            print("### All jobs finished.")
        except KeyboardInterrupt:
            print("\n### Keyboard interrupt received, cleaning up...")

        # Track run end time and collect statistics
        self.run_end_time = datetime.datetime.now(datetime.timezone.utc)
        print("### Collecting job statistics...")
        self.collect_job_statistics(all_jobs)

        # Clean up
        self.cleanup_pods()
        self.cleanup_jobs()
        print(f"### Cleaned up jobs")

        # Print comprehensive statistics
        self.print_run_statistics()

        print(f"### Transfer PVC and pod are left running for reuse")

    def cleanup_pods(self):
        # Cleanup pods with label jobgroup: scenario-runs
        try:
            print(f"### Deleting all pods with label 'jobgroup=scenario-runs'")
            self.k8s_client.delete_collection_namespaced_pod(
                namespace="default",
                label_selector="jobgroup=scenario-runs",
                body=client.V1DeleteOptions(grace_period_seconds=0)
            )
            print(f"### Successfully cleaned up all scenario-runs pods")
        except client.rest.ApiException as e:
            print(f"### Error deleting pods with label selector: {e}")

    
    def upload_tasks_to_transfer_pod(self):
        """Upload all files to transfer PVC using kubectl cp to transfer pod"""

        # Create a temporary directory to organize all files
        with tempfile.TemporaryDirectory() as temp_dir:
            print(f"### Using temporary directory: {temp_dir}")

            prepare_run_configs(self.run_id, self.scenarios, temp_dir)

            # Use kubectl cp to copy the entire config directory to the transfer pod
            try:
                print(f"### Copying config files to transfer pod using kubectl cp...")

                # Copy the config directory to the transfer pod at /exports/config/
                cmd = [
                    "kubectl", "cp",
                    os.path.join(temp_dir, "config"),
                    f"default/{self.transfer_pod_name}:/exports/"
                ]

                subprocess.run(cmd, capture_output=True, text=True, check=True)
                print(f"### Successfully copied config files to transfer pod")

                # Verify the copy was successful by listing the directory
                verify_cmd = [
                    "kubectl", "exec", "-n", "default", self.transfer_pod_name,
                    "--",
                    "ls", "-la", f"/exports/config/{self.run_id}"
                ]

                verify_result = subprocess.run(verify_cmd, capture_output=True, text=True, check=False)
                if verify_result.returncode == 0:
                    print(f"### Verification: Config files successfully uploaded to /exports/config/{self.run_id}/")
                    print(f"### Directory contents:\n{verify_result.stdout}")
                else:
                    print(f"### Warning: Could not verify config file upload: {verify_result.stderr}")

            except subprocess.CalledProcessError as e:
                print(f"### ERROR: Failed to copy config files to transfer pod: {e}")
                print(f"### stdout: {e.stdout}")
                print(f"### stderr: {e.stderr}")
                sys.exit(1)
            except Exception as e:
                print(f"### ERROR: Unexpected error during config file copy: {e}")
                sys.exit(1)
