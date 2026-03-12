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

"""Upload campaign archives from the cluster pod to a remote share service.

Workflow
--------
For each available campaign:

1. **Compress** – run ``s3_to_targz.py`` inside the archiver sidecar to
   create ``/data/<campaign>.tar.gz`` on the pod.
2. **Upload** – execute the share-provider upload script inside the same
   archiver container, streaming progress back to the local terminal.
3. **Cleanup** – remove the remote archive on success (unless
   ``keep_archive`` is set) and delete the S3/GCS prefix (unless
   ``skip_removal`` is set).

On failure the archive and S3 data are kept so the user can retry.
"""

import logging
import subprocess
import sys
import threading

from kubernetes import client, config

from . import archiver, bucket_ops
from .cluster_execution import get_cluster_job_counts_per_campaign
from .share_providers.base import BaseShareProvider

logger = logging.getLogger(__name__)

CLEAR_LINE = "\033[2K"


def _filter_campaigns(campaign_ids, available_campaigns, excluded_runs):
    """Filter available/excluded campaign lists to the requested *campaign_ids*.

    Returns:
        (filtered_available, filtered_excluded), or (None, None) when any
        requested campaign is not found at all (after logging an error).
    """
    requested = list(campaign_ids)
    available_set = set(available_campaigns)
    excluded_map = {rid: (running, pending) for rid, running, pending in excluded_runs}

    filtered_available = []
    filtered_excluded = []
    not_found = []

    for cid in requested:
        if cid in available_set:
            filtered_available.append(cid)
        elif cid in excluded_map:
            running, pending = excluded_map[cid]
            logger.info(
                "Campaign %s is not ready yet (jobs still running: %d, pending: %d).",
                cid, running, pending,
            )
            filtered_excluded.append((cid, running, pending))
        else:
            not_found.append(cid)

    if not_found:
        all_known = sorted(available_set) + sorted(excluded_map)
        if all_known:
            logger.error(
                "Campaign(s) not found: %s\nAvailable campaigns:\n  %s",
                ", ".join(not_found),
                "\n  ".join(all_known),
            )
        else:
            logger.error(
                "Campaign(s) not found: %s. No campaigns available.",
                ", ".join(not_found),
            )
        return None, None

    return filtered_available, filtered_excluded


