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

"""Kubernetes launcher for Hydra.

Dispatches Hydra jobs to Kubernetes. 1 Hydra job = 1 pipeline run = 1 K8s
config (× execution.runs). All jobs are batched into ONE K8s submission.

Handles KeyboardInterrupt by cleaning up all K8s jobs in the active campaign.
"""

import logging
import signal
import sys
from pathlib import Path
from typing import Sequence

from omegaconf import DictConfig, OmegaConf

from robovast.pipeline.context import PipelineContext
from robovast.pipeline.executor import run_pipeline

logger = logging.getLogger(__name__)


class K8sLauncher:
    """Hydra launcher that dispatches jobs to Kubernetes.

    1 Hydra job = 1 pipeline run = 1 K8s config × execution.runs.
    All jobs are batched into ONE K8s submission.

    Args:
        cluster_config: Name of the cluster config (e.g., "minikube", "gcp").
        namespace: Kubernetes namespace.
        kube_context: Optional Kubernetes context override.
    """

    def __init__(
        self,
        cluster_config: str,
        namespace: str = "default",
        kube_context: str | None = None,
    ):
        self.cluster_config = cluster_config
        self.namespace = namespace
        self.kube_context = kube_context
        self._active_campaign_id: str | None = None
        self._job_runner = None

    def launch(
        self,
        configs_and_contexts: list[tuple[DictConfig, PipelineContext]],
        campaign_id: str,
        output_dir: Path,
        subdirs: list[str] | None = None,
        detached: bool = False,
    ) -> dict:
        """Submit all configs as ONE K8s batch and wait for completion.

        Args:
            configs_and_contexts: List of (resolved_cfg, pipeline_ctx) tuples.
                Each tuple represents one Hydra job = one set of pipeline params.
            campaign_id: Unique campaign identifier.
            output_dir: Directory for campaign output.
            subdirs: Per-job subdirectory names within output_dir (multirun).
            detached: If True, submit and return without waiting.

        Returns:
            Campaign results dict with statistics.

        Raises:
            KeyboardInterrupt: Re-raised after cleaning up K8s jobs.
        """
        from robovast.execution.cluster_execution.cluster_setup import (
            restore_cluster_config,
        )

        self._active_campaign_id = campaign_id

        # Restore cluster config for this context
        cluster_cfg = restore_cluster_config(self.kube_context)

        try:
            results = self._submit_and_wait(
                configs_and_contexts, campaign_id, output_dir, cluster_cfg, detached,
                subdirs=subdirs,
            )
            return results

        except KeyboardInterrupt:
            logger.info("Ctrl+C received, cleaning up K8s jobs for campaign '%s'...",
                        campaign_id)
            self._cleanup_campaign(campaign_id)
            logger.info("K8s jobs deleted for campaign '%s'.", campaign_id)
            raise

        finally:
            self._active_campaign_id = None

    def _submit_and_wait(
        self,
        configs_and_contexts,
        campaign_id,
        output_dir,
        cluster_cfg,
        detached,
        subdirs=None,
    ):
        """Internal: prepare manifests, submit, wait."""
        # This will integrate with the existing JobRunner infrastructure.
        # For now, define the interface; the full wiring to JobRunner's
        # manifest creation and S3 upload will be implemented when
        # the execution module is updated.
        raise NotImplementedError(
            "K8sLauncher._submit_and_wait: wiring to JobRunner pending. "
            "This will be implemented when the execution module is adapted "
            "to accept PipelineContext instead of .vast files."
        )

    def _cleanup_campaign(self, campaign_id: str):
        """Delete all K8s jobs and pods for the given campaign."""
        try:
            from robovast.execution.cluster_execution.kubernetes_kueue import (
                cleanup_cluster_campaign,
            )
            cleanup_cluster_campaign(
                campaign=campaign_id,
                namespace=self.namespace,
                kube_context=self.kube_context,
            )
        except Exception as e:
            logger.error("Failed to cleanup K8s campaign '%s': %s", campaign_id, e)
