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
import warnings

import yaml
from kubernetes import client
from kubernetes import config as kube_config

from robovast.common import (create_execution_yaml,
                             get_execution_env_variables, get_run_id,
                             load_config, normalize_secondary_containers,
                             prepare_run_configs)
from robovast.common.config_generation import generate_scenario_variations
from robovast.execution.cluster_execution.kubernetes import (
    check_pod_running, upload_configs_to_s3)

from .manifests import JOB_TEMPLATE

logger = logging.getLogger(__name__)


def _label_safe_run_id(run_id: str) -> str:
    """Convert run_id to a valid Kubernetes label value.

    Label values must be 63 chars or less, alphanumeric, hyphens, periods.
    """
    s = run_id.lower().replace("_", "-")
    return "".join(c for c in s if c.isalnum() or c in "-.")[:63]


def cleanup_cluster_run(namespace="default", run_id=None):
    """Clean up scenario run jobs and pods from the cluster.

    If run_id is given, removes only jobs and pods for that run (with
    label jobgroup=scenario-runs,run-id=<run_id>). Otherwise removes all
    jobs and pods with label 'jobgroup=scenario-runs'.

    Used after a detached run to clean up resources once jobs have completed.
    """
    kube_config.load_kube_config()
    k8s_client = client.CoreV1Api()
    k8s_batch_client = client.BatchV1Api()

    label_selector = "jobgroup=scenario-runs"
    if run_id is not None:
        label_safe = _label_safe_run_id(run_id)
        label_selector = f"jobgroup=scenario-runs,run-id={label_safe}"

    # Cleanup jobs
    try:
        logger.debug(f"Deleting jobs with label selector '{label_selector}'")
        k8s_batch_client.delete_collection_namespaced_job(
            namespace=namespace,
            label_selector=label_selector,
            body=client.V1DeleteOptions(grace_period_seconds=0)
        )
        logger.info("Successfully deleted scenario-runs jobs")
    except client.rest.ApiException as e:
        logger.error(f"Error deleting jobs with label selector: {e}")
        raise

    # Cleanup pods
    try:
        logger.debug(f"Deleting pods with label selector '{label_selector}'")
        k8s_client.delete_collection_namespaced_pod(
            namespace=namespace,
            label_selector=label_selector,
            body=client.V1DeleteOptions(grace_period_seconds=0)
        )
        logger.debug("Successfully cleaned up scenario-runs pods")
    except client.rest.ApiException as e:
        logger.error(f"Error deleting pods with label selector: {e}")
        raise


def get_cluster_run_job_counts(namespace="default"):
    """Get aggregate status counts for scenario run jobs.

    Counts all Kubernetes jobs in the given namespace with the label
    ``jobgroup=scenario-runs`` and classifies each job as completed,
    failed, running, or pending based on its status fields.
    """
    kube_config.load_kube_config()
    k8s_batch_client = client.BatchV1Api()

    try:
        job_list = k8s_batch_client.list_namespaced_job(
            namespace=namespace,
            label_selector="jobgroup=scenario-runs",
        )
    except client.rest.ApiException as e:
        logger.error(f"Error listing jobs with label selector: {e}")
        raise

    counts = {
        "completed": 0,
        "failed": 0,
        "running": 0,
        "pending": 0,
    }

    for job in job_list.items:
        status = job.status
        if status is None:
            counts["pending"] += 1
            continue

        succeeded = status.succeeded or 0
        failed = status.failed or 0
        active = status.active or 0

        if succeeded >= 1:
            counts["completed"] += 1
        elif failed >= 1:
            counts["failed"] += 1
        elif active >= 1:
            counts["running"] += 1
        else:
            counts["pending"] += 1

    return counts


