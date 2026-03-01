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

"""Upload run archives from the cluster pod to a remote share service.

This module provides :class:`ShareUploader`, which:

1. Ensures a tar.gz archive exists in the archiver sidecar for each run
   (by running :mod:`~robovast.execution.cluster_execution.s3_to_targz`)
   exactly like ``cluster download`` does.
2. Executes the share-provider upload script inside the same archiver
   container, streaming progress back to the local terminal.
3. Keeps the tar.gz on the pod if the upload fails (so the user can retry
   or download normally), or removes it on success unless ``keep_archive``
   is requested.
"""

import logging
import subprocess
import sys
import threading

from .download_results import ResultDownloader
from .s3_client import ClusterS3Client
from .share_providers.base import BaseShareProvider

logger = logging.getLogger(__name__)

# Progress bar constants (match download_results.py / upload scripts)
CLEAR_LINE = "\033[2K"


class ShareUploader:
    """Upload run archives from the cluster pod to an external share service.

    Args:
        namespace: Kubernetes namespace where the robovast pod lives.
        cluster_config: Cluster configuration object (used for S3 credentials).
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
        self.namespace = namespace
        self.cluster_config = cluster_config
        self.context = context
        self.provider = provider

        # Reuse ResultDownloader for: pod check, run listing, archive creation
        self._downloader = ResultDownloader(
            namespace=namespace,
            cluster_config=cluster_config,
            context=context,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def upload_runs(
        self,
        force: bool = False,
        verbose: bool = False,
        keep_archive: bool = False,
        skip_removal: bool = False,
    ) -> int:
        """Create tar.gz archives on the pod then upload them to the share.

        Steps for each available run:

        1. Create the remote ``{run_id}.tar.gz`` archive in ``/data/`` (skip
           if it already exists and *force* is ``False``).
        2. Execute the provider's upload script inside the archiver container,
           streaming progress to the local terminal.
        3. On success: remove the remote archive from ``/data/`` unless
           *keep_archive* is ``True``.  Also delete the S3 bucket for the run
           unless *skip_removal* is ``True`` (mirrors ``cluster download``
           behavior).
        4. On failure: keep both the remote archive and the S3 bucket so the
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

        Returns:
            int: Number of runs successfully uploaded.
        """
        available_runs, excluded_runs = self._downloader.list_available_runs()

        if excluded_runs:
            for rid, running, pending in excluded_runs:
                logger.info(
                    "Run %s not ready (jobs still running: %d, pending: %d)",
                    rid,
                    running,
                    pending,
                )

        if not available_runs:
            if excluded_runs:
                logger.info(
                    "No runs ready to upload. Wait for jobs to finish and try again."
                )
            else:
                logger.info("No runs found to upload.")
            return 0

        logger.info(
            "Uploading %d run(s) to %s...",
            len(available_runs),
            self.provider.SHARE_TYPE,
        )

        uploaded = 0
        for run_id in available_runs:
            success = self._process_run(run_id, force=force, verbose=verbose)
            if success:
                if not keep_archive:
                    self._remove_remote_archive(run_id)
                if not skip_removal:
                    self._delete_run_s3(run_id)
                uploaded += 1

        return uploaded

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _process_run(self, run_id: str, force: bool, verbose: bool) -> bool:
        """Ensure archive exists, then upload. Returns True on success."""
        # Step 1: create/check the tar.gz archive
        archive_ok = self._downloader._create_remote_archive(  # pylint: disable=protected-access
            run_id, force=force, verbose=verbose
        )
        if not archive_ok:
            logger.error("Failed to create archive for run %s, skipping upload.", run_id)
            return False

        # Step 2: upload via the provider's pod-side script
        return self._upload_run(run_id, verbose=verbose)

    def _upload_run(self, run_id: str, verbose: bool) -> bool:
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
                "python", "-", run_id,
            ]
        )

        if verbose:
            logger.info("Uploading run %s via %s...", run_id, self.provider.SHARE_TYPE)

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
                    f"{run_id}  upload FAILED (exit code {proc.returncode})\n"
                )
                sys.stdout.flush()
                return False

            return True

        except Exception as exc:  # pylint: disable=broad-except
            sys.stdout.write(
                "\r" + CLEAR_LINE + f"{run_id}  upload FAILED: {exc}\n"
            )
            sys.stdout.flush()
            logger.debug("Upload exception for %s: %s", run_id, exc, exc_info=True)
            return False

    def _delete_run_s3(self, run_id: str) -> None:
        """Delete the S3 bucket for *run_id* after a successful upload."""
        logger.debug("Deleting S3 bucket %s...", run_id)
        try:
            if self.cluster_config:
                access_key, secret_key = self.cluster_config.get_s3_credentials()
            else:
                access_key, secret_key = "minioadmin", "minioadmin"
            with ClusterS3Client(
                namespace=self.namespace,
                access_key=access_key,
                secret_key=secret_key,
                context=self.context,
            ) as s3:
                s3.delete_bucket(run_id)
            logger.debug("Deleted S3 bucket %s", run_id)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Could not delete S3 bucket %s: %s", run_id, exc)

    def _remove_remote_archive(self, run_id: str) -> None:
        """Remove the tar.gz from /data/ in the archiver container."""
        archive_path = f"/data/{run_id}.tar.gz"
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
