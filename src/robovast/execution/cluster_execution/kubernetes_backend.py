#!/usr/bin/env python3
# Copyright (C) 2026 Frederik Pasch
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
"""Kubernetes execution backend for the in-cluster campaign controller.

:class:`KubernetesBackend` is the cluster counterpart of
:class:`~robovast.execution.backends.DockerBackend`: it runs **one batch** of a
campaign as Kubernetes Jobs and leaves results at
``<campaign_root>/<config>/<run>/`` so the controller's scoring and store are
backend-agnostic. It is meant to run **inside the controller pod**
(``vast exec cluster run``), where the storage backend is reachable directly and
with full bandwidth.

The job-manifest toolkit lives here in :class:`BatchJobRunner` (built only via
:meth:`BatchJobRunner.for_batch`): it composes per-job Kubernetes manifests,
submits/polls/cleans up the Jobs, and writes the per-run job-link manifest. The
host-side ``vast exec cluster prepare-run`` reuses the same builder to emit the
exact manifests the controller would submit, for debugging.

Per batch it:

1. prepares the batch's config tree (reusing :func:`prepare_campaign_configs`
   and the :class:`BatchJobRunner` manifest building),
2. uploads it to the campaign's storage prefix (in-pod, via
   :mod:`.in_pod_storage` — no ``kubectl``/archiver),
3. creates one Kubernetes Job per packed job and waits for completion, then
4. downloads that batch's per-config/run results back into ``campaign_root``.

Each batch is isolated under a ``_batches/<batch_tag>/`` storage sub-prefix and
uses batch-namespaced job names, so batches of one search campaign never
collide. A ``_batch_tag`` of ``None`` selects the classic single-batch layout
(used by ``prepare-run``).
"""

import copy
import hashlib
import logging
import os
import re
import tempfile
import time

import yaml
from kubernetes import client

from robovast.common import (COMPAT_VERSION, get_execution_env_variables,
                             normalize_secondary_containers)
from robovast.common.cluster_context import resolve_resources
from robovast.common.common import get_scenario_parameters
from robovast.common.execution import (build_job_parameter_documents,
                                       dump_multi_document_yaml,
                                       resolve_robovast_image,
                                       write_job_links_manifest)
from robovast.common import prepare_campaign_configs
from robovast.execution.backends import ExecutionBackend, RunOptions
from robovast.execution.packer import build_jobs

from . import in_pod_storage
from .cluster_execution import _label_safe_campaign
from .manifests import JOB_TEMPLATE

logger = logging.getLogger(__name__)


def _short_job_name(campaign: str, config_name: str, run_number: int) -> str:
    """Create a short Kubernetes job name (max 63 chars) for campaign-id-config-run.

    Format: <name6>-<HHMMSScc>-<config6chars><sha256_16chars>-<run_number>
    - campaign: "<name>-2026-02-27-14113025"
        -> name prefix: first 6 lowercase alphanumeric chars of <name>
        -> time suffix: last 8 chars of timestamp (HHMMSScc) = "14113025"
      e.g. "dynamic_obstacle-2026-02-27-14113025" -> "dynami-14113025"
           "campaign-2026-02-27-14113025"         -> "campai-14113025"
    - config_name: first 8 alphanumeric for readability, rest as 4-char hash for uniqueness
    - run_number: as-is (e.g. 0, 1, ...)
    Labels keep full campaign-id for identifying.
    """
    # Extract "<name>" from "<name>-YYYY-MM-DD-HHMMSScc" (6-8 digit tail)
    ts_match = re.search(r'\d{4}-\d{2}-\d{2}-(\d{6,8})$', campaign)
    hhmmss = ts_match.group(1) if ts_match else campaign[-8:]
    # Strip the timestamp suffix to get the name prefix
    raw_name = re.sub(r'-\d{4}-\d{2}-\d{2}-\d{6,8}$', '', campaign) if ts_match else campaign
    name_alpha = re.sub(r'[^a-z0-9]', '', raw_name.lower())[:6]
    # Kubernetes names must start with a letter; fall back to 'r' if name is empty or starts with digit
    if not name_alpha or name_alpha[0].isdigit():
        name_alpha = 'r' + name_alpha[:5]
    run_part = f"{name_alpha}-{hhmmss}"

    # First 6 alphanumeric chars for readability + 16-char SHA-256 for collision-free uniqueness
    config_alpha = re.sub(r"[^a-zA-Z0-9]", "", config_name)[:6].lower()
    config_hash = hashlib.sha256(config_name.encode()).hexdigest()[:16]
    config_part = f"{config_alpha}{config_hash}" if config_alpha else config_hash

    return f"{run_part}-{config_part}-{run_number}"


