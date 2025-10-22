#!/usr/bin/env python3
import argparse
import copy
import datetime
import fnmatch
import getpass
import os
import shutil
import subprocess
import sys
import tempfile
import time

import yaml
from kubernetes import client, config
from kubernetes.client import CustomObjectsApi
from robovast_common import (convert_dataclasses_to_dict,
                             generate_scenario_variations,
                             load_scenario_config)


class JobRunner:
    def __init__(self, scenario_variation_file, num_runs=1, single_variant=None):
        self.run_id = f"run-{datetime.datetime.now().strftime('%Y-%m-%d-%H%M%S')}"
        self.num_runs = num_runs
        self.single_variant = single_variant
        self.transfer_pvc_name = "transfer-pvc"
        self.transfer_pod_name = "nfs-server"

        if scenario_variation_file is not None and not os.path.exists(scenario_variation_file):
            raise FileNotFoundError(f"Scenario variation file does not exist: {scenario_variation_file}")

        parameters = load_scenario_config(scenario_variation_file, subsection="execution")

        self.scenario_variation_file = scenario_variation_file
        self.manifest_file_path = os.path.join(os.path.dirname(scenario_variation_file), parameters["kubernetes_manifest"])

        # Read filter patterns once
        self.scenario_filter_patterns = parameters.get("scenario_file_filter", [])
        self.variant_filter_patterns = parameters.get("variant_filter_patterns", [])

        if self.scenario_filter_patterns:
            print(f"### Loaded {len(self.scenario_filter_patterns)} filter patterns.")
        if self.variant_filter_patterns:
            print(f"### Loaded {len(self.variant_filter_patterns)} filter patterns.")

        # Discover scenarios and collect filtered files
        self.scenarios = {}
        scenario_file = os.path.join(os.path.dirname(scenario_variation_file), parameters["scenario"])
        if not os.path.exists(scenario_file):
            raise FileNotFoundError(f"Scenario file does not exist: {scenario_file}")

        self.get_filtered_files(scenario_variation_file, scenario_file)

        common_config_file_path = os.path.join(os.path.dirname(os.path.relpath(__file__)), "files")
        self.common_files = []
        if os.path.exists(common_config_file_path):
            for root, _, files in os.walk(common_config_file_path):
                for file in files:
                    file_path = os.path.join(root, file)
                    self.common_files.append(file_path)

        from pprint import pprint
        pprint(self.scenarios)

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

        # Pod monitor configuration
        # Append current username as a suffix to the ntfy topic to make it user-specific
        username = getpass.getuser() or ""
        if username:
            username = f"-{username}"
        self.ntfy_topic = f"scex-k8s-jobs{username}"
        self.pod_monitor_job_name = None
        self.pod_monitor_manifest_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "pod-monitor-manifest.yaml"
        )

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
            # Job has failed - don't include in running_jobs so execution can continue
            # elif job_status.status.failed is not None and job_status.status.failed > 0:
            #     print(f"### Job {job_name} has failed")
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

    def deploy_pod_monitor(self):
        """Deploy the pod monitor pod to track scenario-runs jobs"""
        try:
            # Check if pod_monitor.py exists
            pod_monitor_script_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "pod_monitor.py"
            )
            if not os.path.exists(pod_monitor_script_path):
                print(f"### Warning: pod_monitor.py not found at {pod_monitor_script_path}")
                return None

            # Read the pod_monitor.py script
            with open(pod_monitor_script_path, 'r') as f:
                pod_monitor_script = f.read()

            # Create ConfigMap with the script
            configmap_name = f"pod-monitor-script-{self.run_id}"
            configmap = client.V1ConfigMap(
                metadata=client.V1ObjectMeta(
                    name=configmap_name,
                    namespace="default",
                    labels={
                        "app": "pod-monitor",
                        "run-id": self.run_id
                    }
                ),
                data={
                    "pod_monitor.py": pod_monitor_script
                }
            )

            try:
                self.k8s_client.create_namespaced_config_map(
                    namespace="default",
                    body=configmap
                )
                print(f"### Created ConfigMap: {configmap_name}")
            except client.exceptions.ApiException as e:
                if e.status == 409:
                    print(f"### ConfigMap {configmap_name} already exists")
                else:
                    raise

            # Check if manifest exists
            if not os.path.exists(self.pod_monitor_manifest_path):
                print(f"### Warning: Pod monitor manifest not found at {self.pod_monitor_manifest_path}")
                return None

            # Read and process manifest
            with open(self.pod_monitor_manifest_path, 'r') as f:
                manifests = list(yaml.safe_load_all(f))

            # Apply RBAC resources (ServiceAccount, Role, RoleBinding)
            for manifest in manifests[:-1]:  # All except the Pod
                kind = manifest.get('kind')
                name = manifest['metadata']['name']

                try:
                    if kind == 'ServiceAccount':
                        self.k8s_client.create_namespaced_service_account(
                            namespace="default", body=manifest
                        )
                        print(f"### Created ServiceAccount: {name}")
                    elif kind == 'Role':
                        rbac_api = client.RbacAuthorizationV1Api(self.k8s_api_client)
                        rbac_api.create_namespaced_role(
                            namespace="default", body=manifest
                        )
                        print(f"### Created Role: {name}")
                    elif kind == 'RoleBinding':
                        rbac_api = client.RbacAuthorizationV1Api(self.k8s_api_client)
                        rbac_api.create_namespaced_role_binding(
                            namespace="default", body=manifest
                        )
                        print(f"### Created RoleBinding: {name}")
                except client.exceptions.ApiException as e:
                    if e.status == 409:  # Already exists
                        print(f"### {kind} {name} already exists, skipping")
                    else:
                        raise

            # Deploy the Pod manifest with template replacements
            pod_manifest = manifests[-1]  # Last one is the Pod
            pod_manifest_copy = copy.deepcopy(pod_manifest)

            # Replace templates
            self.replace_template(pod_manifest_copy, "$RUN_ID", self.run_id)
            self.replace_template(pod_manifest_copy, "$NTFY_TOPIC", self.ntfy_topic)

            pod_name = pod_manifest_copy['metadata']['name']

            # Create the pod
            self.k8s_client.create_namespaced_pod(
                namespace="default",
                body=pod_manifest_copy
            )
            print(f"### Deployed pod monitor pod: {pod_name}")
            print(f"### Notifications will be sent to: https://ntfy.sh/{self.ntfy_topic}")

            return pod_name

        except Exception as e:
            print(f"### Error deploying pod monitor: {e}")
            return None

    def cleanup_pod_monitor(self):
        """Clean up the pod monitor pod and ConfigMap"""
        if self.pod_monitor_job_name:
            try:
                # Delete the pod
                self.k8s_client.delete_namespaced_pod(
                    name=self.pod_monitor_job_name,
                    namespace="default",
                    body=client.V1DeleteOptions(grace_period_seconds=0)
                )
                print(f"### Cleaned up pod monitor pod: {self.pod_monitor_job_name}")
            except Exception as e:
                print(f"### Error cleaning up pod monitor pod: {e}")

            # Delete the ConfigMap
            configmap_name = f"pod-monitor-script-{self.run_id}"
            try:
                self.k8s_client.delete_namespaced_config_map(
                    name=configmap_name,
                    namespace="default"
                )
                print(f"### Cleaned up ConfigMap: {configmap_name}")
            except Exception as e:
                print(f"### Error cleaning up ConfigMap: {e}")

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
                scenario_data = self.scenarios[scenario_key]

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

                        job_name = elem_copy['metadata']['name']
                        all_jobs.append(job_name)
                        self.k8s_batch_client.create_namespaced_job(namespace="default", body=elem_copy)
                        print(f"### Created job {job_name} for run {run_number + 1}, scenario {scenario_key}")

        print(f"### All {len(all_jobs)} jobs created. Starting execution...")

        # Deploy pod monitor
        self.pod_monitor_job_name = self.deploy_pod_monitor()

        # Track run start time
        self.run_start_time = datetime.datetime.now(datetime.timezone.utc)

        # Wait for all jobs to complete
        try:
            while True:
                job_status = self.get_remaining_jobs(all_jobs)

                if job_status:
                    print(f"### Waiting for {len(job_status)} out of {len(all_jobs)} jobs to finish...")
                    # self.print_cpu_usage()
                    time.sleep(10)
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
        self.cleanup_pod_monitor()
        print(f"### Cleaned up jobs")

        # Print comprehensive statistics
        self.print_run_statistics()

        print(f"### Transfer PVC and pod are left running for reuse")

    def cleanup_gridfs_db(self):
        """Not needed for PVC approach"""

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

    def print_cpu_usage(self):
        metrics_api = CustomObjectsApi()
        try:
            pod_metrics = metrics_api.list_namespaced_custom_object(
                group="metrics.k8s.io",
                version="v1beta1",
                namespace="default",
                plural="pods"
            )
            for item in pod_metrics.get("items", []):
                pod_name = item["metadata"]["name"]
                if pod_name.startswith("mongo"):
                    continue
                total_cpu_usage_milli = 0
                total_memory_usage_mb = 0

                for container in item["containers"]:
                    cpu = container["usage"]["cpu"]
                    if cpu.endswith('n'):
                        cpu_milli = int(cpu[:-1]) / 1_000_000
                    elif cpu.endswith('m'):
                        cpu_milli = float(cpu[:-1])
                    else:
                        cpu_milli = float(cpu) * 1000

                    mem = container["usage"]["memory"]
                    mem_mb = 0
                    if mem.endswith('Ki'):
                        mem_mb = float(mem[:-2]) * 1024 / 1_000_000  # KiB to bytes, then to MB
                    elif mem.endswith('Mi'):
                        mem_mb = float(mem[:-2])

                    total_memory_usage_mb += mem_mb
                    total_cpu_usage_milli += cpu_milli
                print(f"  Pod: {pod_name}, CPU: {total_cpu_usage_milli:.0f}m, MEM {total_memory_usage_mb:.0f}MB")
        except Exception as e:
            print(f"Error fetching metrics: {e}")

    def read_filter_file(self, filter_file_path):
        """Read and parse a .gitignore-like filter file"""
        patterns = []
        if not os.path.exists(filter_file_path):
            return patterns

        with open(filter_file_path, 'r') as f:
            for line in f:
                line = line.strip()
                # Skip empty lines and comments
                if line and not line.startswith('#'):
                    patterns.append(line)
        return patterns

    def matches_patterns(self, file_path, patterns, base_dir):
        """Check if a file matches any of the gitignore-like patterns with support for ** recursive matching"""
        if not patterns:
            return False

        # Get relative path from base directory
        try:
            rel_path = os.path.relpath(file_path, base_dir)
        except ValueError:
            # Path is not relative to base_dir
            return False

        # Normalize path separators for consistent matching
        rel_path = rel_path.replace(os.sep, '/')

        for pattern in patterns:
            if self._match_pattern(rel_path, pattern):
                return True

        return False

    def _match_pattern(self, rel_path, pattern):
        """Match a single pattern against a relative path, supporting ** for recursive matching"""
        # Normalize pattern separators
        pattern = pattern.replace(os.sep, '/')

        # Handle directory patterns (ending with /)
        if pattern.endswith('/'):
            pattern = pattern[:-1]
            # Check if any parent directory matches
            parts = rel_path.split('/')
            for i in range(len(parts)):
                parent_path = '/'.join(parts[:i+1])
                if self._glob_match(parent_path, pattern):
                    return True
            return self._glob_match(os.path.dirname(rel_path), pattern)
        else:
            # Handle file patterns
            if self._glob_match(rel_path, pattern):
                return True
            # Also check just the filename
            if self._glob_match(os.path.basename(rel_path), pattern):
                return True
            return False

    def _glob_match(self, path, pattern):
        """Enhanced glob matching with support for ** recursive patterns"""
        # Handle ** patterns
        if '**' in pattern:
            return self._match_recursive_pattern(path, pattern)
        else:
            # Use standard fnmatch for simple patterns
            return fnmatch.fnmatch(path, pattern)

    def _match_recursive_pattern(self, path, pattern):
        """Match patterns containing ** for recursive directory matching"""
        import re

        # Split pattern by ** to handle each part
        pattern_parts = pattern.split('**')

        if len(pattern_parts) == 1:
            # No ** in pattern, use standard matching
            return fnmatch.fnmatch(path, pattern)

        # Convert glob pattern to regex, handling ** specially
        regex_pattern = ''
        for i, part in enumerate(pattern_parts):
            if i > 0:
                # Add regex for ** (match zero or more path segments)
                regex_pattern += '(?:[^/]+/)*'

            # Convert glob to regex for this part
            if part:
                # Remove leading/trailing slashes to avoid double slashes
                part = part.strip('/')
                if part:
                    # Convert fnmatch pattern to regex
                    part_regex = fnmatch.translate(part).replace('\\Z', '')
                    # Remove the (?ms: prefix and ) suffix that fnmatch.translate adds
                    if part_regex.startswith('(?ms:'):
                        part_regex = part_regex[5:-1]
                    regex_pattern += part_regex
                    if i < len(pattern_parts) - 1:
                        regex_pattern += '/'

        # Ensure the pattern matches the entire string
        regex_pattern = '^' + regex_pattern + '$'

        try:
            return bool(re.match(regex_pattern, path))
        except re.error:
            # Fallback to simple fnmatch if regex fails
            return fnmatch.fnmatch(path, pattern)

    def get_filtered_files(self, variation_file, scenario_file):

        output_dir = tempfile.TemporaryDirectory(prefix="robovast_execution_", delete=False)
        variants = generate_scenario_variations(variation_file, print, variation_classes=None, output_dir=output_dir.name)

        if not variants:
            print("### Warning: No variants found.")
            return []

        # scenario_path = os.path.join(root, scenario_file)

        # scenario_key = os.path.splitext(os.path.relpath(scenario_path, self.scenario_variation_file))[0].replace(os.sep, '_')

        # Add files located next to the scenario file that match the filter patterns
        scenario_files = self.collect_filtered_files(self.scenario_filter_patterns, os.path.dirname(variation_file))

        # If we have variants data, create separate scenario entries for each variant
        for variant in variants:
            if variant is None:
                continue
            if 'name' not in variant and 'variant' not in variant:
                continue
            # Extract the name from the variant data for unique identification
            variant_name = variant["name"]
            variant_files = []
            variant_file_path = ''
            if "floorplan_variant_path" in variant:
                variant_file_path = variant["floorplan_variant_path"]
                variant_files = self.collect_filtered_files(self.variant_filter_patterns, variant_file_path)

            variant_data = variant.get('variant')

            self.scenarios[variant_name] = {
                'scenario_files': scenario_files,
                'variant_files': variant_files,
                'original_scenario_path': scenario_file,
                'variant_file_path': variant_file_path,
                'variant_data': {
                    'nav_scenario': convert_dataclasses_to_dict(variant_data)
                }
            }
            print(f"### Created scenario entry for variant {variant_name}")
        # else:
        #     # No variants file or empty/invalid variants, store as single scenario
        #     self.scenarios[scenario_key] = {
        #         'scenario_files': scenario_files,
        #         'variant_files': [],
        #         'original_scenario_path': scenario_path,
        #         'variant_file_path': '',
        #         'variant_data': None
        #     }
        #     print("### Found scenario file:", scenario_path)

        # # Filter scenarios if single_variant is specified
        # if self.single_variant:
        #     if self.single_variant in self.scenarios:
        #         self.scenarios = {self.single_variant: self.scenarios[self.single_variant]}
        #         print(f"### Filtered to single variant: {self.single_variant}")
        #     else:
        #         print(f"### ERROR: Variant '{self.single_variant}' not found!")
        #         print(f"### Available variants: {list(self.scenarios.keys())}")
        #         sys.exit(1)

    def collect_filtered_files(self, filter_pattern, rel_path):
        """Collect files from scenario directory that match the filter patterns"""
        filtered_files = []
        print("### Collecting filtered files from:", rel_path)
        if not filter_pattern:
            return filtered_files
        for root, dirs, files in os.walk(rel_path):
            for file in files:
                file_path = os.path.join(root, file)
                if self.matches_patterns(file_path, filter_pattern, rel_path):
                    key = os.path.relpath(file_path, rel_path)
                    filtered_files.append(key)

        return filtered_files

    def upload_tasks_to_transfer_pod(self):
        """Upload all files to transfer PVC using kubectl cp to transfer pod"""

        # Create a temporary directory to organize all files
        with tempfile.TemporaryDirectory() as temp_dir:
            print(f"### Using temporary directory: {temp_dir}")

            # Create the config directory structure: /config/$RUN_ID/
            config_dir = os.path.join(temp_dir, "config", self.run_id)
            os.makedirs(config_dir, exist_ok=True)

            # Organize files by scenario
            for scenario_key, scenario_data in self.scenarios.items():
                scenario_dir = os.path.join(config_dir, scenario_key)
                os.makedirs(scenario_dir, exist_ok=True)

                # Copy scenario file
                original_scenario_path = scenario_data.get('original_scenario_path')
                shutil.copy2(original_scenario_path, os.path.join(scenario_dir, 'scenario.osc'))

                # Copy filtered files
                for config_file in scenario_data["scenario_files"]:
                    src_path = os.path.join(os.path.dirname(original_scenario_path), config_file)
                    dst_path = os.path.join(scenario_dir, config_file)
                    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
                    shutil.copy2(src_path, dst_path)

                # Copy variant files
                for config_file in scenario_data["variant_files"]:
                    src_path = os.path.join(os.path.dirname(original_scenario_path),
                                            scenario_data["variant_file_path"], config_file)
                    dst_path = os.path.join(scenario_dir, config_file)
                    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
                    shutil.copy2(src_path, dst_path)

                # Copy common files
                for common_file in self.common_files:
                    short_filename = os.path.basename(common_file)
                    shutil.copy2(common_file, os.path.join(scenario_dir, short_filename))

                # Create variant file if needed
                variant_data = scenario_data.get('variant_data')
                if variant_data is not None:
                    with open(os.path.join(scenario_dir, 'scenario.variant'), 'w') as f:
                        yaml.dump(variant_data, f)

            # Use kubectl cp to copy the entire config directory to the transfer pod
            try:
                print(f"### Copying config files to transfer pod using kubectl cp...")

                # Copy the config directory to the transfer pod at /exports/config/
                cmd = [
                    "kubectl", "cp",
                    os.path.join(temp_dir, "config"),
                    f"default/{self.transfer_pod_name}:/exports/"
                ]

                result = subprocess.run(cmd, capture_output=True, text=True, check=True)
                print(f"### Successfully copied config files to transfer pod")

                # Verify the copy was successful by listing the directory
                verify_cmd = [
                    "kubectl", "exec", "-n", "default", self.transfer_pod_name,
                    "--",
                    "ls", "-la", f"/exports/config/{self.run_id}"
                ]

                verify_result = subprocess.run(verify_cmd, capture_output=True, text=True)
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


def main():
    default_scenario_variation_file = os.path.join(os.getcwd(), "Dataset", "scenario.variation")
    parser = argparse.ArgumentParser(description='Run Kubernetes jobs with different variants.')
    parser.add_argument('--scenario-variation-file', type=str, default=default_scenario_variation_file,
                        help='Scenario Variation File containing the parameters for execution')
    parser.add_argument('--variant', type=str, default=None, help='Run only a specific variant by name')
    parser.add_argument('--runs', type=int, default=1,
                        help='Number of runs to execute (default: 1). All jobs for all runs will be created before execution starts.')
    args = parser.parse_args()

    try:
        job_runner = JobRunner(args.scenario_variation_file, args.runs, args.variant)
        job_runner.run()
    except Exception as e:
        print(f"Error: {e}")


if __name__ == '__main__':
    main()
