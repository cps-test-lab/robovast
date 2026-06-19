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

Per batch it:

1. prepares the batch's config tree (reusing :func:`prepare_campaign_configs`
   and :class:`JobRunner`'s manifest building),
2. uploads it to the campaign's storage prefix (in-pod, via
   :mod:`.in_pod_storage` — no ``kubectl``/archiver),
3. creates one Kubernetes Job per packed job and waits for completion, then
4. downloads that batch's per-config/run results back into ``campaign_root``.

Each batch is isolated under a ``_batches/<batch_tag>/`` storage sub-prefix and
uses batch-namespaced job names, so batches of one search campaign never
collide. :class:`JobRunner`'s single-batch behaviour is unchanged (the batch
namespacing is gated on ``_batch_tag``).
"""

import logging
import os
import tempfile
import time

from robovast.common import prepare_campaign_configs
from robovast.common.execution import resolve_robovast_image
from robovast.execution.backends import ExecutionBackend, RunOptions

from . import in_pod_storage
from .cluster_execution import JobRunner

logger = logging.getLogger(__name__)


class _BatchJobRunner(JobRunner):
    """A :class:`JobRunner` driven by a pre-built ``campaign_data`` for one batch.

    Bypasses the config-path-driven ``__init__`` (the controller already composed
    ``campaign_data``) and runs in-pod: storage I/O is direct (no archiver) and
    the Kubernetes client uses the in-cluster service account.
    """

    @classmethod
    def for_batch(cls, *, campaign_data, campaign_id, batch_tag, runs, cluster_config,
                  namespace, image, kube_context=None, log_tree=False):
        self = cls.__new__(cls)
        self.cluster_config = cluster_config
        self.namespace = namespace
        # Used only for resolve_resources() (per-cluster resource lists); the
        # Kubernetes API client uses in-cluster config (see _ensure_k8s_initialized).
        self.kube_context = kube_context
        self.log_tree = log_tree
        self.config_path = None
        self.config_output_file_dir = None

        self.campaign = campaign_id
        self.campaign_data = campaign_data
        self.configs = campaign_data.get("configs", [])
        self.num_runs = runs
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
        self.run_start_time = None
        self.run_end_time = None
        self.campaign_start_time = None
        self.campaign_end_time = None
        self.job_statistics = {}
        return self

    def _ensure_k8s_initialized(self):
        """Initialise Kubernetes clients from the in-cluster service account."""
        if self._k8s_initialized:
            return
        from kubernetes import client  # pylint: disable=import-outside-toplevel
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

    def run_batch_in_pod(self, campaign_root: str):
        """Upload, run and download one batch; results land under *campaign_root*."""
        from kubernetes import client  # pylint: disable=import-outside-toplevel

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
        and turned into real symlinks by ``s3_to_targz`` / ``gcs_to_targz`` during
        ``upload-to-share``.
        """
        import yaml  # pylint: disable=import-outside-toplevel

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

    def run_batch(self, campaign_data: dict, *, campaign_root: str, batch_tag: str,
                  runs: int, options: RunOptions) -> None:
        campaign_id = os.path.basename(os.path.normpath(campaign_root))
        self._execution_params = campaign_data.get("execution", {}) or {}
        self._runs = runs
        image = resolve_robovast_image(
            explicit=options.image,
            config_image=self._execution_params.get("image"),
        )
        runner = _BatchJobRunner.for_batch(
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
