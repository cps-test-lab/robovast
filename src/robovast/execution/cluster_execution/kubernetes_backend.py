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
4. downloads that batch's per-config/run results, its job-artifact dir
   (``_jobs/<batch_tag>/`` — ``sysinfo.yaml``, resource monitor, logs) **and** the
   campaign-level snapshot (``_config``/``_transient``) back into ``campaign_root``,
   materialises the per-run ``job`` symlinks, and records
   ``_execution/execution.yaml``.

Steps 1–4 leave ``campaign_root`` **complete** — the same shape a local
(``DockerBackend``) run leaves — so the backend-agnostic analysis postprocessing
and the canonical publish (:meth:`KubernetesBackend.finalize_campaign`) consume it
identically. The object store is the source of truth; ``campaign_root`` is its
projection (the same one the service's ``fetch_campaign`` reconstructs on re-run).

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
                                       create_job_links,
                                       dump_multi_document_yaml,
                                       resolve_robovast_image,
                                       write_job_links_manifest)
from robovast.common import prepare_campaign_configs
from robovast.execution.backends import (CampaignConfigError, CampaignStopped,
                                          ExecutionBackend, RunOptions)
from robovast.execution.packer import build_jobs

from . import in_pod_storage
from .cluster_execution import _label_safe_campaign, blocked_job_reasons
from .kubernetes_kueue import KUEUE_QUEUE_NAME
from .manifests import JOB_TEMPLATE

logger = logging.getLogger(__name__)

# How often (seconds) the result-download progress logger emits a running count.
_DOWNLOAD_PROGRESS_INTERVAL = 5.0


def _download_progress_logger(batch_tag, interval=_DOWNLOAD_PROGRESS_INTERVAL):
    """Return a no-argument callback that logs the running download count.

    Passed to ``StorageClient.download_prefix`` as ``on_file`` — it is called
    once per fetched file and emits a throttled ``downloaded N so far`` line so a
    large batch's projection shows progress instead of sitting silent. The count
    is cumulative across the several ``download_prefix`` calls a search-mode batch
    makes (the callback is shared), so the log reads as one continuous total.
    """
    state = {"count": 0, "last": time.monotonic()}

    def on_file():
        state["count"] += 1
        now = time.monotonic()
        if now - state["last"] >= interval:
            state["last"] = now
            logger.info("Batch %s: downloaded %d result file(s) so far...",
                        batch_tag, state["count"])

    return on_file


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


def pullable_digest(image_id: str | None) -> str | None:
    """A pullable ``repo@sha256:…`` ref from a pod container's ``imageID``, or None.

    Kubernetes reports the exact image a container ran as ``status.imageID`` — usually
    ``registry/repo@sha256:<digest>``, sometimes prefixed (``docker-pullable://…``). We
    keep only refs that carry an ``@sha256:`` digest (a plain local ``sha256:…`` id is
    not pullable and is rejected), stripping any ``scheme://`` prefix.
    """
    if not image_id:
        return None
    ref = image_id.split("://", 1)[-1]
    return ref if "@sha256:" in ref else None


def resolve_image_digest(container_statuses, image: str) -> str | None:
    """The immutable digest ref of the SUT *image* from a run pod's container statuses.

    Prefers the container whose ``image`` matches the resolved SUT tag; falls back to
    any container that yields a pullable digest. Returns None when nothing usable is
    found (an old node, a missing status) — the caller then leaves the image unpinned
    rather than guessing.
    """
    statuses = list(container_statuses or [])
    for match_wanted in (True, False):
        for cs in statuses:
            if match_wanted and getattr(cs, "image", None) != image:
                continue
            digest = pullable_digest(getattr(cs, "image_id", None))
            if digest:
                return digest
    return None