def get_cluster_run_job_counts_per_run(namespace="default"):
    """Get status counts per run_id for scenario run jobs.

    Returns a dict mapping run_id (or "<legacy>" for jobs without run-id label)
    to counts dict with keys completed, failed, running, pending.
    """
    kube_config.load_kube_config()
    k8s_batch_client = client.BatchV1Api()

    try:
        job_list = k8s_batch_client.list_namespaced_job(
            namespace=namespace,
            label_selector="jobgroup=scenario-runs",
        )
    except client.rest.ApiException as e:
        logger.error(f"Error listing jobs with label selector: {e}")
        raise

    per_run = {}

    for job in job_list.items:
        run_id = "<legacy>"
        if job.metadata.labels and "run-id" in job.metadata.labels:
            run_id = job.metadata.labels["run-id"]

        if run_id not in per_run:
            per_run[run_id] = {"completed": 0, "failed": 0, "running": 0, "pending": 0}

        status = job.status
        if status is None:
            per_run[run_id]["pending"] += 1
            continue

        succeeded = status.succeeded or 0
        failed = status.failed or 0
        active = status.active or 0

        if succeeded >= 1:
            per_run[run_id]["completed"] += 1
        elif failed >= 1:
            per_run[run_id]["failed"] += 1
        elif active >= 1:
            per_run[run_id]["running"] += 1
        else:
            per_run[run_id]["pending"] += 1

    return per_run


