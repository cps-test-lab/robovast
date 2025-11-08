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

import copy
import datetime
import os
import sys
import tempfile
import time

import yaml
from kubernetes import client, config

from robovast.common import (get_execution_env_variables,
                             get_execution_variants, load_config,
                             prepare_run_configs)
from robovast.common.kubernetes import (check_pod_running,
                                        copy_config_to_cluster)

from .manifests import JOB_TEMPLATE


class JobRunner:
    def __init__(self, variation_config, single_variant=None, num_runs=None, cluster_config=None):
        self.single_variant = single_variant
        self.cluster_config = cluster_config
        if not self.cluster_config:
            raise ValueError("Cluster config must be provided to JobRunner")

        # Generate unique run ID
        self.run_id = f"run-{datetime.datetime.now().strftime('%Y-%m-%d-%H%M%S')}"

        parameters = load_config(variation_config, subsection="execution")

        # Use provided num_runs if specified, otherwise use config value or default to 1
        if num_runs is not None:
            self.num_runs = num_runs
            print(f"### Overriding config: Running {self.num_runs} runs")
        elif "runs" in parameters:
            self.num_runs = parameters["runs"]
        else:
            self.num_runs = 1

        self.manifest = self.get_job_manifest(parameters["image"],
                                              parameters["kubernetes"]["resources"],
                                              parameters.get("env", []))

        self.scenarios, self.variant_output_file_dir = get_execution_variants(variation_config)

        # Filter scenarios if single_variant is specified
        if self.single_variant:
            if self.single_variant not in self.scenarios:
                print(f"### ERROR: Variant '{self.single_variant}' not found in config.")
                print("### Available variants:")
                for v in self.scenarios:
                    print(f"###   - {v}")
                sys.exit(1)
            print(f"### Running single variant: {self.single_variant}")
            self.scenarios = {self.single_variant: self.scenarios[self.single_variant]}

        config.load_kube_config()
        self.k8s_client = client.CoreV1Api()
        self.k8s_batch_client = client.BatchV1Api()
        self.k8s_api_client = client.ApiClient()

        # Check if transfer-pod exists
        if not check_pod_running(self.k8s_client, "robovast"):
            print(f"### ERROR: Transfer pod 'robovast' is not available!")
            sys.exit(1)

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

    def create_job_manifest_for_scenario(self, scenario_key: str, run_number: int) -> dict:
        """Create a complete job manifest for a specific scenario and run number.

        Args:
            scenario_key: The scenario identifier
            run_number: The run number for this scenario

        Returns:
            A complete Kubernetes job manifest dictionary
        """
        # Create a deep copy of the base manifest
        job_manifest = copy.deepcopy(self.manifest)

        # Replace template variables
        self.replace_template(job_manifest, "$ITEM",
                              f"{scenario_key.replace('/', '-').replace('_', '-')}-{run_number}")
        self.replace_template(job_manifest, "$RUN_ID", self.run_id)
        self.replace_template(job_manifest, "$SCENARIO_CONFIG", scenario_key)
        self.replace_template(job_manifest, "$RUN_NUM", str(run_number))
        self.replace_template(job_manifest, "$SCENARIO_ID",
                              f"{scenario_key}-{run_number}")

        # Add environment variables and volume mounts to the container
        containers = job_manifest['spec']['template']['spec']['containers']
        if containers:
            # Add environment variables
            if 'env' not in containers[0]:
                containers[0]['env'] = []

            env_vars = get_execution_env_variables(run_number, scenario_key)
            for name, val in env_vars.items():
                containers[0]['env'].append({
                    'name': str(name),
                    'value': "" if val is None else str(val)
                })

            # Add volume mounts
            if 'volumeMounts' not in containers[0]:
                containers[0]['volumeMounts'] = []

            volume_mounts = [
                {
                    'name': 'data-storage',
                    'mountPath': '/config',
                    'subPath': f'config/{self.run_id}/{scenario_key}',
                    'readOnly': False
                },
                {
                    'name': 'data-storage',
                    'mountPath': '/out',
                    'subPath': f'out/{self.run_id}/{scenario_key}/{run_number}',
                    'readOnly': False
                }
            ]
            containers[0]['volumeMounts'].extend(volume_mounts)

        return job_manifest

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

                completion_time = job_status.status.completion_time
                succeeded = job_status.status.succeeded or 0
                failed = job_status.status.failed or 0

                # Get the actual pod start time (when it started running, not pending)
                pod_start_time = None
                try:
                    # Get pods associated with this job
                    pods = self.k8s_client.list_namespaced_pod(
                        namespace="default",
                        label_selector=f"job-name={job_name}"
                    )

                    # Find the earliest pod start time (when container actually started)
                    for pod in pods.items:
                        if pod.status.start_time:
                            # Use container start time if available, otherwise use pod start time
                            if pod.status.container_statuses:
                                for container_status in pod.status.container_statuses:
                                    if container_status.state.running and container_status.state.running.started_at:
                                        container_start = container_status.state.running.started_at
                                        if pod_start_time is None or container_start < pod_start_time:
                                            pod_start_time = container_start
                                    elif container_status.state.terminated and container_status.state.terminated.started_at:
                                        container_start = container_status.state.terminated.started_at
                                        if pod_start_time is None or container_start < pod_start_time:
                                            pod_start_time = container_start
                            # Fallback to pod start time if no container info available
                            if pod_start_time is None:
                                pod_start_time = pod.status.start_time
                except Exception as e:
                    print(f"### Warning: Could not get pod start time for job {job_name}: {e}")

                # Only collect stats for jobs that have both start and completion times
                if pod_start_time and completion_time:
                    duration = (completion_time - pod_start_time).total_seconds()

                    self.job_statistics[job_name] = {
                        'start_time': pod_start_time,
                        'completion_time': completion_time,
                        'duration_seconds': duration,
                        'succeeded': succeeded,
                        'failed': failed,
                        'status': 'completed' if succeeded > 0 else 'failed' if failed > 0 else 'unknown'
                    }
                elif completion_time:
                    # Job completed but we couldn't determine actual start time
                    print(f"### Warning: Could not determine actual running start time for job {job_name}")

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
        manifest_data = self.manifest
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
        create_start_time = time.time()
        print(f"### Creating {self.num_runs} runs with {len(self.scenarios)} scenarios each (ID: {self.run_id})...")
        for run_number in range(self.num_runs):
            print(f"### Creating jobs for run {run_number + 1}/{self.num_runs}")

            for scenario_key in self.scenarios:
                job_manifest = self.create_job_manifest_for_scenario(scenario_key, run_number)
                job_name = job_manifest['metadata']['name']
                all_jobs.append(job_name)
                self.k8s_batch_client.create_namespaced_job(namespace="default", body=job_manifest)
                print(f"### Created job {job_name} for run {run_number + 1}")

        print(f"### All {len(all_jobs)} jobs created. Starting execution...")
        create_end_time = time.time()
        print(f"### {create_end_time - create_start_time:.2f} seconds to create all jobs")
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

            prepare_run_configs(self.run_id, self.scenarios, self.variant_output_file_dir.name, temp_dir)

            copy_config_to_cluster(os.path.join(temp_dir, "config"), self.run_id)

    def get_job_manifest(self, image: str, kubernetes_resources: dict, env: list) -> dict:
        yaml_str = JOB_TEMPLATE.format(image=image, cpu=kubernetes_resources["cpu"], volumes=self.cluster_config.get_job_volumes())
        manifest = yaml.safe_load(yaml_str)
        if "memory" in kubernetes_resources:
            # Add memory resource if specified
            manifest['spec']['template']['spec']['containers'][0]['resources'] = {
                'limits': {
                    'memory': kubernetes_resources["memory"]
                },
                'requests': {
                    'memory': kubernetes_resources["memory"]
                }
            }
        for env_var in env:
            manifest['spec']['template']['spec']['containers'][0].setdefault('env', []).append({
                'name': env_var["name"],
                'value': env_var["value"]
            })
        return manifest