class BatchJobRunner:
    """Build, submit and clean up the Kubernetes Jobs for **one** batch.

    Constructed only via :meth:`for_batch` from a pre-built ``campaign_data``
    (the controller has already composed it). Runs in-pod: storage I/O is direct
    (no archiver) and the Kubernetes client uses the in-cluster service account.
    The same builder is reused offline by ``vast exec cluster prepare-run`` to emit
    job manifests without touching the API (only :meth:`run_batch_in_pod` does).
    """

    @classmethod
    def for_batch(cls, *, campaign_data, campaign_id, batch_tag, runs, cluster_config,
                  namespace, image, kube_context=None, log_tree=False):
        self = cls()
        self.cluster_config = cluster_config
        self.namespace = namespace
        # Used only for resolve_resources() (per-cluster resource lists); the
        # Kubernetes API client uses in-cluster config (see _ensure_k8s_initialized).
        self.kube_context = kube_context
        self.log_tree = log_tree

        self.campaign = campaign_id
        self.campaign_data = campaign_data
        self.configs = campaign_data.get("configs", [])
        self.num_runs = runs
        # ``None`` ⇒ classic single-batch layout (prepare-run); the controller sets
        # a tag per search batch so jobs/param files/storage prefix don't collide.
        self._batch_tag = batch_tag

        execution_params = campaign_data.get("execution", {}) or {}
        self.pre_command = execution_params.get("pre_command")
        self.post_command = execution_params.get("post_command")
        self.run_as_user = execution_params.get("run_as_user", 1000)

        # Builds self.manifest and sets self.env / self.secondary_containers.
        self.manifest = self.get_job_manifest(
            image,
            execution_params.get("resources") or {},
            execution_params.get("env", []),
            self.run_as_user,
            execution_params.get("secondary_containers") or [],
        )
        timeout = execution_params.get("timeout")
        if timeout:
            self.manifest["spec"]["activeDeadlineSeconds"] = int(timeout)

        self.k8s_client = None
        self.k8s_batch_client = None
        self.k8s_api_client = None
        self._k8s_initialized = False
        return self

    def _ensure_k8s_initialized(self):
        """Initialise Kubernetes clients from the in-cluster service account."""
        if self._k8s_initialized:
            return
        from kubernetes import config as kube_config  # pylint: disable=import-outside-toplevel
        try:
            kube_config.load_incluster_config()
        except kube_config.ConfigException:
            # Fallback for host-side dry-runs / tests.
            kube_config.load_kube_config(context=self.kube_context)
        self.k8s_client = client.CoreV1Api()
        self.k8s_batch_client = client.BatchV1Api()
        self.k8s_api_client = client.ApiClient()
        self._k8s_initialized = True

    # -- manifest toolkit ---------------------------------------------------

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

    def _s3_settings(self):
        """Return (endpoint, access_key, secret_key, bucket_name, campaign_prefix).

        ``campaign_prefix`` is ``"<campaign>/"`` for shared-bucket backends
        (e.g. GCS) and ``""`` for per-campaign buckets (embedded MinIO). It is the
        flat campaign prefix — batches share it (no ``_batches/`` component); cross-
        batch collisions are prevented by the batch-namespaced ``_job_tag`` (job
        names, ``<tag>.params.yaml``, ``_jobs/<tag>``), so the layout matches local.
        """
        s3_endpoint = self.cluster_config.get_s3_endpoint()
        s3_access_key, s3_secret_key = self.cluster_config.get_s3_credentials()
        bucket_name, campaign_prefix = in_pod_storage.campaign_storage_location(
            self.cluster_config, self.campaign)
        return s3_endpoint, s3_access_key, s3_secret_key, bucket_name, campaign_prefix

    def _job_tag(self, index: int) -> str:
        """Flat, slash-free job tag for job *index*, namespaced by the batch when set.

        Used for the (globally unique, K8s-safe) job name and the
        ``<tag>.params.yaml`` file, so these never collide across batches.
        """
        return f"{self._batch_tag}-job-{index}" if self._batch_tag else f"job-{index}"

    def _job_artifact_path(self, index: int) -> str:
        """Path of the job's artifact dir under ``_jobs/`` (no leading ``_jobs/``).

        Nested ``<batch>/job-<idx>`` when batched (matching the local layout), else
        flat ``job-<idx>``. This is the symlink target base used by ``job_links``.
        """
        return f"{self._batch_tag}/job-{index}" if self._batch_tag else f"job-{index}"

    def _build_job_manifest(self, *, job_short_name, job_full_name, item_tag,
                            total_jobs, s3_prefix, init_cmd, extra_main_env=()):
        """Assemble a job manifest shared by single-config and packed jobs.

        The two paths differ only in job naming, the S3 output prefix, the
        initContainer mirror command, and a few extra env vars
        (``extra_main_env``); everything else (volumes, the init container, the
        main container env, secondary containers) is identical and lives here.
        """
        job_manifest = copy.deepcopy(self.manifest)

        label_safe_campaign = _label_safe_campaign(self.campaign)
        self.replace_template(job_manifest, "$CAMPAIGN_ID", label_safe_campaign)
        self.replace_template(job_manifest, "$JOB_NAME", job_short_name)
        self.replace_template(job_manifest, "$JOB_FULL_NAME", job_full_name)
        self.replace_template(job_manifest, "$ITEM", item_tag)
        self.replace_template(job_manifest, "$TOTAL_JOB_NUM", str(total_jobs))

        s3_endpoint, s3_access_key, s3_secret_key, bucket_name, campaign_prefix = self._s3_settings()

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

        init_env = [
            {'name': 'S3_ENDPOINT', 'value': s3_endpoint},
            {'name': 'S3_BUCKET', 'value': bucket_name},
            {'name': 'S3_ACCESS_KEY', 'value': s3_access_key},
            {'name': 'S3_SECRET_KEY', 'value': s3_secret_key},
            {'name': 'S3_CAMPAIGN_PREFIX', 'value': campaign_prefix},
        ]
        spec['initContainers'] = [
            {
                'name': 's3-init',
                'image': 'ghcr.io/cps-test-lab/robovast-sidecar:latest',
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
        scenario_file_name = os.path.basename(
            self.campaign_data.get('scenario_file', 'scenario.osc')
        )
        if containers:
            if 'env' not in containers[0]:
                containers[0]['env'] = []

            env_vars = get_execution_env_variables(0, item_tag)
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
                containers[0]['env'].append({'name': 'PRE_COMMAND', 'value': str(self.pre_command)})
            if self.post_command:
                containers[0]['env'].append({'name': 'POST_COMMAND', 'value': str(self.post_command)})
            if self.log_tree:
                containers[0]['env'].append({'name': 'SCENARIO_EXECUTION_PARAMETERS', 'value': '-t'})

            containers[0]['env'].append({'name': 'SCENARIO_FILE', 'value': scenario_file_name})

            for k, v in extra_main_env:
                containers[0]['env'].append({'name': k, 'value': v})

            containers[0]['volumeMounts'] = shared_volume_mounts

        # Add secondary containers (they receive the same packed env so a
        # sim/SUT server resolves file-valued reset parameters identically).
        for sc in self.secondary_containers:
            sc_name = sc['name']
            sc_resources = resolve_resources(sc['resources'], self.kube_context)
            secondary_env = [
                {'name': 'CONTAINER_NAME', 'value': sc_name},
                {'name': 'SCENARIO_FILE', 'value': scenario_file_name},
            ]
            for k, v in extra_main_env:
                secondary_env.append({'name': k, 'value': v})
            for env_var in self.env:
                if isinstance(env_var, dict):
                    for key, value in env_var.items():
                        secondary_env.append({'name': key, 'value': str(value)})
            secondary_spec = {
                'name': sc_name,
                'image': job_manifest['spec']['template']['spec']['containers'][0]['image'],
                'command': ['/usr/bin/tini', '--', '/bin/bash', '/config/secondary_entrypoint.sh'],
                'env': secondary_env,
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

    def create_job_manifest(self, job, total_jobs: int) -> dict:
        """Create a manifest for one job (1..K configs).

        One K8s Job runs all the job's configs via a multi-document param file
        (the simulator is reset between them). ``/out`` is this pod's emptyDir shaped
        as the campaign root, uploaded to the campaign prefix, so per-config
        results land at ``<campaign>/<config>/<run>/`` via each document's
        ``_output_dir``. Job-level artifacts go to a per-job subdir, and each
        config's files are mirrored under ``/config/<config-name>/`` to avoid
        collisions. The job's multi-document param file ships in ``_transient/``
        and so lands at ``/config/<job-tag>.params.yaml``.
        """
        _, _, _, _, campaign_prefix = self._s3_settings()
        job_tag = self._job_tag(job.index)
        per_config_mirror = "".join(
            f"(mc mirror mystore/$S3_BUCKET/${{S3_CAMPAIGN_PREFIX}}{cn}/_config/ /config/{cn}/ 2>/dev/null || true); "
            for cn in job.config_names
        )
        init_cmd = (
            f"mc alias set mystore \"$S3_ENDPOINT\" \"$S3_ACCESS_KEY\" \"$S3_SECRET_KEY\" && "
            f"mc mirror mystore/$S3_BUCKET/${{S3_CAMPAIGN_PREFIX}}_config/ /config/ && "
            f"mc mirror mystore/$S3_BUCKET/${{S3_CAMPAIGN_PREFIX}}_transient/ /config/ && "
            f"{per_config_mirror}"
            f"for s3pfx in ${{S3_CAMPAIGN_PREFIX}}_config ${{S3_CAMPAIGN_PREFIX}}_transient; do "
            f"mc find mystore/$S3_BUCKET/$s3pfx/ 2>/dev/null | while IFS= read -r obj; do "
            f"mc stat --json \"$obj\" 2>/dev/null | grep -qi 'executable.*yes' && "
            f"chmod +x \"/config/${{obj#mystore/$S3_BUCKET/$s3pfx/}}\" 2>/dev/null || true; "
            f"done; done; true"
        )
        extra_env = (
            ('SCENARIO_PARAMETER_FILE', f"/config/{job_tag}.params.yaml"),
            ('OUTPUT_RESULT_PER_SCENARIO', 'true'),
            # Job artifacts land in the nested _jobs/<batch>/job-<idx> layout
            # (matching local), while the K8s job name / param file stay flat
            # (slash-free, globally unique). See _job_artifact_path / _job_tag.
            ('OUTPUT_DIR', f"/out/_jobs/{self._job_artifact_path(job.index)}"),
            ('SCENARIO_OUTPUT_DIR', '/out'),
        )
        return self._build_job_manifest(
            job_short_name=_short_job_name(self.campaign, job_tag, job.index),
            job_full_name=f"{self.campaign}-{job_tag}",
            item_tag=job_tag,
            total_jobs=total_jobs,
            s3_prefix=campaign_prefix.rstrip("/"),
            init_cmd=init_cmd,
            extra_main_env=extra_env,
        )

    def _runs_per_job(self) -> int:
        """How many runs (config × run-number work items) to pack into one job."""
        return int((self.campaign_data.get("execution") or {}).get("runs_per_job") or 1)

    def _build_jobs(self):
        """Group (config, run) work items into jobs per runs_per_job.

        Deterministic, so the jobs used to write per-job param files match the
        jobs used to create job manifests.
        """
        return build_jobs(self.configs, self.num_runs, self.campaign_data.get("execution") or {})

    def _write_job_param_files(self, out_dir):
        """Write one multi-document scenario-parameter file per packed job into
        ``out_dir/_transient/`` so they upload with the campaign and are mirrored
        into each packed job's ``/config`` as ``job-<idx>.params.yaml``."""
        vast_dir = os.path.dirname(self.campaign_data["vast"])
        scenario_path = os.path.join(vast_dir, self.campaign_data["scenario_file"])
        scenario_name = next(iter(get_scenario_parameters(scenario_path).keys()))
        transient_dir = os.path.join(out_dir, "_transient")
        os.makedirs(transient_dir, exist_ok=True)
        jobs = self._build_jobs()
        for job in jobs:
            docs = build_job_parameter_documents(job, scenario_name)
            with open(os.path.join(transient_dir, f"{self._job_tag(job.index)}.params.yaml"), "w") as f:
                f.write(dump_multi_document_yaml(docs))
        # Canonical link manifest, consumed by the controller's upload-to-share
        # compression to materialise <config>/<run>/job symlinks into the tar.gz.
        # Skipped in per-batch mode: build_job_links assumes the single-batch
        # ``_jobs/job-<idx>`` layout, which the batch-namespaced job tag breaks.
        if not self._batch_tag:
            write_job_links_manifest(transient_dir, jobs)

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

    def cleanup_jobs(self, campaign=None):
        """Delete jobs. If campaign is given, only delete jobs with that campaign-id label."""
        label_selector = "jobgroup=scenario-runs"
        if campaign is not None:
            label_safe = _label_safe_campaign(campaign)
            label_selector = f"jobgroup=scenario-runs,campaign-id={label_safe}"
        try:
            logger.debug(f"Deleting jobs with label selector '{label_selector}'")
            self.k8s_batch_client.delete_collection_namespaced_job(
                namespace=self.namespace,
                label_selector=label_selector,
                body=client.V1DeleteOptions(
                    grace_period_seconds=0, propagation_policy="Background"
                ),
            )
            logger.info("Successfully deleted scenario-runs jobs")
        except client.rest.ApiException as e:
            logger.error(f"Error deleting jobs with label selector: {e}")

    def cleanup_pods(self, campaign=None):
        """Delete pods. If campaign is given, only delete pods with that campaign-id label."""
        label_selector = "jobgroup=scenario-runs"
        if campaign is not None:
            label_safe = _label_safe_campaign(campaign)
            label_selector = f"jobgroup=scenario-runs,campaign-id={label_safe}"
        try:
            logger.debug(f"Deleting pods with label selector '{label_selector}'")
            self.k8s_client.delete_collection_namespaced_pod(
                namespace=self.namespace,
                label_selector=label_selector,
                body=client.V1DeleteOptions(
                    grace_period_seconds=0, propagation_policy="Background"
                ),
            )
            logger.debug("Successfully cleaned up scenario-runs pods")
        except client.rest.ApiException as e:
            logger.error(f"Error deleting pods with label selector: {e}")

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

        # Resolve per-cluster resource values for the active Kubernetes context
        resources = resolve_resources(resources, self.kube_context)

        # Normalize secondary_containers: may be Pydantic models, normalized dicts, or raw YAML dicts
        self.secondary_containers = normalize_secondary_containers(secondary_containers)
        self.env = env or []

        logger.debug(f"Using run_as_user={run_as_user} for job containers")

        yaml_str = JOB_TEMPLATE.format(image=image, namespace=self.namespace,
                                       compat_version=COMPAT_VERSION)
        manifest = yaml.safe_load(yaml_str)

        manifest.setdefault("metadata", {}).setdefault("annotations", {})[
            "kueue.x-k8s.io/queue-name"
        ] = "robovast"

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

    # -- in-pod execution ---------------------------------------------------

    def run_batch_in_pod(self, campaign_root: str):
        """Upload, run and download one batch; results land under *campaign_root*."""
        self._ensure_k8s_initialized()
        _, _, _, bucket_name, campaign_prefix = self._s3_settings()
        storage = in_pod_storage.storage_client_for(self.cluster_config)

        # 1. Prepare this batch's config tree + per-job parameter files.
        with tempfile.TemporaryDirectory(prefix="robovast_batch_") as out_dir:
            prepare_campaign_configs(out_dir, self.campaign_data, cluster=True)
            self._write_job_param_files(out_dir)

            # 2. Upload to the batch's storage prefix (job init containers mirror from here).
            n = storage.upload_dir(out_dir, bucket_name, campaign_prefix)
            logger.info("Batch %s: uploaded %d config file(s) to %s/%s",
                        self._batch_tag, n, bucket_name, campaign_prefix)

        # 3. Build and submit one Job per packed job, then wait.
        jobs = self._build_jobs()
        total_jobs = len(jobs)
        job_names = []
        for job in jobs:
            manifest = self.create_job_manifest(job, total_jobs)
            name = manifest["metadata"]["name"]
            job_names.append(name)
            try:
                self.k8s_batch_client.create_namespaced_job(namespace=self.namespace, body=manifest)
            except client.exceptions.ApiException as exc:
                if exc.status == 409:
                    logger.debug("Batch %s: job %s already exists.", self._batch_tag, name)
                else:
                    raise
        logger.info("Batch %s: created %d job(s); waiting for completion...",
                    self._batch_tag, len(job_names))

        while True:
            remaining = self.get_remaining_jobs(job_names)
            if not remaining:
                break
            logger.info("Batch %s: %d/%d job(s) still running...",
                        self._batch_tag, len(remaining), len(job_names))
            time.sleep(2)
        logger.info("Batch %s: all jobs finished.", self._batch_tag)

        # 4. Download this batch's results into the campaign root for scoring.
        #    The campaign prefix is flat/shared across batches, so we identify this
        #    batch's results by its config names (self.configs == this batch's
        #    composed configs) and fetch only those <config>/ dirs — the same
        #    config names the controller scores at campaign_root/<config>/.
        os.makedirs(campaign_root, exist_ok=True)
        got = 0
        for config_data in self.configs:
            cn = config_data.get("name")
            if not cn:
                continue
            got += storage.download_prefix(
                bucket_name, f"{campaign_prefix}{cn}", os.path.join(campaign_root, cn))
        logger.info("Batch %s: downloaded %d result file(s) into %s",
                    self._batch_tag, got, campaign_root)

        # 4b. Record this batch's <config>/<run>/job -> _jobs/<batch>/job-<idx>
        #     links so upload-to-share can materialise the per-run `job` symlinks.
        self._write_job_links(campaign_root)

        # 5. Clean up this batch's jobs/pods (sequential batches share the
        #    campaign-id label, so only this batch's resources are present).
        self.cleanup_jobs(campaign=self.campaign)
        self.cleanup_pods(campaign=self.campaign)

    def _write_job_links(self, campaign_root: str):
        """Merge this batch's job-link entries into ``_transient/job_links.yaml``.

        ``<config>/<run>/job`` -> ``../../_jobs/<batch>/job-<idx>``. Accumulated
        across batches (the manifest is shared), uploaded by ``finalize_campaign``,
        and turned into real symlinks by the controller's upload-to-share
        compression.
        """
        from robovast.common.execution import \
            JOB_LINKS_MANIFEST  # pylint: disable=import-outside-toplevel

        transient = os.path.join(campaign_root, "_transient")
        os.makedirs(transient, exist_ok=True)
        manifest = os.path.join(transient, JOB_LINKS_MANIFEST)
        links = {}
        if os.path.isfile(manifest):
            with open(manifest, encoding="utf-8") as f:
                links = yaml.safe_load(f) or {}
        for job in self._build_jobs():
            target = f"../../_jobs/{self._job_artifact_path(job.index)}"
            for item in job.items:
                links[f"{item.config_name}/{item.run_number}/job"] = target
        with open(manifest, "w", encoding="utf-8") as f:
            yaml.safe_dump(links, f, default_flow_style=False, sort_keys=True)


class KubernetesBackend(ExecutionBackend):
    """Run batches as Kubernetes Jobs from inside the controller pod.

    Args:
        cluster_config: Reconstructed cluster config (storage + scheduling).
        namespace: Kubernetes namespace for the jobs.
        kube_context: Host context name, used only to resolve per-cluster resource
            lists; the API client itself uses in-cluster config.
        log_tree: Forward ``-t`` (live scenario tree) to the jobs.
    """

    def __init__(self, *, cluster_config, namespace="default", kube_context=None,
                 log_tree=False):
        self.cluster_config = cluster_config
        self.namespace = namespace
        self.kube_context = kube_context
        self.log_tree = log_tree
        # Captured from run_batch for finalize_campaign (execution.yaml metadata).
        self._execution_params: dict = {}
        self._runs = None
        # Lazily-built read-only storage client for count_run_artifacts (the
        # controller's progress poller); separate from the write path.
        self._progress_storage = None

    def run_batch(self, campaign_data: dict, *, campaign_root: str, batch_tag: str,
                  runs: int, options: RunOptions) -> None:
        campaign_id = os.path.basename(os.path.normpath(campaign_root))
        self._execution_params = campaign_data.get("execution", {}) or {}
        self._runs = runs
        image = resolve_robovast_image(
            explicit=options.image,
            config_image=self._execution_params.get("image"),
        )
        runner = BatchJobRunner.for_batch(
            campaign_data=campaign_data,
            campaign_id=campaign_id,
            batch_tag=batch_tag,
            runs=runs,
            cluster_config=self.cluster_config,
            namespace=self.namespace,
            image=image,
            kube_context=self.kube_context,
            log_tree=self.log_tree or options.log_tree,
        )
        runner.run_batch_in_pod(campaign_root)

    def finalize_campaign(self, campaign_root: str) -> None:
        """Publish the canonical campaign to storage so the bucket matches local.

        Jobs upload raw per-run results (``<config>/<run>/test.xml`` etc.) and
        ``_jobs/`` *before* the controller runs search postprocessing, and each
        batch uploads ``_config``/``_transient``. This step publishes the full
        in-pod ``campaign_root`` — which additionally holds ``campaign.db``,
        ``_execution/`` and the postprocessing-derived per-run artifacts (e.g.
        ``metrics.csv``, written next to ``trajectory.csv`` by ``QuadMetrics``) —
        so ``upload-to-share`` + ``download`` yield a layout identical to a local
        run. Re-uploading the small raw files is idempotent.
        """
        from robovast.common.execution import \
            create_execution_yaml  # pylint: disable=import-outside-toplevel

        campaign_id = os.path.basename(os.path.normpath(campaign_root))
        bucket, prefix = in_pod_storage.campaign_storage_location(
            self.cluster_config, campaign_id)
        storage = in_pod_storage.storage_client_for(self.cluster_config)

        # _execution/execution.yaml (best-effort cluster info; degrades in-pod).
        create_execution_yaml(self._runs or 0, campaign_root,
                              execution_params=self._execution_params,
                              context=self.kube_context)

        n = storage.upload_dir(campaign_root, bucket, prefix)
        logger.info("Published canonical campaign (%d file(s), incl. campaign.db / "
                    "_execution / metrics) to %s/%s", n, bucket, prefix)

    # Per-run JUnit report each scenario run uploads on completion; counting these
    # under the (flat, campaign-wide) prefix gives cumulative finished runs.
    _RUN_SENTINEL = "/test.xml"

    def count_run_artifacts(self, campaign_id: str) -> int | None:
        bucket, prefix = in_pod_storage.campaign_storage_location(
            self.cluster_config, campaign_id)
        if self._progress_storage is None:
            self._progress_storage = in_pod_storage.storage_client_for(self.cluster_config)
        keys = self._progress_storage.list_keys(bucket, prefix)
        return sum(1 for k in keys if k.endswith(self._RUN_SENTINEL))
