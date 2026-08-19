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
submits/polls/cleans up the Jobs, and writes the per-run job-link manifest.

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
collide. A ``_batch_tag`` of ``None`` selects the classic single-batch layout.
"""

import copy
import hashlib
import logging
import os
import re
import shlex
import tempfile
import time

import yaml
from kubernetes import client

from robovast.common import (COMPAT_VERSION, MIN_IMAGE_COMPAT, get_execution_env_variables,
                             plan_containers, prepare_campaign_configs, scenario_env)
from robovast.common.common import get_scenario_parameters
from robovast.common.config import per_run_deadline_seconds
from robovast.common.execution import (build_job_parameter_documents, create_job_links,
                                       dump_multi_document_yaml, job_artifact_rel, read_job_links,
                                       resolve_sidecar_image, sidecar_backend_env,
                                       write_job_links_manifest)
from robovast.common.simulators import SIM_OVERRIDES_MOUNT, SIMULATION_CONTAINER, sim_job_overlay
from robovast.execution.backends import (CampaignConfigError, CampaignStopped, ExecutionBackend,
                                         RunOptions)
from robovast.execution.packer import build_jobs

from . import in_pod_storage
from .cluster_context import resolve_resources
from .cluster_execution import _label_safe_campaign, blocked_job_reasons, restarted_job_reasons
from .kubernetes_gpu import GPU_RESOURCE
from .kubernetes_kueue import KUEUE_QUEUE_NAME
from .manifests import JOB_TEMPLATE

logger = logging.getLogger(__name__)

#: Handed to the NVIDIA container runtime for a GPU container. The load-bearing member is
#: ``graphics``: it is what injects ``/dev/dri``, ``libEGL_nvidia`` and the glvnd ICD, and
#: without it the container gets the device but no way to render on it -- which is not an
#: error anywhere, just a job that quietly renders in software many times slower. ``all``
#: rather than an explicit list so one ``.vast`` key means the same thing here as on the
#: Compose lane, which already writes exactly this.
GPU_DRIVER_CAPABILITIES = "all"

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


def _instance_type_command(cluster_config) -> str | None:
    """The provider's shell line for recording the node's instance type, or ``None``.

    Each cluster config implements ``get_instance_type_command`` — GCP and Azure query
    their metadata service for the machine type, bare-metal providers report the
    architecture. Best-effort: a provider that does not implement it (the base class
    raises) simply records no instance type, because sysinfo collection must never be the
    reason a campaign fails.

    .. todo::

       Only the ``uname -m`` implementations (rke2, minikube) have been exercised. The
       GCP and Azure commands query a cloud metadata service and are **untested** —
       verify the recorded ``instance_type`` on a real run on each before relying on it
       (a wrong URL or response shape would silently record an empty string).
    """
    try:
        return cluster_config.get_instance_type_command()
    except (NotImplementedError, AttributeError):
        logger.debug("cluster config %s records no instance type",
                     type(cluster_config).__name__)
        return None


def _s3_env(endpoint, bucket, access_key, secret_key, prefix) -> tuple:
    """The credentials ``/tmp/s3_upload.sh`` reads at run time.

    Given to the sidecars as well as the main container. A sidecar runs the same script
    when its workload exits, because whatever it wrote after the main container's upload
    -- the simulator's recording, and the tail of every sidecar log -- would otherwise die
    with the pod's emptyDir.
    """
    return (
        ('S3_ENDPOINT', endpoint),
        ('S3_BUCKET', bucket),
        ('S3_ACCESS_KEY', access_key),
        ('S3_SECRET_KEY', secret_key),
        ('S3_PREFIX', prefix),
    )


def _run_output_dir_env(job) -> tuple:
    """``RUN_OUTPUT_DIR`` for a job that is exactly one run, else nothing.

    ``/out`` is the pod's campaign root and ``OUTPUT_DIR`` is a per-*job* subdir, so neither names the
    place this run's results land -- ``/out/<config>/<run>``, which scenario_execution derives per work
    item from its parameter document. A process the scenario merely *launched* (a simulator brought up
    by a ROS launch file, say) therefore has nowhere correct to drop a per-run artifact: writing to
    ``/out`` collides across runs, since every pod mirrors its ``/out`` into the same campaign prefix.

    So name it, for the case where it is unambiguous. With the default packing (``runs_per_job: 1``,
    :class:`OnePerJob`) that is every job; a packed job runs several work items sequentially and one
    variable cannot serve them all, so it is omitted rather than made wrong, and a consumer falls back
    to ``OUTPUT_DIR``.
    """
    items = getattr(job, "items", None) or []
    if len(items) != 1:
        return ()
    item = items[0]
    return (('RUN_OUTPUT_DIR', f"/out/{item.config_name}/{item.run_number}"),)


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
    Building manifests touches no API; only :meth:`run_batch_in_pod` does.
    """

    #: Cooperative-stop signal, set by :meth:`for_batch`. Class-level default so a
    #: runner built another way (offline manifest emit, tests) has no stop wired.
    _state = None

    #: SUT image ref and the immutable digest captured from the run pods; class-level
    #: defaults so a runner built another way (offline manifest emit, tests) is safe.
    image = None
    _resolved_image_digest = None
    _resolved_image_digests: dict = {}

    #: Effective per-Job ``activeDeadlineSeconds`` and the set of Jobs already logged
    #: as hard-killed on it. Class-level defaults for runners not built via
    #: :meth:`for_batch`.
    _deadline_seconds = None
    _deadline_killed = frozenset()

    #: How long a batch tolerates jobs stuck unable to start (image pull / config
    #: error) before failing with the Kubernetes reason. A short grace absorbs a
    #: transient registry blip while never letting a doomed image hang the campaign.
    _BLOCKED_GRACE_SECONDS = 60.0

    #: How often the wait loop re-checks the Kueue admission path and reports why jobs
    #: are still suspended. Much slower than the 2s poll: a queue does not break every
    #: two seconds, and a normal quota wait must not spam the campaign log.
    _SUSPEND_CHECK_INTERVAL_SECONDS = 30.0

    @classmethod
    def for_batch(cls, *, campaign_data, campaign_id, batch_tag, runs, cluster_config,
                  namespace, image, kube_context=None, log_tree=False, state=None,
                  built_images=None):
        self = cls()
        self.cluster_config = cluster_config
        self.namespace = namespace
        # Used only for resolve_resources() (per-cluster resource lists); the
        # Kubernetes API client uses in-cluster config (see _ensure_k8s_initialized).
        self.kube_context = kube_context
        self.log_tree = log_tree
        # Cooperative-stop signal; ``None`` for offline callers (manifest emit, tests).
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
        self._resolved_image_digests = {}
        # ``None`` ⇒ classic single-batch layout; the controller sets
        # a tag per search batch so jobs/param files/storage prefix don't collide.
        self._batch_tag = batch_tag

        execution_params = campaign_data.get("execution", {}) or {}
        self.pre_command = execution_params.get("pre_command")
        self.post_command = execution_params.get("post_command")
        self.run_as_user = execution_params.get("run_as_user", 1000)

        # One container plan, shared with the local lane and exec_in_container.
        self.plan = plan_containers(execution_params, images=built_images,
                                    explicit_main=image)
        # Once per campaign, not per job: a `list_node()` per job would add an API call to
        # every one of a sweep's runs to answer a question whose answer cannot change
        # between them.
        self._discover_gpu_support()
        # Builds self.manifest and sets self.env.
        self.manifest = self.get_job_manifest(
            self.plan.main.image or image,
            self.plan.main.resources or {},
            execution_params.get("env", []),
            self.run_as_user,
        )
        # Always cap a Job's wall-clock time so a scenario that never shuts itself
        # down is force-killed by Kubernetes (``DeadlineExceeded``) instead of hanging
        # the campaign forever. ``execution.timeout`` is a *per-run* limit; scale by
        # the number of runs packed into a Job (default 1) so a packed Job isn't killed
        # prematurely. The per-run figure comes from ``common.config`` because the
        # campaign status uses the same one to decide a run is stalled — were the two
        # to diverge, a Job could be killed while the status still called it healthy.
        self._deadline_seconds = (per_run_deadline_seconds(execution_params)
                                  * self._runs_per_job())
        self.manifest["spec"]["activeDeadlineSeconds"] = self._deadline_seconds
        # Pod-level, so it has to be decided across every container of the plan rather
        # than inside get_job_manifest -- which sees only the main container and would
        # therefore lose the case that matters most, a GPU on the simulation sidecar.
        self._apply_pod_gpu_runtime()
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
        from .kube_client import load_kube_config  # pylint: disable=import-outside-toplevel

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
        return job_artifact_rel(index, self._batch_tag)

    def _build_job_manifest(self, *, job_short_name, job_full_name, item_tag,
                            sim_overlay=None,
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
        # configured at setup; a public/insecure registry needs none. Falls back to the
        # well-known Secret setup creates when the env var naming it is absent — which is
        # the normal case for an off-cluster service, since setup writes that name into the
        # deployed pod's env (see ClusterService._resolve_registry_objects).
        try:
            pull_secret = self.cluster_config.get_registry_config().pull_secret_name
            if not pull_secret:
                from .service_deploy import REGISTRY_PUSH_SECRET_NAME
                try:
                    self.k8s_client.read_namespaced_secret(
                        REGISTRY_PUSH_SECRET_NAME, self.namespace)
                    pull_secret = REGISTRY_PUSH_SECRET_NAME
                except client.exceptions.ApiException:
                    pull_secret = ""
        except Exception:  # noqa: BLE001 - registry config is optional
            pull_secret = ""
        if pull_secret:
            spec['imagePullSecrets'] = [{'name': pull_secret}]

        # Hosts the cluster's DNS cannot resolve (ROBOVAST_EXTRA_HOST_ALIASES). This
        # covers what the *pod* resolves; the image pull itself is the node's container
        # runtime, which no pod spec reaches — see BaseConfig.get_host_aliases.
        try:
            host_aliases = self.cluster_config.get_host_aliases()
        except Exception:  # noqa: BLE001 - never block a run on an optional alias list
            logger.warning("could not resolve host aliases for the campaign Job",
                           exc_info=True)
            host_aliases = []
        if host_aliases:
            spec['hostAliases'] = host_aliases

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
                'image': resolve_sidecar_image(),
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
            for k, v in _s3_env(s3_endpoint, bucket_name, s3_access_key,
                                s3_secret_key, s3_prefix):
                containers[0]['env'].append({'name': k, 'value': v})

            # Add PRE_COMMAND and POST_COMMAND if specified
            if self.pre_command:
                containers[0]['env'].append({'name': 'PRE_COMMAND', 'value': str(self.pre_command)})
            if self.post_command:
                containers[0]['env'].append({'name': 'POST_COMMAND', 'value': str(self.post_command)})
            if self.log_tree:
                containers[0]['env'].append({'name': 'SCENARIO_EXECUTION_PARAMETERS', 'value': '-t'})

            # SCENARIO_FILE / SIMULATION / SCENARIO_MODE, derived from the .vast by the
            # same helper the local lane and container-exec use.
            for name, val in scenario_env(self.campaign_data).items():
                containers[0]['env'].append({'name': name, 'value': str(val)})

            for k, v in extra_main_env:
                containers[0]['env'].append({'name': k, 'value': v})

            containers[0]['volumeMounts'] = shared_volume_mounts

        # Add secondary containers (they receive the same packed env so a
        # sim/SUT server resolves file-valued reset parameters identically).
        for sc in self.plan.sidecars:
            sc_name = sc.name
            sc_resources = resolve_resources(sc.resources, self.kube_context)
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
            # A sidecar with no command runs the scenario-execution server, so the
            # scenario can drive it with remote(). One that declares a command runs that
            # instead -- a simulator, or a stack RoboVAST does not drive. BOTH go through
            # secondary_entrypoint.sh: it sources the ROS overlay, tees stdout into the
            # job's log dir and starts the resource monitor, none of which a container
            # gets when its command is exec'd as the entrypoint. Passing the command by
            # env rather than as argv is what lets the same entrypoint serve both.
            # The backend's own env, for the container the backend describes. scenario_env
            # puts it on the main container, which is only right when the simulator IS the
            # main container (the stepped shape); in the ROS shape it is this sidecar.
            # The job's own resolved values win over the campaign default: a world belongs
            # to a configuration, and this sidecar is running one.
            sc_backend_env = dict(sidecar_backend_env(
                self.campaign_data.get('execution') or {}, sc_name))
            if sc_name == SIMULATION_CONTAINER and sim_overlay:
                sc_backend_env.update(sim_overlay.get('env') or {})
            for key, value in sc_backend_env.items():
                secondary_env.append({'name': key, 'value': value})
            # So this sidecar can run /tmp/s3_upload.sh once its workload has exited.
            for k, v in _s3_env(s3_endpoint, bucket_name, s3_access_key,
                                s3_secret_key, s3_prefix):
                secondary_env.append({'name': k, 'value': v})
            # The simulator's command is the one per-configuration thing in the plan: it
            # names the world. Everything else about this container -- image, resources,
            # packages -- stays campaign-level.
            sc_cmd = sc.command
            if sc_name == SIMULATION_CONTAINER and sim_overlay and sim_overlay.get('command'):
                sc_cmd = sim_overlay['command']
            if sc_cmd:
                secondary_env.append({'name': 'ROBOVAST_CONTAINER_COMMAND',
                                      'value': shlex.join(list(sc_cmd))})
            secondary_spec = {
                'name': sc_name,
                'image': sc.image,
                # A NATIVE SIDECAR: an init container with restartPolicy Always. That is
                # what ties the pod's lifetime to the scenario rather than to whichever
                # container happens to run longest. As an ordinary container, a sidecar
                # running a simulator never exits -- a simulator has no reason to -- so the
                # Job stayed at 0/1 forever after the scenario had finished and uploaded
                # its results, and the controller polled "1/1 job(s) still running" until
                # something killed it. The command-less sidecars hid this: their
                # scenario-execution server has a --watchdog that ends it when the client
                # disconnects, so `sut` stopped and only `simulation` was left behind.
                #
                # Kubelet starts native sidecars before the regular containers and
                # terminates them after the last one exits, so this also fixes the
                # start-up order for free: the simulator is up before the scenario.
                # Stable since k8s 1.29.
                'restartPolicy': 'Always',
                'command': ['/usr/bin/tini', '--', '/bin/bash',
                            '/config/secondary_entrypoint.sh'],
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
            self._apply_gpu_to_container(secondary_spec, secondary_env,
                                         self._gpu_request(sc_resources, sc))
            if self.run_as_user is not None:
                secondary_spec.setdefault('securityContext', {})['runAsUser'] = self.run_as_user
            # Appended AFTER s3-init, which is an ordinary init container and therefore
            # runs to completion first -- it is what populates /config, and a sidecar
            # reads secondary_entrypoint.sh from there.
            spec['initContainers'].append(secondary_spec)

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
        sim_overlay = self._sim_overlay(job)
        # The simulator's overrides document ships per job (``<job-tag>.sim.yaml``, unique
        # like the parameter file) but is READ at a fixed path, because a backend builds
        # its command before any job exists and argv cannot expand an environment
        # variable. Locally the two are reconciled by the bind mount's target; here the
        # whole ``_transient/`` prefix is mirrored wholesale, so the reconciliation is this
        # one copy.
        sim_rename = (
            f"(cp /config/{job_tag}.sim.yaml {SIM_OVERRIDES_MOUNT} 2>/dev/null || true); "
            if sim_overlay["document"] else "")
        per_config_mirror = "".join(
            f"(mc mirror mystore/$S3_BUCKET/${{S3_CAMPAIGN_PREFIX}}{cn}/_config/ /config/{cn}/ 2>/dev/null || true); "
            for cn in job.config_names
        )
        init_cmd = (
            f"mc alias set mystore \"$S3_ENDPOINT\" \"$S3_ACCESS_KEY\" \"$S3_SECRET_KEY\" && "
            f"mc mirror mystore/$S3_BUCKET/${{S3_CAMPAIGN_PREFIX}}_config/ /config/ && "
            f"mc mirror mystore/$S3_BUCKET/${{S3_CAMPAIGN_PREFIX}}_transient/ /config/ && "
            f"{per_config_mirror}"
            f"{sim_rename}"
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
        ) + _run_output_dir_env(job)
        return self._build_job_manifest(
            job_short_name=_short_job_name(self.campaign, job_tag, job.index),
            job_full_name=f"{self.campaign}-{job_tag}",
            item_tag=job_tag,
            total_jobs=total_jobs,
            s3_prefix=campaign_prefix.rstrip("/"),
            init_cmd=init_cmd,
            extra_main_env=extra_env,
            sim_overlay=sim_overlay,
        )

    def _sim_overlay(self, job) -> dict:
        """This job's resolved simulator command, environment and overrides document.

        ``job.items[0]`` speaks for the whole job: the packer groups work items by
        ``sim_key``, so a job that mixed simulator settings cannot be built. Asked of the
        backend with the job's own block, which is why the world differs per job while the
        image, the resources and the container set do not.
        """
        return sim_job_overlay(self.campaign_data.get("execution") or {},
                               job.items[0].config.get("sim") or {},
                               os.path.dirname(self.campaign_data.get("vast") or ""))

    def _runs_per_job(self) -> int:
        """How many runs (config × run-number work items) to pack into one job."""
        return int((self.campaign_data.get("execution") or {}).get("runs_per_job") or 1)

    def _build_jobs(self):
        """Group (config, run) work items into jobs per runs_per_job.

        Deterministic, so the jobs used to write per-job param files match the
        jobs used to create job manifests.
        """
        return build_jobs(self.configs, self.num_runs, self.campaign_data.get("execution") or {})

    def _write_job_param_files(self, out_dir, campaign_root=None):
        """Write one multi-document scenario-parameter file per packed job into
        ``out_dir/_transient/`` so they upload with the campaign and are mirrored
        into each packed job's ``/config`` as ``job-<idx>.params.yaml``."""
        # Already resolved against the .vast's location by config generation (same note as in
        # execute_local); prepending the .vast's directory again doubles it whenever the project's
        # config path has a directory part.
        scenario_path = self.campaign_data["scenario_file"]
        scenario_name = next(iter(get_scenario_parameters(scenario_path).keys()))
        transient_dir = os.path.join(out_dir, "_transient")
        os.makedirs(transient_dir, exist_ok=True)
        jobs = self._build_jobs()
        for job in jobs:
            docs = build_job_parameter_documents(job, scenario_name)
            with open(os.path.join(transient_dir, f"{self._job_tag(job.index)}.params.yaml"), "w") as f:
                f.write(dump_multi_document_yaml(docs))
            # The simulation channel's per-job document. Single-document, because the
            # packer groups by `sim_key` and a job's items therefore agree on it.
            document = self._sim_overlay(job)["document"]
            if document:
                with open(os.path.join(transient_dir,
                                       f"{self._job_tag(job.index)}.sim.yaml"), "w") as f:
                    yaml.dump(document, f, default_flow_style=False, sort_keys=False)
        # Canonical link manifest, consumed by the controller's upload-to-share
        # compression to materialise <config>/<run>/job symlinks into the tar.gz, and
        # by readers resolving a job's artifacts while it is still running. Written in
        # per-batch mode too: the manifest is batch-aware, so the batch-namespaced job
        # tag no longer breaks the target path.
        #
        # Seeded with what the campaign already has, because THIS DIRECTORY IS UPLOADED TO
        # THE CAMPAIGN PREFIX: a manifest holding only this batch overwrites the campaign's
        # at the same key, and every earlier batch's runs stop resolving. The damage is not
        # even consistent -- ``download_prefix`` skips a file whose local size matches the
        # remote, so a same-sized batch-only copy is sometimes skipped and sometimes not, and
        # a four-batch campaign was observed keeping exactly its last two batches.
        # *campaign_root* is absent only for the one-shot template dir (``vast prepare``),
        # which has no campaign to accumulate onto.
        write_job_links_manifest(
            transient_dir, jobs, self._batch_tag,
            base=read_job_links(campaign_root) if campaign_root else None)

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

    def get_job_manifest(self, image: str, resources: dict, env: list,
                         run_as_user: int = None) -> dict:
        """Generate the base Kubernetes job manifest from templates.

        Args:
            image: Image for the container the scenario runs in
            resources: Resource limits/requests for that container (cpu, memory)
            env: List of environment variables
            run_as_user: UID to run container as (defaults to 1000 if None)

        Returns:
            Dictionary containing the job manifest
        """
        if run_as_user is None:
            run_as_user = 1000

        # Normalize resources: may be a dict or a Pydantic model
        if hasattr(resources, 'cpu'):
            # `gpu` is carried through here too. Listing only cpu/memory silently dropped
            # a declared GPU for any model-shaped input, which is exactly the shape of
            # failure this whole path has to avoid: no error, just software rendering.
            resources = {'cpu': resources.cpu, 'memory': resources.memory,
                         'gpu': getattr(resources, 'gpu', None)}

        # Resolve per-cluster resource values for the active Kubernetes context
        resources = resolve_resources(resources, self.kube_context)

        self.env = env or []

        logger.debug(f"Using run_as_user={run_as_user} for job containers")

        yaml_str = JOB_TEMPLATE.format(image=image, namespace=self.namespace,
                                       compat_version=COMPAT_VERSION,
                                       min_compat_version=MIN_IMAGE_COMPAT)
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
        # In the stepped shape the simulator IS this container, so this is where its GPU
        # goes; in the ROS shape the sidecar below carries it instead.
        main_env = main_container.setdefault('env', [])
        self._apply_gpu_to_container(
            main_container, main_env,
            self._gpu_request(resources, getattr(getattr(self, "plan", None), "main", None)))

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

    def _discover_gpu_support(self) -> None:
        """Record whether this cluster can schedule GPUs, and how they must be requested.

        Both answers come from the live cluster, so a campaign needs no per-cluster
        configuration to do the right thing: the same ``.vast`` renders on a GPU where one
        is advertised and in software where none is. A cluster that cannot answer is a
        cluster without GPUs as far as this run is concerned -- never an error, because a
        CPU-only cluster is the ordinary case and must behave exactly as it did before.
        """
        self._gpu_capacity = 0
        self._gpu_runtime_class = None
        try:
            from .kubernetes_gpu import get_cluster_allocatable_gpus, gpu_runtime_class_for
            self._gpu_capacity = get_cluster_allocatable_gpus(
                kube_context=self.kube_context)
            if self._gpu_capacity:
                # Asked rather than assumed, and "cannot tell" is not "no" -- see
                # gpu_runtime_class_for for why an unreadable answer still names the class.
                self._gpu_runtime_class = gpu_runtime_class_for(
                    kube_context=self.kube_context)
        except Exception as exc:  # noqa: BLE001 - absence of GPUs is not a failure
            logger.debug("Could not determine GPU support (%s); assuming none", exc)

    def _gpu_request(self, resources, container=None) -> int:
        """How many GPUs one container should request.

        An explicit ``resources.gpu`` always wins, ``0`` included -- that is how a campaign
        opts out of a GPU on a cluster that has one, e.g. to run wider than the advertised
        replica count. Otherwise the container that runs the simulator asks for one if the
        cluster advertises any, which is what makes "use the GPU if there is one" need no
        ``.vast`` edit at all.

        The cost of that convenience, stated where it is incurred: a ``.vast`` no longer
        fully determines the pod, so the same file yields different pods on a GPU cluster
        and a CPU one. The run's own log records which backend it bound, so the result
        stays interpretable afterwards.
        """
        declared = (resources or {}).get('gpu')
        if declared is not None:
            try:
                return max(0, int(declared))
            except (TypeError, ValueError):
                logger.warning("Ignoring non-numeric resources.gpu %r", declared)
                return 0
        if container is None or SIMULATION_CONTAINER not in (container.roles or ()):
            return 0
        return 1 if getattr(self, "_gpu_capacity", 0) else 0

    def _apply_gpu_to_container(self, spec, env_list, count) -> None:
        """Put *count* GPUs on one container spec, with the env the runtime needs."""
        if not count:
            return
        # Both requests and limits. Kubernetes defaults one from the other when a *Pod* is
        # created, but Kueue computes a workload's quota from the Job's pod *template*,
        # which no pod has been created from yet -- so a request left empty is accounted as
        # zero GPUs and admitted straight past the quota.
        spec['resources'].setdefault('requests', {})[GPU_RESOURCE] = str(count)
        spec['resources'].setdefault('limits', {})[GPU_RESOURCE] = str(count)
        # NVIDIA_VISIBLE_DEVICES is deliberately NOT set, and the asymmetry with the
        # Compose lane (which sets it to `all`) is the point: there, nothing allocates
        # devices, so the container must claim them. Here the device plugin injects the
        # UUID it allocated into exactly the container that requested one, and overriding
        # that with `all` would hand every container every GPU regardless of quota.
        env_list.append({'name': 'NVIDIA_DRIVER_CAPABILITIES',
                         'value': GPU_DRIVER_CAPABILITIES})

    def _apply_pod_gpu_runtime(self) -> None:
        """Set ``runtimeClassName`` when any container of the plan wants a GPU.

        ``runtimeClassName`` is a pod field with no per-container form, so one container
        asking for a GPU decides it for the pod. On RKE2 it is also the only thing that
        makes the request usable: nvidia is a registered runtime rather than the default
        one, so without it the kubelet allocates the device and the container gets no
        driver, no ``/dev/dri`` and no way to render -- while the quota is still charged.
        """
        plan = getattr(self, "plan", None)
        if plan is None or not getattr(self, "_gpu_runtime_class", None):
            return
        wants = any(self._gpu_request(resolve_resources(c.resources, self.kube_context), c)
                    for c in plan.containers)
        if wants:
            self.manifest['spec']['template']['spec']['runtimeClassName'] = \
                self._gpu_runtime_class

    def gpu_resources_requested(self) -> bool:
        """Whether any container of this campaign requests a GPU.

        Used to tell the Kueue pre-flight which resources the ClusterQueue must cover: an
        uncovered request is suspended forever rather than rejected, so it has to be caught
        before any job is created.
        """
        plan = getattr(self, "plan", None)
        if plan is None:
            return False
        return any(self._gpu_request(resolve_resources(c.resources, self.kube_context), c)
                   for c in plan.containers)

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
            # Sidecars are NATIVE sidecars now, so they report under
            # init_container_statuses; reading only container_statuses would see the
            # scenario container alone and pin every role to its digest.
            statuses = [cs for pod in pods
                        for cs in ((pod.status.container_statuses or [])
                                   + (pod.status.init_container_statuses or []))]
            digest = resolve_image_digest(statuses, self.image)
            # One digest per container, so a consumer can ask for the image a PARTICULAR
            # role ran. The single `image_revision` is the scenario container's, which is
            # the wrong answer for anything the simulator produced: the run view compiles
            # a run's geometry from the world the capture names, and that world and its
            # exporter live in the simulation image. Keyed on the container's own digest,
            # it was compiled -- or rather, failed to compile -- in the scenario image.
            per_role = {}
            for cs in statuses:
                name = getattr(cs, "name", None)
                pullable = pullable_digest(getattr(cs, "image_id", None))
                if name and pullable and name not in per_role:
                    per_role[name] = pullable
        except Exception as exc:  # noqa: BLE001 - never block the run on a status read
            logger.debug("Could not resolve SUT image digest for %s: %s",
                         self.campaign, exc)
            return
        if per_role:
            self._resolved_image_digests = per_role
        if digest:
            self._resolved_image_digest = digest
            logger.info("Pinned SUT image for %s to %s", self.campaign, digest)

    # -- in-pod execution ---------------------------------------------------

    def _verify_admission_path(self):
        """Fail the batch if Kueue cannot admit its jobs; warn if it cannot be checked.

        A missing read permission must not stop a campaign that would otherwise run —
        that would trade a rare hang for a common outage — so
        :class:`KueueCheckUnavailable` is downgraded to a warning naming what could not
        be read. Only a queue that is provably broken raises.
        """
        from .kubernetes_kueue import KueueCheckUnavailable, verify_kueue_admission_ready

        # A GPU request the ClusterQueue does not cover is not rejected by Kueue -- it is
        # suspended, permanently, while the Job reports active. Checked here so it costs one
        # error before any job exists rather than a whole sweep's worth of hung ones.
        required = (GPU_RESOURCE,) if self.gpu_resources_requested() else ()
        try:
            verify_kueue_admission_ready(namespace=self.namespace,
                                         kube_context=self.kube_context,
                                         required_resources=required)
        except KueueCheckUnavailable as exc:
            logger.warning("Batch %s: cannot verify the Kueue admission path (%s); "
                           "proceeding. If jobs never start, check that ClusterQueue "
                           "and LocalQueue exist.", self._batch_tag, exc)

    def _report_suspended_jobs(self, remaining):
        """Log why still-suspended jobs are waiting, and re-check the admission path.

        Kueue holds a Job suspended both for the normal reason (the queue is busy) and
        for terminal ones (no ClusterQueue, queue held, flavor missing). The two are
        indistinguishable from the Job alone, and Kueue's own wait message is not a
        stable enough API to tell them apart, so the structural re-check makes the
        fail-or-wait decision and the message is only ever logged.
        """
        suspended = []
        for job_name in remaining:
            try:
                job = self.k8s_batch_client.read_namespaced_job(name=job_name,
                                                                namespace=self.namespace)
            except client.exceptions.ApiException as exc:
                if exc.status == 404:
                    continue
                logger.debug("Could not read job %s for suspend check: %s", job_name, exc)
                continue
            if getattr(getattr(job, "spec", None), "suspend", False):
                suspended.append(job_name)
        if not suspended:
            return
        from .kubernetes_kueue import workload_wait_reasons
        reasons = workload_wait_reasons(self.namespace, job_names=suspended)
        detail = ("; ".join(sorted(set(reasons.values()))) if reasons
                  else "Kueue has not reported a reason")
        logger.warning("Batch %s: %d/%d job(s) suspended by Kueue, not yet admitted — "
                       "%s", self._batch_tag, len(suspended), len(remaining), detail)
        # Raises when the queue is structurally broken; a busy queue keeps waiting.
        self._verify_admission_path()

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
            prepare_campaign_configs(
                out_dir, self.campaign_data, cluster=True,
                instance_type_command=_instance_type_command(self.cluster_config))
            self._write_job_param_files(out_dir, campaign_root)

            # 2. Upload to the batch's storage prefix (job init containers mirror from here).
            n = storage.upload_dir(out_dir, bucket_name, campaign_prefix)
            logger.info("Batch %s: uploaded %d config file(s) to %s/%s",
                        self._batch_tag, n, bucket_name, campaign_prefix)

        # 3. Build and submit one Job per packed job, then wait.
        # Every job is labeled into the Kueue LocalQueue, so a broken admission path
        # does not fail the submit — it silently suspends all of them forever. Check
        # before creating anything, so the campaign dies here with the reason instead
        # of in the wait loop with none.
        self._verify_admission_path()
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
        last_suspend_check = time.monotonic()
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
                        f"Kubernetes reports: {reasons}. An image reason points at the "
                        f"execution image reference and pull credentials; an "
                        f"Unschedulable one at cluster capacity or a resource no node "
                        f"can satisfy (the message above names it).")
            elif blocked is not None:
                # A successful probe that found nothing blocked clears the timer.
                blocked_since = None
            # No grace period, deliberately: unlike a blocked pod, a restart has already
            # happened. The simulator lost its state, so every extra second spent waiting
            # buys a more convincing wrong answer rather than a chance of recovery.
            try:
                restarted = restarted_job_reasons(self.k8s_client, self.namespace,
                                                  job_label)
            except Exception as exc:  # noqa: BLE001 - probe failed this iteration
                logger.warning("Batch %s: could not check for restarted containers: %s",
                               self._batch_tag, exc)
                restarted = None
            if restarted:
                detail = "; ".join(f"{job}: {why}" for job, why in sorted(restarted.items()))
                raise CampaignConfigError(
                    f"{len(restarted)} scenario job(s) had a container restarted, which "
                    f"invalidates the trial — the simulator runs as a native sidecar, so "
                    f"the kubelet restarts it on a crash without failing the pod, and the "
                    f"scenario carries on against a simulator that lost all its state. "
                    f"{detail}")
            # blocked is None (probe failed) => leave blocked_since unchanged.
            # A Kueue-suspended Job has no pod at all, so the probe above cannot see it
            # and activeDeadlineSeconds never fires (its timer does not run while
            # suspended). Report it separately, and re-check the admission path so a
            # queue deleted or held *mid-campaign* fails the batch instead of hanging it.
            if time.monotonic() - last_suspend_check >= self._SUSPEND_CHECK_INTERVAL_SECONDS:
                last_suspend_check = time.monotonic()
                self._report_suspended_jobs(remaining)
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

        Run after the batch's results are downloaded, because the download can bring a
        manifest of its own. It accumulates through the same writer the upload side uses --
        one definition of "merge", so the two cannot drift into disagreeing about what the
        campaign's links are.
        """
        write_job_links_manifest(
            os.path.join(campaign_root, "_transient"), self._build_jobs(), self._batch_tag,
            base=read_job_links(campaign_root))


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
        from robovast.execution.backends import _scenario_image
        image = _scenario_image(execution_params, options)
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
            built_images=options.images,
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
                              image_digest=getattr(runner, "_resolved_image_digest", None),
                              image_digests=getattr(runner, "_resolved_image_digests", None))

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
        from . import in_pod_upload  # pylint: disable=import-outside-toplevel

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

        from . import in_pod_upload  # pylint: disable=import-outside-toplevel


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

    def count_run_artifacts(self, campaign_id: str,
                            campaign_root: str) -> int | None:
        """Count the per-run JUnit reports uploaded under the campaign's prefix.

        The object-store counterpart of :meth:`DockerBackend.count_run_artifacts`:
        each finished run uploads its own ``test.xml``, so counting them under the
        (flat, campaign-wide) prefix gives cumulative finished runs. The local
        ``campaign_root`` is not the source of truth here — results reach it only when
        a batch is downloaded — so it is unused.
        """
        del campaign_root
        bucket, prefix = in_pod_storage.campaign_storage_location(
            self.cluster_config, campaign_id)
        if self._progress_storage is None:
            self._progress_storage = in_pod_storage.storage_client_for(self.cluster_config)
        keys = self._progress_storage.list_keys(bucket, prefix)
        return sum(1 for k in keys if k.endswith(f"/{self.RUN_SENTINEL}"))