class ShareUploader:
    """Upload campaign archives from the cluster pod to an external share service.

    Args:
        namespace: Kubernetes namespace where the robovast pod lives.
        cluster_config: Cluster configuration object providing S3/GCS credentials.
        context: Kubernetes context name (or ``None`` for the active context).
        provider: Instantiated :class:`~.share_providers.base.BaseShareProvider`
            that supplies the upload script and pod environment.
    """

    def __init__(
        self,
        namespace: str = "default",
        cluster_config=None,
        context: str | None = None,
        provider: BaseShareProvider | None = None,
    ) -> None:
        if cluster_config is None:
            raise ValueError("cluster_config is required for ShareUploader.")

        self.namespace = namespace
        self.cluster_config = cluster_config
        self.context = context
        self.provider = provider

        # Verify the robovast pod (with archiver sidecar) is reachable.
        config.load_kube_config(context=context)
        self._k8s = client.CoreV1Api()
        self._check_pod()

    # ------------------------------------------------------------------
    # Pod health check
    # ------------------------------------------------------------------

    def _check_pod(self) -> None:
        """Exit with an error message if the robovast pod is not running."""
        try:
            pod = self._k8s.read_namespaced_pod(name="robovast", namespace=self.namespace)
            if pod.status.phase not in ("Running", "Pending"):
                logger.error(
                    "Pod 'robovast' exists but is not running (status: %s).",
                    pod.status.phase,
                )
                sys.exit(1)
            logger.debug("Found robovast pod (phase: %s)", pod.status.phase)
        except client.exceptions.ApiException as exc:
            if exc.status == 404:
                logger.error("Required pod 'robovast' does not exist.")
                sys.exit(1)
            raise

    # ------------------------------------------------------------------
    # Campaign listing
    # ------------------------------------------------------------------

    def list_available_campaigns(self):
        """List campaigns that are finished and ready for upload.

        Returns:
            tuple: (available_campaigns, excluded_runs) where *excluded_runs*
                is a list of ``(campaign_id, running_count, pending_count)``
                for campaigns that still have active jobs.
        """
        try:
            all_campaigns = bucket_ops.list_campaigns(
                self.cluster_config,
                namespace=self.namespace,
                context=self.context,
            )

            job_counts = get_cluster_job_counts_per_campaign(
                namespace=self.namespace, context=self.context
            )
            available, excluded = [], []
            for cid in all_campaigns:
                counts = job_counts.get(cid, {})
                running = counts.get("running", 0)
                pending = counts.get("pending", 0)
                if running == 0 and pending == 0:
                    available.append(cid)
                else:
                    excluded.append((cid, running, pending))

            if available:
                logger.debug("Available campaigns (finished): %s", available)
            return available, excluded

        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Unexpected error listing campaigns: %s", exc)
            return [], []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def upload_campaigns(
        self,
        force: bool = False,
        verbose: bool = False,
        keep_archive: bool = False,
        skip_removal: bool = False,
        campaign_ids: list | None = None,
    ) -> int:
        """Create tar.gz archives on the pod then upload them to the share.

        Steps for each available run:

        1. **Existence check** – if the archive already exists on the share and
           *force* is ``False``, skip both compression and upload (only for
           providers that implement
           :meth:`~.share_providers.base.BaseShareProvider.archive_exists_on_share`,
           e.g. WebDAV).
        2. Create the remote ``{campaign}.tar.gz`` archive in ``/data/`` (skip
           if it already exists and *force* is ``False``).
        3. Execute the provider's upload script inside the archiver container,
           streaming progress to the local terminal.
        4. On success: remove the remote archive from ``/data/`` unless
           *keep_archive* is ``True``.  Also delete the S3 bucket for the run
           unless *skip_removal* is ``True`` (mirrors ``cluster download``
           behavior).
        5. On failure: keep both the remote archive and the S3 bucket so the
           user can retry or fall back to ``cluster download``.

        Args:
            force: If ``True``, recreate the tar.gz archive even if it already
                exists in ``/data/``.
            verbose: If ``True``, emit detailed log messages instead of the
                single-line progress bar.
            keep_archive: If ``True``, do not remove the tar.gz from ``/data/``
                after a successful upload.
            skip_removal: If ``True``, do not delete the S3 bucket after a
                successful upload.
            campaign_ids: If provided, only upload these campaigns.

        Returns:
            int: Number of runs successfully uploaded.
        """
        available_campaigns, excluded_runs = self.list_available_campaigns()

        if campaign_ids is not None:
            available_campaigns, excluded_runs = _filter_campaigns(
                campaign_ids, available_campaigns, excluded_runs
            )
            if available_campaigns is None:
                return 0

        if excluded_runs:
            for rid, running, pending in excluded_runs:
                logger.info(
                    "Campaign %s not ready (jobs still running: %d, pending: %d).",
                    rid, running, pending,
                )

        if not available_campaigns:
            if excluded_runs:
                logger.info("No campaigns ready. Wait for jobs to finish and try again.")
            else:
                logger.info("No campaigns found.")
            return 0

        logger.info(
            "Uploading %d campaign(s) to %s...",
            len(available_campaigns),
            self.provider.SHARE_TYPE,
        )

        uploaded = 0
        for campaign in available_campaigns:
            archive_name = f"{campaign}.tar.gz"
            if not force and self.provider.archive_exists_on_share(archive_name):
                logger.info(
                    "Archive %s already exists on share, skipping compression and upload "
                    "(use --force to re-upload).",
                    archive_name,
                )
                continue

            script_path, env_vars, script_args = archiver.compress_args_for_config(
                self.cluster_config, campaign
            )
            archive_ok = archiver.compress_campaign(
                campaign,
                script_path,
                env_vars,
                script_args,
                namespace=self.namespace,
                context=self.context,
                force=force,
                verbose=verbose,
            )
            if not archive_ok:
                logger.error(
                    "Failed to create archive for %s, skipping upload.", campaign
                )
                continue

            success = self._upload_campaign(campaign, verbose=verbose)
            if success:
                if not keep_archive:
                    self._remove_remote_archive(campaign)
                if not skip_removal:
                    bucket_ops.delete_campaign(
                        campaign,
                        self.cluster_config,
                        namespace=self.namespace,
                        context=self.context,
                    )
                uploaded += 1
            else:
                logger.error(
                    "Upload failed for %s. Archive and S3 data kept for retry.", campaign
                )

        return uploaded

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------

    def _upload_campaign(self, campaign: str, verbose: bool) -> bool:
        """Execute the provider upload script inside the archiver container."""
        script_path = self.provider.get_upload_script_path()
        try:
            with open(script_path, encoding="utf-8") as fh:
                script_content = fh.read()
        except OSError as exc:
            logger.error(
                "Cannot read upload script %s: %s", script_path, exc
            )
            return False

        # Prepend env var assignments so the script can read os.environ.
        # Using repr() ensures all special characters (quotes, newlines, etc.)
        # are safely encoded without any shell involvement.
        env_header_lines = ["import os"]
        for key, value in self.provider.build_pod_env().items():
            env_header_lines.append(f"os.environ[{key!r}] = {value!r}")
        combined_script = "\n".join(env_header_lines) + "\n\n" + script_content

        ctx_args = ["--context", self.context] if self.context else []
        cmd = (
            ["kubectl"]
            + ctx_args
            + [
                "exec", "-i", "-n", self.namespace, "robovast",
                "-c", "archiver",
                "--",
                "python", "-", campaign,
            ]
        )

        if verbose:
            logger.info("Uploading campaign %s via %s...", campaign, self.provider.SHARE_TYPE)

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            # Stream stdout from pod back to our terminal in a background thread
            # so the progress bar (with \r overwriting) renders correctly.
            def _relay_stdout():
                while True:
                    chunk = proc.stdout.read(512)
                    if not chunk:
                        break
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()

            def _relay_stderr():
                while True:
                    chunk = proc.stderr.read(512)
                    if not chunk:
                        break
                    sys.stderr.buffer.write(chunk)
                    sys.stderr.buffer.flush()

            t_out = threading.Thread(target=_relay_stdout, daemon=True)
            t_err = threading.Thread(target=_relay_stderr, daemon=True)
            t_out.start()
            t_err.start()

            proc.stdin.write(combined_script.encode("utf-8"))
            proc.stdin.close()

            t_out.join()
            t_err.join()
            proc.wait()

            if proc.returncode != 0:
                sys.stdout.write(
                    "\r" + CLEAR_LINE +
                    f"{campaign}  upload FAILED (exit code {proc.returncode})\n"
                )
                sys.stdout.flush()
                return False

            return True

        except Exception as exc:  # pylint: disable=broad-except
            sys.stdout.write(
                "\r" + CLEAR_LINE + f"{campaign}  upload FAILED: {exc}\n"
            )
            sys.stdout.flush()
            logger.debug("Upload exception for %s: %s", campaign, exc, exc_info=True)
            return False

    def _remove_remote_archive(self, campaign_id: str) -> None:
        """Remove the tar.gz from /data/ in the archiver container."""
        archive_path = f"/data/{campaign_id}.tar.gz"
        ctx_args = ["--context", self.context] if self.context else []
        try:
            subprocess.run(
                ["kubectl"]
                + ctx_args
                + [
                    "exec", "-n", self.namespace, "robovast",
                    "-c", "archiver",
                    "--",
                    "rm", "-f", archive_path,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            logger.debug("Removed remote archive %s", archive_path)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Could not remove remote archive %s: %s", archive_path, exc)