class BatchJobRunner:
    """Build, submit and clean up the Kubernetes Jobs for **one** batch.

    Constructed only via :meth:`for_batch` from a pre-built ``campaign_data``
    (the controller has already composed it). Runs in-pod: storage I/O is direct
    (no archiver) and the Kubernetes client uses the in-cluster service account.
    The same builder is reused offline by ``vast exec cluster prepare-run`` to emit
    job manifests without touching the API (only :meth:`run_batch_in_pod` does).
    """

    #: Cooperative-stop signal, set by :meth:`for_batch`. Class-level default so a
    #: runner built another way (offline manifest emit, tests) has no stop wired.
    _state = None

    #: SUT image ref and the immutable digest captured from the run pods; class-level
    #: defaults so a runner built another way (offline manifest emit, tests) is safe.
    image = None
    _resolved_image_digest = None

    #: Effective per-Job ``activeDeadlineSeconds`` and the set of Jobs already logged
    #: as hard-killed on it. Class-level defaults for runners not built via
    #: :meth:`for_batch`.
    _deadline_seconds = None
    _deadline_killed = frozenset()

    #: How long a batch tolerates jobs stuck unable to start (image pull / config
    #: error) before failing with the Kubernetes reason. A short grace absorbs a
    #: transient registry blip while never letting a doomed image hang the campaign.
    _BLOCKED_GRACE_SECONDS = 60.0

    #: Fallback wall-clock cap *per run* when ``execution.timeout`` is unset, so a
    #: scenario Job that never shuts itself down is always force-killed by Kubernetes
    #: (via ``activeDeadlineSeconds``) rather than hanging the campaign forever. 1 hour.
    DEFAULT_RUN_DEADLINE_SECONDS = 60 * 60

    @classmethod
    def for_batch(cls, *, campaign_data, campaign_id, batch_tag, runs, cluster_config,
                  namespace, image, kube_context=None, log_tree=False, state=None):
        self = cls()
        self.cluster_config = cluster_config
        self.namespace = namespace
        # Used only for resolve_resources() (per-cluster resource lists); the
        # Kubernetes API client uses in-cluster config (see _ensure_k8s_initialized).
        self.kube_context = kube_context
        self.log_tree = log_tree
        # Cooperative-stop signal; ``None`` for offline callers (prepare-run).
        self._state = state

        self.campaign = campaign_id
        self.campaign_data = campaign_data
        self.configs = campaign_data.get("configs", [])
        self.num_runs = runs
        # The SUT image ref the run pods use; captured back as an immutable digest
        # after the batch runs (see run_batch_in_pod) so postprocessing reuses the
        # exact image the runs recorded their bags with.
        self.image = image
        self._resolved_image_digest = None
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
        # Always cap a Job's wall-clock time so a scenario that never shuts itself
        # down is force-killed by Kubernetes (``DeadlineExceeded``) instead of hanging
        # the campaign forever. ``execution.timeout`` is a *per-run* limit; scale by
        # the number of runs packed into a Job (default 1) so a packed Job isn't killed
        # prematurely. Falls back to ``DEFAULT_RUN_DEADLINE_SECONDS`` when unset.
        timeout = execution_params.get("timeout")
        per_run = int(timeout) if timeout else self.DEFAULT_RUN_DEADLINE_SECONDS
        self._deadline_seconds = per_run * self._runs_per_job()
        self.manifest["spec"]["activeDeadlineSeconds"] = self._deadline_seconds
        # Jobs already logged as hard-killed on the deadline, so the wait loop warns
        # once per job rather than every poll.
        self._deadline_killed = set()

        self.k8s_client = None
        self.k8s_batch_client = None
        self.k8s_api_client = None
        self._k8s_initialized = False
        return self

    def _ensure_k8s_initialized(self):
        """Initialise Kubernetes clients from the in-cluster service account."""
        if self._k8s_initialized:
            return
        from robovast.common.kube import load_kube_config  # pylint: disable=import-outside-toplevel
        # In-cluster in the service pod; host kubeconfig for host-side dry-runs / tests.
        load_kube_config(context=self.kube_context)
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

        # Pull secret for an agent-built experiment image pushed to a private
        # registry (see RegistryConfig). Only present when a registry with auth was
        # configured at setup; a public/insecure registry needs none.
        try:
            pull_secret = self.cluster_config.get_registry_config().pull_secret_name
        except Exception:  # noqa: BLE001 - registry config is optional
            pull_secret = ""
        if pull_secret:
            spec['imagePullSecrets'] = [{'name': pull_secret}]

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

            # Simulation backend (execution.simulation) -> --simulation in entrypoint.sh
            simulation = self.campaign_data.get('execution', {}).get('simulation')
            if simulation:
                containers[0]['env'].append({'name': 'SIMULATION', 'value': str(simulation)})

            # Runner selection (execution.mode) -> SCENARIO_MODE in entrypoint.sh
            mode = self.campaign_data.get('execution', {}).get('mode', 'auto')
            if mode and mode != 'auto':
                containers[0]['env'].append({'name': 'SCENARIO_MODE', 'value': str(mode)})

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
            try:
                job_status = self.k8s_batch_client.read_namespaced_job_status(name=job_name, namespace=self.namespace)
            except client.exceptions.ApiException as exc:
                if exc.status == 404:
                    # Job no longer exists: it finished and was garbage-collected
                    # (e.g. an external job-TTL policy) or was cleaned up. Either
                    # way it is no longer running, so just skip it.
                    logger.debug("Job %s not found (404); treating as finished.", job_name)
                    continue
                raise

            self._log_if_deadline_killed(job_name, job_status.status)

            # Check if job is still active/running
            if job_status.status.active is not None and job_status.status.active >= 1:
                running_jobs.append(job_name)
            # Check if job has not completed yet (no completion_time and no failure)
            elif job_status.status.completion_time is None and (job_status.status.failed is None or job_status.status.failed == 0):
                running_jobs.append(job_name)
        return running_jobs

    def _log_if_deadline_killed(self, job_name, status):
        """Emit a clear, greppable WARNING the first time *job_name* is seen to have
        been force-killed by ``activeDeadlineSeconds``.

        Kubernetes marks such a Job with a ``Failed`` condition whose reason is
        ``DeadlineExceeded``. These runs typically produce no ``/test.xml`` artifact,
        so this log line is the record that the job was hung and hard-stopped — for
        later analysis (grep ``HARD-KILLED by activeDeadlineSeconds``)."""
        if status is None or job_name in self._deadline_killed:
            return
        for cond in (status.conditions or []):
            if cond.type == "Failed" and cond.reason == "DeadlineExceeded":
                # ``_deadline_killed`` is a class-level frozenset for offline runners;
                # promote to an instance set before recording.
                if not isinstance(self._deadline_killed, set):
                    self._deadline_killed = set()
                self._deadline_killed.add(job_name)
                logger.warning(
                    "Campaign %s batch %s: job %s HARD-KILLED by activeDeadlineSeconds "
                    "(%ss) — likely hung, no self-shutdown.",
                    self.campaign, self._batch_tag, job_name, self._deadline_seconds)
                return

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

        # Kueue keys queue membership off the label (not an annotation); an
        # annotation is not honored by Kueue 0.16.x, so the job would never be
        # suspended/admitted and would run unmanaged.
        manifest.setdefault("metadata", {}).setdefault("labels", {})[
            "kueue.x-k8s.io/queue-name"
        ] = KUEUE_QUEUE_NAME

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

    def _capture_image_digest(self, job_label: str) -> None:
        """Record the immutable digest the run pods actually used for the SUT image.

        Read once, from this batch's pods while they still exist (before Job cleanup),
        so ``execution.yaml`` can pin ``:latest`` to the exact ``repo@sha256:…`` the runs
        ran — and postprocessing reuses that identical image. Best-effort: any failure
        leaves the image unpinned (the tag), never blocking the campaign.
        """
        if self._resolved_image_digest:
            return  # already captured (search mode calls this per batch)
        try:
            pods = self.k8s_client.list_namespaced_pod(
                self.namespace, label_selector=job_label).items
            statuses = [cs for pod in pods
                        for cs in (pod.status.container_statuses or [])]
            digest = resolve_image_digest(statuses, self.image)
        except Exception as exc:  # noqa: BLE001 - never block the run on a status read
            logger.debug("Could not resolve SUT image digest for %s: %s",
                         self.campaign, exc)
            return
        if digest:
            self._resolved_image_digest = digest
            logger.info("Pinned SUT image for %s to %s", self.campaign, digest)

    # -- in-pod execution ---------------------------------------------------

    def run_batch_in_pod(self, campaign_root: str, whole_campaign: bool = False):
        """Upload, run and download one batch; this batch's results and the
        campaign-level snapshot (``_config``/``_transient``) land under *campaign_root*.

        ``whole_campaign`` is set in batch mode, where this batch *is* the entire
        campaign: the prefix holds no other batch's artifacts, so the results can be
        fetched with a single prefix download instead of the per-config enumeration
        that search mode needs to scope ``_jobs/`` to the current batch."""
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

        job_label = f"jobgroup=scenario-runs,campaign-id={_label_safe_campaign(self.campaign)}"
        blocked_since = None
        while True:
            if self._state is not None and self._state.stop_requested:
                raise CampaignStopped(f"campaign {self.campaign} stopped during batch "
                                      f"{self._batch_tag}")
            remaining = self.get_remaining_jobs(job_names)
            if not remaining:
                break
            # A Job whose pod can't start (bad/missing image, no pull creds, ...) stays
            # "active" with a Pending pod forever, so this loop would otherwise spin
            # indefinitely with no progress. Detect it and, after a short grace window
            # (a transient registry blip may clear on its own), fail the batch with
            # Kubernetes' own message so the campaign reports *why* instead of hanging.
            try:
                blocked = blocked_job_reasons(self.k8s_client, self.namespace, job_label)
            except Exception as exc:  # noqa: BLE001 - probe failed this iteration
                # Could not check pods this cycle. Treat as "unknown", NOT as
                # "nothing blocked": clearing blocked_since here would silently reset
                # the grace timer and let a truly blocked batch hang until the
                # deadline hard-kill. Keep any existing blocked state and retry.
                logger.warning("Batch %s: could not check for blocked jobs: %s",
                               self._batch_tag, exc)
                blocked = None
            if blocked:
                reasons = "; ".join(sorted(set(blocked.values())))
                if blocked_since is None:
                    blocked_since = time.monotonic()
                    logger.warning("Batch %s: %d job(s) cannot start: %s",
                                   self._batch_tag, len(blocked), reasons)
                elif time.monotonic() - blocked_since >= self._BLOCKED_GRACE_SECONDS:
                    raise CampaignConfigError(
                        f"{len(blocked)} scenario job(s) cannot start after "
                        f"{self._BLOCKED_GRACE_SECONDS:.0f}s and will not recover — "
                        f"Kubernetes reports: {reasons}. Check the execution image "
                        f"reference and pull credentials.")
            elif blocked is not None:
                # A successful probe that found nothing blocked clears the timer.
                blocked_since = None
            # blocked is None (probe failed) => leave blocked_since unchanged.
            logger.info("Batch %s: %d/%d job(s) still running...",
                        self._batch_tag, len(remaining), len(job_names))
            time.sleep(2)
        # A stop that landed while the last jobs were being torn down leaves the loop
        # via the empty-remaining path; catch it here too before the result download.
        if self._state is not None and self._state.stop_requested:
            raise CampaignStopped(f"campaign {self.campaign} stopped during batch "
                                  f"{self._batch_tag}")
        logger.info("Batch %s: all jobs finished.", self._batch_tag)
        self._capture_image_digest(job_label)
        if self._deadline_killed:
            logger.warning(
                "Batch %s: %d job(s) hard-killed on the %ss deadline: %s",
                self._batch_tag, len(self._deadline_killed), self._deadline_seconds,
                ", ".join(sorted(self._deadline_killed)))

        # 4. Project this batch's results — and the campaign-level snapshot — from
        #    the object store into the campaign root, so the host campaign is
        #    complete (matching a local run and the service's fetch_campaign).
        os.makedirs(campaign_root, exist_ok=True)
        # A large batch's download is otherwise silent between "all jobs finished"
        # and the final "downloaded N" line — potentially minutes on hundreds of
        # files. Announce the start and log a running count every few seconds so
        # the campaign log shows progress instead of appearing hung.
        logger.info("Batch %s: downloading result files from object store...",
                    self._batch_tag)
        on_file = _download_progress_logger(self._batch_tag)
        # The 3s run-progress poller lists the whole campaign prefix over this same
        # (off-cluster) storage tunnel; pause it for the duration of the download so
        # the transfer runs uncontended. Resumed in the finally so a download error
        # (or an early return) can never leave the poller permanently off.
        got = 0
        if self._state is not None:
            self._state.suspend_progress()
        try:
            if whole_campaign:
                # Batch mode: this batch *is* the whole campaign, so the prefix holds
                # nothing but its own artifacts. One prefix download does a single
                # paginated list instead of one list per config — the per-config
                # enumeration below costs 600+ sequential list calls on a large batch,
                # during which the campaign sits in "running" with no progress.
                got = storage.download_prefix(bucket_name, campaign_prefix, campaign_root,
                                              on_file=on_file)
            else:
                # Search mode: the campaign prefix is flat/shared across batches, so we
                #    fetch by name: this batch's <config>/ dirs (self.configs == this
                #    batch's composed configs, the same names the controller scores at
                #    campaign_root/<config>/) and its job-artifact dir _jobs/<batch_tag>/
                #    (sysinfo.yaml, resource monitor, logs — read via each run's `job`
                #    symlink), plus the campaign-level _config/ (holds the .vast the
                #    auto-chain postprocessing reads) and _transient/. Batch-scoping
                #    _jobs avoids re-fetching prior batches each iteration; the small
                #    campaign-level dirs are re-fetched (idempotent — download_prefix
                #    never deletes, so the locally-accumulated _transient/job_links.yaml
                #    survives).
                job_root = f"_jobs/{self._batch_tag}" if self._batch_tag else "_jobs"
                targets = [c["name"] for c in self.configs if c.get("name")]
                targets += ["_config", "_transient", job_root]
                for rel in targets:
                    got += storage.download_prefix(
                        bucket_name, f"{campaign_prefix}{rel}",
                        os.path.join(campaign_root, rel), on_file=on_file)
        finally:
            if self._state is not None:
                self._state.resume_progress()
        logger.info("Batch %s: downloaded %d result file(s) into %s",
                    self._batch_tag, got, campaign_root)

        # 4b. Record this batch's <config>/<run>/job -> _jobs/<batch>/job-<idx>
        #     links, then materialise them as real symlinks now (not only at
        #     upload-to-share), so downstream readers resolve <run>/job/sysinfo.yaml
        #     during the driver's own metadata/postprocessing — as in a local run.
        self._write_job_links(campaign_root)
        create_job_links(campaign_root)

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
                 log_tree=False, state=None):
        self.cluster_config = cluster_config
        self.namespace = namespace
        self.kube_context = kube_context
        self.log_tree = log_tree
        # Cooperative-stop signal (Ctrl+C / Stop): lets the batch wait loop abort
        # cleanly instead of pressing on into a doomed result download.
        self._state = state
        # Lazily-built read-only storage client for count_run_artifacts (the
        # controller's progress poller); separate from the write path.
        self._progress_storage = None

    def run_batch(self, campaign_data: dict, *, campaign_root: str, batch_tag: str,
                  runs: int, options: RunOptions, whole_campaign: bool = False) -> None:
        campaign_id = os.path.basename(os.path.normpath(campaign_root))
        execution_params = campaign_data.get("execution", {}) or {}
        image = resolve_robovast_image(
            required=True,
            explicit=options.image,
            config_image=execution_params.get("image"),
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
            state=self._state,
        )
        runner.run_batch_in_pod(campaign_root, whole_campaign=whole_campaign)

        # Record _execution/execution.yaml now (not at finalize), so the campaign
        # root is complete before the controller chains analysis postprocessing —
        # which reads the execution image from it. This mirrors the local backend,
        # whose run.sh writes execution.yaml during the run. Best-effort cluster
        # info; degrades in-pod. Idempotent across a search's repeated batches.
        from robovast.common.execution import \
            create_execution_yaml  # pylint: disable=import-outside-toplevel
        create_execution_yaml(runs, campaign_root,
                              execution_params=execution_params,
                              context=self.kube_context,
                              image_digest=getattr(runner, "_resolved_image_digest", None))

    def finalize_campaign(self, campaign_root: str) -> None:
        """Publish the canonical campaign to storage so the bucket matches local.

        Jobs upload raw per-run results (``<config>/<run>/test.xml`` etc.) and
        ``_jobs/`` *before* the controller runs search postprocessing, and each
        batch uploads ``_config``/``_transient`` and records
        ``_execution/execution.yaml``. This step publishes the full in-pod
        ``campaign_root`` — which additionally holds ``campaign.db`` and the
        postprocessing-derived per-run artifacts (e.g. ``metrics.csv``, written next
        to ``trajectory.csv`` by ``QuadMetrics``) — so ``upload-to-share`` +
        ``download`` yield a layout identical to a local run. Re-uploading the small
        raw files is idempotent.
        """
        campaign_id = os.path.basename(os.path.normpath(campaign_root))
        bucket, prefix = in_pod_storage.campaign_storage_location(
            self.cluster_config, campaign_id)
        storage = in_pod_storage.storage_client_for(self.cluster_config)

        n = storage.upload_dir(campaign_root, bucket, prefix)
        logger.info("Published canonical campaign (%d file(s), incl. campaign.db / "
                    "_execution / metrics) to %s/%s", n, bucket, prefix)

    def preflight_upload_to_share(self) -> None:
        """Fail fast when ``--upload-to-share`` is set but no share is configured.

        The upload runs in-driver (this same process/env), so the env checked here is
        exactly the one :meth:`share_campaign` would read at the finish tail. Raising
        now — before the campaign runs — turns what used to be a silent, end-of-run
        skip into an up-front, actionable error.
        """
        from robovast.execution.backends import \
            CampaignConfigError  # pylint: disable=import-outside-toplevel
        from robovast.execution.cluster_execution import \
            in_pod_upload  # pylint: disable=import-outside-toplevel

        if not in_pod_upload.share_type_configured():
            raise CampaignConfigError(
                "--upload-to-share was requested, but no share provider is configured: "
                "ROBOVAST_SHARE_TYPE is unset in the campaign environment.\n"
                "Set it (and its provider credentials) in the environment / .env that "
                "'vast serve' runs with — e.g.\n"
                "  ROBOVAST_SHARE_TYPE=gcs\n"
                "  ROBOVAST_GCS_BUCKET=my-robovast-results\n"
                "— or drop --upload-to-share.")

    def share_campaign(self, campaign_root: str, options) -> None:
        """Stream the raw campaign straight to the configured share provider.

        Overrides the local tar.gz-on-disk behaviour: the campaign is already on the
        driver's scratch (the batch runner downloaded it back for scoring), so it is
        tarred + gzipped **on the fly** into the provider's request body — no
        compressed copy ever lands on disk, which matters for ~1TB campaigns. Runs
        before analysis postprocessing, so the shared archive is the minimal raw
        snapshot. A share failure is surfaced but never loses the campaign (the
        controller wraps this call).
        """
        from robovast.execution import campaign_archive  # pylint: disable=import-outside-toplevel
        from robovast.execution.cluster_execution import in_pod_upload  # pylint: disable=import-outside-toplevel

        from robovast.execution.backends import \
            CampaignConfigError  # pylint: disable=import-outside-toplevel

        campaign_id = os.path.basename(os.path.normpath(campaign_root))
        provider = in_pod_upload.load_provider_from_env()
        if provider is None:
            # Unreachable in normal flow: preflight_upload_to_share() already rejected
            # this at campaign start. Kept as a loud backstop rather than a silent skip
            # so a share going unconfigured can never pass unnoticed again.
            raise CampaignConfigError(
                "upload-to-share enabled but no share provider is configured "
                "(ROBOVAST_SHARE_TYPE unset) for %s." % campaign_id)
        in_pod_upload.verify_share_access(provider)
        object_name = f"{campaign_id}.tar.gz"
        logger.info("Streaming raw campaign %s to %s share as %s...",
                    campaign_id, provider.SHARE_TYPE, object_name)
        with campaign_archive.campaign_tar_stream(campaign_root) as stream:
            provider.upload_archive_stream(stream, object_name)
        logger.info("Uploaded %s to the %s share.", object_name, provider.SHARE_TYPE)

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
