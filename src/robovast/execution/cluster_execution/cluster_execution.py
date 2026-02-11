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
import logging
import os
import sys
import tempfile
import time

import yaml
from kubernetes import client
from kubernetes import config as kube_config

from robovast.common import (get_execution_env_variables, get_run_id,
                             load_config, prepare_run_configs)
from robovast.common.config_generation import generate_scenario_variations
from robovast.execution.cluster_execution.kubernetes import (
    check_pod_running, copy_config_to_cluster)

from .manifests import JOB_TEMPLATE

logger = logging.getLogger(__name__)


class JobRunner:
    def __init__(self, config_path, single_config=None, num_runs=None, cluster_config=None):
        self.cluster_config = cluster_config
        if not self.cluster_config:
            raise ValueError("Cluster config must be provided to JobRunner")

        # Store config path for later use
        self.config_path = config_path

        # Generate unique run ID
        self.run_id = get_run_id()

        parameters = load_config(config_path, subsection="execution")

        # Use provided num_runs if specified, otherwise use config value or default to 1
        if num_runs is not None:
            self.num_runs = num_runs
        elif "runs" in parameters:
            self.num_runs = parameters["runs"]
        else:
            self.num_runs = 1

        # Store pre_command and post_command if provided
        self.pre_command = parameters.get("pre_command")
        self.post_command = parameters.get("post_command")

        self.run_as_user = parameters.get("run_as_user", 1000)

        self.manifest = self.get_job_manifest(parameters["image"],
                                              parameters["kubernetes"]["resources"],
                                              parameters.get("env", []),
                                              self.run_as_user)

        # Generate configs with filtered files
        self.config_output_file_dir = tempfile.TemporaryDirectory(prefix="robovast_execution_")
        self.configs, _ = generate_scenario_variations(
            config_path,
            None,
            variation_classes=None,
            output_dir=self.config_output_file_dir.name,
        )

        if not self.configs:
            raise ValueError("No scenario configs generated.")
        # Filter scenarios if single_config is specified
        if single_config:
            found_config = None
            for config in self.configs:
                if config["name"] == single_config:
                    found_config = config
                    break

            if not found_config:
                logger.error(f"Config '{single_config}' not found in config.")
                logger.error("Available configs:")
                for v in self.configs:
                    logger.error(f"   - {v["name"]}")
                sys.exit(1)
            self.configs = [found_config]

        kube_config.load_kube_config()
        self.k8s_client = client.CoreV1Api()
        self.k8s_batch_client = client.BatchV1Api()
        self.k8s_api_client = client.ApiClient()

        # Check if transfer-pod exists
        if not check_pod_running(self.k8s_client, "robovast"):
            logger.error(f"Transfer pod 'robovast' is not available!")
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
            
            # Add PRE_COMMAND and POST_COMMAND if specified
            if self.pre_command:
                containers[0]['env'].append({
                    'name': 'PRE_COMMAND',
                    'value': str(self.pre_command)
                })
            if self.post_command:
                containers[0]['env'].append({
                    'name': 'POST_COMMAND',
                    'value': str(self.post_command)
                })

            # Add volume mounts
            if 'volumeMounts' not in containers[0]:
                containers[0]['volumeMounts'] = []

            volume_mounts = [
                {
                    'name': 'data-storage',
                    'mountPath': '/config',
                    'subPath': f'out/{self.run_id}/{scenario_key}/_config',
                    'readOnly': True
                },
                {
                    'name': 'data-storage',
                    'mountPath': '/out',
                    'subPath': f'out/{self.run_id}/{scenario_key}/{run_number}',
                    'readOnly': False
                },
                {
                    'name': 'data-storage',
                    'mountPath': '/entrypoint.sh',
                    'subPath': f'out/{self.run_id}/entrypoint.sh',
                    'readOnly': True
                }
            ]
            containers[0]['volumeMounts'].extend(volume_mounts)

        # Get runAsUser from container to match ownership in initContainer
        run_as_user = 1000
        if containers and 'securityContext' in containers[0] and 'runAsUser' in containers[0]['securityContext']:
            run_as_user = containers[0]['securityContext']['runAsUser']

        # Add initContainer to fix permissions
        job_manifest['spec']['template']['spec']['initContainers'] = [
            {
                'name': 'fix-permissions',
                'image': 'alpine:latest',
                'command': ['sh', '-c', f'chown -R {run_as_user}:{run_as_user} /out'],
                'securityContext': {
                    'runAsUser': 0
                },
                'volumeMounts': [
                    {
                        'name': 'data-storage',
                        'mountPath': '/out',
                        'subPath': f'out/{self.run_id}/{scenario_key}/{run_number}'
                    }
                ]
            }
        ]

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
                    logger.warning(f"Could not get pod start time for job {job_name}: {e}")

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
                    logger.warning(f"Could not determine actual running start time for job {job_name}")

            except Exception as e:
                logger.warning(f"Could not collect statistics for job {job_name}: {e}")

    def print_run_statistics(self):
        """Print comprehensive statistics about the run"""
        if not self.job_statistics:
            logger.info("No job statistics available")
            return

        logger.info("")
        logger.info("=" * 80)
        logger.info("RUN STATISTICS")
        logger.info("=" * 80)

        # Overall run duration
        if self.run_start_time and self.run_end_time:
            total_run_duration = (self.run_end_time - self.run_start_time).total_seconds()
            logger.info(f"Total run duration: {self.format_duration(total_run_duration)}")
            logger.info(f"Run started: {self.run_start_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
            logger.info(f"Run ended: {self.run_end_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
            logger.info("")

        # Job statistics
        completed_jobs = [job for job, stats in self.job_statistics.items() if stats['status'] == 'completed']
        failed_jobs = [job for job, stats in self.job_statistics.items() if stats['status'] == 'failed']

        logger.info(f"Total jobs: {len(self.job_statistics)}")
        logger.info(f"Completed successfully: {len(completed_jobs)}")
        logger.info(f"Failed: {len(failed_jobs)}")
        logger.info("")

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

            logger.info("Job Duration Statistics (completed jobs only):")
            logger.info(f"  Average: {self.format_duration(avg_duration)}")
            logger.info(f"  Median:  {self.format_duration(median_duration)}")
            logger.info(f"  Minimum: {self.format_duration(min_duration)}")
            logger.info(f"  Maximum: {self.format_duration(max_duration)}")
            logger.info("")

            # Find fastest and slowest jobs
            fastest_job = min(completed_jobs, key=lambda job: self.job_statistics[job]['duration_seconds'])
            slowest_job = max(completed_jobs, key=lambda job: self.job_statistics[job]['duration_seconds'])

            logger.info(f"Fastest job: {fastest_job} ({self.format_duration(self.job_statistics[fastest_job]['duration_seconds'])})")
            logger.info(f"Slowest job: {slowest_job} ({self.format_duration(self.job_statistics[slowest_job]['duration_seconds'])})")
            logger.info("")

        if failed_jobs:
            logger.info("Failed jobs:")
            for job in failed_jobs[:10]:  # Show first 10 failed jobs
                stats = self.job_statistics[job]
                if stats['start_time']:
                    duration = self.format_duration(stats['duration_seconds']) if stats['duration_seconds'] > 0 else "Unknown"
                    logger.info(f"  {job} (duration: {duration})")
                else:
                    logger.info(f"  {job} (never started)")

            if len(failed_jobs) > 10:
                logger.info(f"  ... and {len(failed_jobs) - 10} more failed jobs")
            logger.info("")

        logger.info("=" * 80)

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
            logger.debug(f"Deleting all jobs with label 'jobgroup=scenario-runs'")
            self.k8s_batch_client.delete_collection_namespaced_job(
                namespace="default",
                label_selector="jobgroup=scenario-runs",
                body=client.V1DeleteOptions(grace_period_seconds=0)
            )
            logger.info(f"Successfully deleted all scenario-runs jobs")
        except client.rest.ApiException as e:
            logger.error(f"Error deleting jobs with label selector: {e}")

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
                logger.debug(f"Found existing job to cleanup: {job.metadata.name}")

        if jobs_to_cleanup:
            logger.debug(f"Cleaning up {len(jobs_to_cleanup)} existing jobs...")
            self.cleanup_jobs()

        # Clean up existing pods with label jobgroup: scenario-runs
        self.cleanup_pods()

        # upload all config files to transfer PVC via transfer pod
        logger.debug(f"Uploading task config files for {len(self.configs)} scenarios to cluster...")
        self.upload_tasks_to_transfer_pod()

        # Create all jobs for all runs before executing any
        all_jobs = []
        logger.info(f"Creating {len(self.configs)} config(s) with {self.num_runs} runs each (ID: {self.run_id})...")
        for run_number in range(self.num_runs):
            logger.debug(f"Creating jobs for run {run_number + 1}/{self.num_runs}")

            for config in self.configs:
                config_name = config.get("name")
                job_manifest = self.create_job_manifest_for_scenario(config_name, run_number)
                job_name = job_manifest['metadata']['name']
                all_jobs.append(job_name)
                self.k8s_batch_client.create_namespaced_job(namespace="default", body=job_manifest)
                logger.debug(f"Created job {job_name} for run {run_number + 1}")

        logger.info(f"All {len(all_jobs)} jobs created. Starting execution...")
        # Track run start time
        self.run_start_time = datetime.datetime.now(datetime.timezone.utc)

        # Wait for all jobs to complete
        try:
            while True:
                job_status = self.get_remaining_jobs(all_jobs)

                if job_status:
                    logger.info(f"Waiting for {len(job_status)} out of {len(all_jobs)} jobs to finish...")
                    time.sleep(1)
                else:
                    break
            logger.info("All jobs finished.")
        except KeyboardInterrupt:
            logger.info("\nKeyboard interrupt received, cleaning up...")

        # Track run end time and collect statistics
        self.run_end_time = datetime.datetime.now(datetime.timezone.utc)
        logger.info("Collecting job statistics...")
        self.collect_job_statistics(all_jobs)

        # Clean up
        self.cleanup_pods()
        self.cleanup_jobs()
        logger.info(f"Cleaned up jobs.")

        # Print comprehensive statistics
        self.print_run_statistics()

        logger.debug(f"Transfer PVC and pod are left running for reuse")

    def cleanup_pods(self):
        # Cleanup pods with label jobgroup: scenario-runs
        try:
            logger.debug(f"Deleting all pods with label 'jobgroup=scenario-runs'")
            self.k8s_client.delete_collection_namespaced_pod(
                namespace="default",
                label_selector="jobgroup=scenario-runs",
                body=client.V1DeleteOptions(grace_period_seconds=0)
            )
            logger.debug(f"Successfully cleaned up all scenario-runs pods")
        except client.rest.ApiException as e:
            logger.error(f"Error deleting pods with label selector: {e}")

    def upload_tasks_to_transfer_pod(self):
        """Upload all files to transfer PVC using kubectl cp to transfer pod"""

        # Create a temporary directory to organize all files
        with tempfile.TemporaryDirectory() as temp_dir:
            logger.debug(f"Using temporary directory: {temp_dir}")

            out_dir = os.path.join(temp_dir, "out_template", self.run_id)
            prepare_run_configs(out_dir, self.configs)

            copy_config_to_cluster(os.path.join(temp_dir, "out_template"), self.run_id)

    def get_job_manifest(self, image: str, kubernetes_resources: dict, env: list, run_as_user: int = None) -> dict:
        """Generate the base Kubernetes job manifest from templates.

        Args:
            image: Docker image to use
            kubernetes_resources: Resource limits/requests
            env: List of environment variables
            run_as_user: UID to run container as (defaults to 1000 if None)

        Returns:
            Dictionary containing the job manifest
        """
        if run_as_user is None:
            run_as_user = 1000

        logger.debug(f"Using run_as_user={run_as_user} for job containers")

        yaml_str = JOB_TEMPLATE.format(image=image, cpu=kubernetes_resources["cpu"], volumes=self.cluster_config.get_job_volumes())
        manifest = yaml.safe_load(yaml_str)
        
        manifest['spec']['template']['spec']['containers'][0]['securityContext']['runAsUser'] = run_as_user

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