class JobRunner:
    def __init__(self, config_path, single_config=None, num_runs=None, cluster_config=None,
                 namespace="default", cleanup_before_run=False):
        self.cluster_config = cluster_config
        self.cleanup_before_run = cleanup_before_run
        self.namespace = namespace
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

        # Generate configs with filtered files first, so we have access to execution params
        self.config_output_file_dir = tempfile.TemporaryDirectory(prefix="robovast_execution_")
        self.run_data, _ = generate_scenario_variations(
            config_path,
            None,
            variation_classes=None,
            output_dir=self.config_output_file_dir.name,
        )
        self.configs = self.run_data["configs"]

        # Get execution parameters from run_data
        execution_params = self.run_data.get("execution", {})
        self.run_as_user = execution_params.get("run_as_user", 1000)

        # Create manifest with env vars from config
        self.manifest = self.get_job_manifest(parameters["image"],
                                              parameters.get("resources") or {},
                                              execution_params.get("env", []),
                                              self.run_as_user,
                                              execution_params.get("secondary_containers") or [])

        if not self.configs:
            raise ValueError("No scenario configs generated.")
        # Filter scenarios if single_config is specified
        if single_config:
            found_config = None
            for config in self.configs:
                config_name = config.get("name", "<unnamed>")
                if config_name == single_config:
                    found_config = config
                    break

            if not found_config:
                logger.error(f"Config '{single_config}' not found.")
                logger.info("Available configs:")
                for v in self.configs:
                    logger.info(f"   - {v.get('name', '<unnamed>')}")
                raise ValueError(f"Config '{single_config}' not found (see available configs above)")
            self.run_data["configs"] = [found_config]
            self.configs = [found_config]

        # Initialize k8s clients to None - will be initialized lazily when needed
        self.k8s_client = None
        self.k8s_batch_client = None
        self.k8s_api_client = None
        self._k8s_initialized = False

        # Initialize statistics tracking
        self.run_start_time = None
        self.run_end_time = None
        self.job_statistics = {}

    def _ensure_k8s_initialized(self):
        """Initialize Kubernetes clients if not already initialized.

        This is called lazily only when actually needed (e.g., during run()),
        not during prepare-run which just generates manifests.
        """
        if self._k8s_initialized:
            return

        logger.debug("Initializing Kubernetes connection...")
        kube_config.load_kube_config()
        self.k8s_client = client.CoreV1Api()
        self.k8s_batch_client = client.BatchV1Api()
        self.k8s_api_client = client.ApiClient()
        self._k8s_initialized = True

        # Check if transfer-pod exists
        if not check_pod_running(self.k8s_client, "robovast", self.namespace):
            logger.error(f"Transfer pod 'robovast' is not available!")
            sys.exit(1)
        logger.debug("Kubernetes connection initialized successfully.")

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
        label_safe_run_id = _label_safe_run_id(self.run_id)
        self.replace_template(job_manifest, "$RUN_ID", label_safe_run_id)
        self.replace_template(job_manifest, "$ITEM",
                              f"{scenario_key.replace('/', '-').replace('_', '-')}-{run_number}")
        self.replace_template(job_manifest, "$TEST_ID",
                              f"{scenario_key}-{run_number}")

        # S3 connection details from cluster config
        s3_endpoint = self.cluster_config.get_s3_endpoint()
        s3_access_key, s3_secret_key = self.cluster_config.get_s3_credentials()
        bucket_name = self._bucket_name_for_run(self.run_id)
        s3_prefix = f"{scenario_key}/{run_number}"

        spec = job_manifest['spec']['template']['spec']

        # Volumes: config (populated by initContainer), out (shared output), dshm (shared /dev/shm),
        # ipc (named sockets between main and secondary containers)
        spec['volumes'] = [
            {'name': 'config', 'emptyDir': {}},
            {'name': 'out', 'emptyDir': {}},
            {'name': 'dshm', 'emptyDir': {'medium': 'Memory'}},
            {'name': 'ipc', 'emptyDir': {}},
            {'name': 'tmp', 'emptyDir': {}},
        ]

        # Build the initContainer that downloads all needed files from S3 into /config/
        # After downloading, chmod +x all scripts so the containers can execute them.
        init_cmd = (
            f"mc alias set myminio \"$S3_ENDPOINT\" \"$S3_ACCESS_KEY\" \"$S3_SECRET_KEY\" && "
            f"mc cp myminio/$S3_BUCKET/entrypoint.sh /config/ && "
            f"mc cp myminio/$S3_BUCKET/secondary_entrypoint.sh /config/ && "
            f"mc cp myminio/$S3_BUCKET/collect_sysinfo.py /config/ && "
            f"mc cp myminio/$S3_BUCKET/scenario.osc /config/ && "
            f"mc mirror myminio/$S3_BUCKET/_config/ /config/ || true && "
            f"mc mirror myminio/$S3_BUCKET/{scenario_key}/_config/ /config/ || true && "
            f"mc cp myminio/$S3_BUCKET/{scenario_key}/scenario.config /config/ || true && "
            f"chmod +x /config/*.sh /config/*.py 2>/dev/null; true"
        )

        init_env = [
            {'name': 'S3_ENDPOINT', 'value': s3_endpoint},
            {'name': 'S3_BUCKET', 'value': bucket_name},
            {'name': 'S3_ACCESS_KEY', 'value': s3_access_key},
            {'name': 'S3_SECRET_KEY', 'value': s3_secret_key},
        ]

        spec['initContainers'] = [
            {
                'name': 's3-init',
                'image': 'minio/mc:latest',
                'command': ['sh', '-c', init_cmd],
                'env': init_env,
                'volumeMounts': [
                    {'name': 'config', 'mountPath': '/config'}
                ],
            }
        ]

        shared_volume_mounts = [
            {'name': 'config', 'mountPath': '/config', 'readOnly': True},
            {'name': 'out', 'mountPath': '/out', 'readOnly': False},
            {'name': 'dshm', 'mountPath': '/dev/shm'},
            {'name': 'ipc', 'mountPath': '/ipc'},
            {'name': 'tmp', 'mountPath': '/tmp'},
        ]

        # Add environment variables and volume mounts to the main (robovast) container
        containers = spec['containers']
        if containers:
            if 'env' not in containers[0]:
                containers[0]['env'] = []

            env_vars = get_execution_env_variables(run_number, scenario_key)
            for name, val in env_vars.items():
                containers[0]['env'].append({
                    'name': str(name),
                    'value': "" if val is None else str(val)
                })

            # S3 env vars for entrypoint post-run upload
            for k, v in [
                ('S3_ENDPOINT', s3_endpoint),
                ('S3_BUCKET', bucket_name),
                ('S3_ACCESS_KEY', s3_access_key),
                ('S3_SECRET_KEY', s3_secret_key),
                ('S3_PREFIX', s3_prefix),
            ]:
                containers[0]['env'].append({'name': k, 'value': v})

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

            containers[0]['volumeMounts'] = shared_volume_mounts

        # Add secondary containers
        for sc in self.secondary_containers:
            sc_name = sc['name']
            sc_resources = sc['resources']
            secondary_spec = {
                'name': sc_name,
                'image': job_manifest['spec']['template']['spec']['containers'][0]['image'],
                'command': ['/bin/bash', '/config/secondary_entrypoint.sh'],
                'env': [
                    {'name': 'CONTAINER_NAME', 'value': sc_name},
                    {'name': 'ROS_LOG_DIR', 'value': '/out/logs'},
                ],
                'resources': {
                    'requests': {},
                    'limits': {},
                },
                'volumeMounts': shared_volume_mounts,
            }
            if sc_resources.get('cpu'):
                secondary_spec['resources']['requests']['cpu'] = str(sc_resources['cpu'])
                secondary_spec['resources']['limits']['cpu'] = str(sc_resources['cpu'])
            if sc_resources.get('memory'):
                secondary_spec['resources']['requests']['memory'] = sc_resources['memory']
                secondary_spec['resources']['limits']['memory'] = sc_resources['memory']
            if self.run_as_user is not None:
                secondary_spec.setdefault('securityContext', {})['runAsUser'] = self.run_as_user
            containers.append(secondary_spec)

        return job_manifest

    @staticmethod
    def _bucket_name_for_run(run_id: str) -> str:
        """Convert a run_id into a valid S3 bucket name.

        Bucket names must be lowercase, 3-63 chars, no underscores.
        """
        return run_id.lower().replace("_", "-")

    def get_remaining_jobs(self, job_names):
        running_jobs = []
        for job_name in job_names:
            job_status = self.k8s_batch_client.read_namespaced_job_status(name=job_name, namespace=self.namespace)

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
                job_status = self.k8s_batch_client.read_namespaced_job_status(name=job_name, namespace=self.namespace)

                completion_time = job_status.status.completion_time
                succeeded = job_status.status.succeeded or 0
                failed = job_status.status.failed or 0

                # Get the actual pod start time (when it started running, not pending)
                pod_start_time = None
                try:
                    # Get pods associated with this job
                    pods = self.k8s_client.list_namespaced_pod(
                        namespace=self.namespace,
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

    def cleanup_jobs(self, run_id=None):
        """Delete jobs. If run_id is given, only delete jobs with that run-id label."""
        label_selector = "jobgroup=scenario-runs"
        if run_id is not None:
            label_safe = _label_safe_run_id(run_id)
            label_selector = f"jobgroup=scenario-runs,run-id={label_safe}"
        try:
            logger.debug(f"Deleting jobs with label selector '{label_selector}'")
            self.k8s_batch_client.delete_collection_namespaced_job(
                namespace=self.namespace,
                label_selector=label_selector,
                body=client.V1DeleteOptions(grace_period_seconds=0)
            )
            logger.info("Successfully deleted scenario-runs jobs")
        except client.rest.ApiException as e:
            logger.error(f"Error deleting jobs with label selector: {e}")

    def run(self, detached=False):
        # Ensure Kubernetes clients are initialized before running
        self._ensure_k8s_initialized()

        # check if k8s element names have required templates
        manifest_data = self.manifest
        if '$RUN_ID' not in manifest_data['metadata']['name'] or '$TEST_ID' not in manifest_data['metadata']['name']:
            raise ValueError("Manifest element names need to contain '$RUN_ID' and '$TEST_ID' templates")

        # Optionally clean up previous runs before starting
        if self.cleanup_before_run:
            logger.debug("Cleaning up previous scenario-runs jobs and pods...")
            self.cleanup_pods()
            self.cleanup_jobs()

        # upload all config files to S3 bucket
        logger.debug(f"Uploading task config files for {len(self.configs)} scenarios to S3...")
        self.upload_tasks_to_s3()

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
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always")
                    self.k8s_batch_client.create_namespaced_job(namespace=self.namespace, body=job_manifest)
                if caught:
                    logger.error(f"Kubernetes API warnings for job '{job_name}':")
                    for w in caught:
                        logger.error(f"  Warning: {w.message}")
                    raise SystemExit(1)
                logger.debug(f"Created job {job_name} for run {run_number + 1}")

        logger.info(f"All {len(all_jobs)} jobs created. Starting execution...")

        # If detached, exit here without waiting
        if detached:
            logger.info("Running in detached mode. Jobs will continue running in the background.")
            return

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

        # Clean up only this run's jobs and pods
        self.cleanup_pods(run_id=self.run_id)
        self.cleanup_jobs(run_id=self.run_id)
        logger.info("Cleaned up jobs.")

        # Print comprehensive statistics
        self.print_run_statistics()

        logger.debug(f"MinIO S3 pod is left running for reuse")

    def cleanup_pods(self, run_id=None):
        """Delete pods. If run_id is given, only delete pods with that run-id label."""
        label_selector = "jobgroup=scenario-runs"
        if run_id is not None:
            label_safe = _label_safe_run_id(run_id)
            label_selector = f"jobgroup=scenario-runs,run-id={label_safe}"
        try:
            logger.debug(f"Deleting pods with label selector '{label_selector}'")
            self.k8s_client.delete_collection_namespaced_pod(
                namespace=self.namespace,
                label_selector=label_selector,
                body=client.V1DeleteOptions(grace_period_seconds=0)
            )
            logger.debug("Successfully cleaned up scenario-runs pods")
        except client.rest.ApiException as e:
            logger.error(f"Error deleting pods with label selector: {e}")

    def upload_tasks_to_s3(self):
        """Upload all run config files to an S3 bucket (one bucket per run_id)."""

        bucket_name = self._bucket_name_for_run(self.run_id)

        with tempfile.TemporaryDirectory() as temp_dir:
            logger.debug(f"Using temporary directory: {temp_dir}")

            out_dir = os.path.join(temp_dir, "out_template")
            prepare_run_configs(out_dir, self.run_data, cluster=True)

            # Inject instance type command into entrypoint.sh if the cluster config provides one
            entrypoint_path = os.path.join(out_dir, "entrypoint.sh")
            try:
                instance_type_cmd = None
                if hasattr(self.cluster_config, "get_instance_type_command"):
                    instance_type_cmd = self.cluster_config.get_instance_type_command()

                if instance_type_cmd and os.path.exists(entrypoint_path):
                    with open(entrypoint_path, "r", encoding="utf-8") as f:
                        content = f.read()
                    placeholder = 'INSTANCE_TYPE=""'
                    if placeholder in content:
                        content = content.replace(placeholder, instance_type_cmd, 1)
                        with open(entrypoint_path, "w", encoding="utf-8") as f:
                            f.write(content)
            except Exception as exc:  # pragma: no cover - best-effort, non-fatal
                logger.warning(f"Could not inject instance type command into entrypoint.sh: {exc}")

            create_execution_yaml(self.num_runs, out_dir,
                                  execution_params=self.run_data.get("execution", {}))

            logger.info(f"Uploading config files to S3 bucket '{bucket_name}'...")
            upload_configs_to_s3(out_dir, bucket_name, self.cluster_config, self.namespace)

    def get_job_manifest(self, image: str, resources: dict, env: list, run_as_user: int = None,
                         secondary_containers: list = None) -> dict:
        """Generate the base Kubernetes job manifest from templates.

        Args:
            image: Docker image to use
            resources: Resource limits/requests for the main container (cpu, memory)
            env: List of environment variables
            run_as_user: UID to run container as (defaults to 1000 if None)
            secondary_containers: List of secondary container configs (name + resources)

        Returns:
            Dictionary containing the job manifest
        """
        if run_as_user is None:
            run_as_user = 1000

        # Normalize resources: may be a dict or a Pydantic model
        if hasattr(resources, 'cpu'):
            resources = {'cpu': resources.cpu, 'memory': resources.memory}

        # Normalize secondary_containers: may be Pydantic models, normalized dicts, or raw YAML dicts
        self.secondary_containers = normalize_secondary_containers(secondary_containers)

        logger.debug(f"Using run_as_user={run_as_user} for job containers")

        yaml_str = JOB_TEMPLATE.format(image=image, namespace=self.namespace)
        manifest = yaml.safe_load(yaml_str)

        main_container = manifest['spec']['template']['spec']['containers'][0]
        main_container.setdefault('securityContext', {})['runAsUser'] = run_as_user

        if resources.get('cpu'):
            main_container['resources']['requests']['cpu'] = str(resources['cpu'])
            main_container['resources']['limits']['cpu'] = str(resources['cpu'])
        if resources.get('memory'):
            main_container['resources']['requests']['memory'] = resources['memory']
            main_container['resources']['limits']['memory'] = resources['memory']

        # Add custom environment variables
        if env:
            for env_var in env:
                if isinstance(env_var, dict):
                    for key, value in env_var.items():
                        main_container.setdefault('env', []).append({
                            'name': key,
                            'value': str(value)
                        })
        return manifest
