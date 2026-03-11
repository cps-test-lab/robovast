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
"""kubectl orchestration for the robovast archiver sidecar.

Pure functions — no classes, no storage-SDK dependencies.  All storage
communication happens inside the pod; the host only needs ``kubectl``.

Two public helpers:

* :func:`compress_campaign` — run ``s3_to_targz.py`` or ``gcs_to_targz.py``
  inside the archiver to create ``/data/<campaign>.tar.gz``.
* :func:`upload_configs` — tar a local config directory, ``kubectl cp`` it
  to the archiver, then run ``targz_to_s3.py`` or ``targz_to_gcs.py`` to
  upload into the storage backend.
"""

import logging
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
import time

logger = logging.getLogger(__name__)

CLEAR_LINE = "\033[2K"


# ---------------------------------------------------------------------------
# Internal utilities (also exported for use in generated scripts)
# ---------------------------------------------------------------------------

def _format_size(num_bytes: int) -> str:
    """Return a human-readable file size string."""
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


def _create_config_targz(config_dir: str, targz_path: str) -> int:
    """Create a gzipped tar archive of *config_dir* at *targz_path*.

    Returns:
        int: Number of files archived.
    """
    file_count = 0
    with open(targz_path, "wb") as fh:
        with tarfile.open(fileobj=fh, mode="w:gz") as tar:
            for root, _dirs, files in os.walk(config_dir):
                for filename in files:
                    local_path = os.path.join(root, filename)
                    arcname = os.path.relpath(local_path, config_dir).replace(os.sep, "/")
                    tar.add(local_path, arcname=arcname)
                    file_count += 1
    return file_count


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compress_campaign(
    campaign_id: str,
    script_path: str,
    env_vars: dict,
    script_args: list,
    namespace: str = "default",
    context: str | None = None,
    *,
    force: bool = False,
    verbose: bool = False,
) -> bool:
    """Run a compress script inside the archiver sidecar.

    Creates ``/data/<campaign_id>.tar.gz`` on the archiver pod by piping
    *script_path* (with *env_vars* injected) via ``kubectl exec -i``.

    Handles the unfinished-flag pattern: writes
    ``/data/<campaign_id>.tar.gz_unfinished`` before starting and removes
    it on success.  An existing unfinished flag means the previous attempt
    was interrupted — the partial archive is removed and compression restarts.

    Args:
        campaign_id:  Campaign identifier used as the archive name.
        script_path:  Absolute path to ``s3_to_targz.py`` or ``gcs_to_targz.py``.
        env_vars:     Dict of env-var overrides injected at the top of the script.
        script_args:  Positional arguments passed to the script (after ``python -``).
        namespace:    Kubernetes namespace.
        context:      Kubernetes context (or ``None`` for the active context).
        force:        Recreate archive even if it already exists.
        verbose:      Emit detailed log messages instead of single-line progress.

    Returns:
        bool: ``True`` if the archive exists or was created successfully.
    """
    archive_name = f"{campaign_id}.tar.gz"
    remote_archive_path = f"/data/{archive_name}"
    unfinished_flag_path = f"{remote_archive_path}_unfinished"
    ctx_args = ["--context", context] if context else []

    def _kubectl_test(path: str) -> bool:
        return subprocess.run(
            ["kubectl"] + ctx_args + [
                "exec", "-n", namespace, "robovast",
                "-c", "archiver", "--", "test", "-f", path,
            ],
            capture_output=True, text=True, check=False,
        ).returncode == 0

    def _kubectl_rm(*paths: str) -> None:
        for path in paths:
            subprocess.run(
                ["kubectl"] + ctx_args + [
                    "exec", "-n", namespace, "robovast",
                    "-c", "archiver", "--", "rm", "-f", path,
                ],
                capture_output=True, text=True, check=False,
            )

    archive_exists = _kubectl_test(remote_archive_path)

    if archive_exists:
        flag_exists = _kubectl_test(unfinished_flag_path)
        if flag_exists:
            logger.info(
                "Campaign %s: incomplete archive found (unfinished flag), recreating…",
                campaign_id,
            )
            _kubectl_rm(remote_archive_path, unfinished_flag_path)
            archive_exists = False
        elif not force:
            if verbose:
                logger.info("Campaign %s: archive already exists, skipping.", campaign_id)
            else:
                sys.stdout.write(
                    "\r" + CLEAR_LINE + f"{campaign_id}  skipped (archive exists)\n"
                )
                sys.stdout.flush()
            return True
        else:
            _kubectl_rm(remote_archive_path)
            archive_exists = False

    try:
        if not verbose:
            sys.stdout.write(
                "\r" + CLEAR_LINE + f"{campaign_id}  compressing…"
            )
            sys.stdout.flush()
        logger.debug("Compressing campaign %s via archiver…", campaign_id)

        # Mark as in progress.
        subprocess.run(
            ["kubectl"] + ctx_args + [
                "exec", "-n", namespace, "robovast",
                "-c", "archiver", "--", "touch", unfinished_flag_path,
            ],
            capture_output=True, text=True, check=False,
        )

        with open(script_path, encoding="utf-8") as fh:
            script_content = fh.read()

        env_lines = ["import os"] + [
            f"os.environ[{k!r}] = {v!r}" for k, v in env_vars.items()
        ]
        combined_script = "\n".join(env_lines) + "\n\n" + script_content

        subprocess.run(
            ["kubectl"] + ctx_args + [
                "exec", "-i", "-n", namespace, "robovast",
                "-c", "archiver", "--",
                "python", "-",
            ] + script_args,
            input=combined_script,
            capture_output=True,
            text=True,
            check=True,
        )

        _kubectl_rm(unfinished_flag_path)

        if verbose:
            logger.info("Created archive at %s", remote_archive_path)
        else:
            sys.stdout.write(
                "\r" + CLEAR_LINE + f"{campaign_id}  compressed to {archive_name}\n"
            )
            sys.stdout.flush()
        return True

    except subprocess.CalledProcessError as exc:
        logger.error("Failed to create archive for campaign %s: %s", campaign_id, exc)
        if exc.stderr:
            logger.error("Archiver stderr: %s", exc.stderr.strip())
        return False


def upload_configs(
    config_dir: str,
    campaign_id: str,
    bucket: str,
    script_path: str,
    env_vars: dict,
    namespace: str = "default",
    context: str | None = None,
    *,
    prefix: str | None = None,
) -> None:
    """Upload *config_dir* to the storage backend via the archiver sidecar.

    Steps:
    1. Create a local ``tar.gz`` from *config_dir*.
    2. ``kubectl cp`` the archive to ``/data/_upload_<campaign_id>.tar.gz``.
    3. ``kubectl cp`` *script_path* (with *env_vars* injected) to
       ``/data/_upload_script.py``.
    4. ``kubectl exec`` the script: ``python /data/_upload_script.py <bucket>
       /data/_upload_<campaign_id>.tar.gz [--prefix <prefix>]``.
    5. Remove both temporary files from the archiver.

    Args:
        config_dir:   Local directory of generated config files.
        campaign_id:  Campaign identifier (used to name temporary files).
        bucket:       Target bucket (or shared-bucket name for GCS/external S3).
        script_path:  Absolute path to ``targz_to_s3.py`` or ``targz_to_gcs.py``.
        env_vars:     Dict of env-var overrides injected at the top of the script.
        namespace:    Kubernetes namespace.
        context:      Kubernetes context (or ``None`` for the active context).
        prefix:       Key prefix inside the bucket (shared-bucket mode).

    Raises:
        FileNotFoundError: *config_dir* does not exist.
        RuntimeError:      ``kubectl cp`` or ``kubectl exec`` failed.
    """
    if not os.path.isdir(config_dir):
        raise FileNotFoundError(f"Config directory does not exist: {config_dir}")

    ctx_args = ["--context", context] if context else []

    with tempfile.TemporaryDirectory() as tmp:
        targz_path = os.path.join(tmp, "configs.tar.gz")

        # Step 1: Create tar.gz locally.
        sys.stdout.write("Creating archive...")
        sys.stdout.flush()
        file_count = _create_config_targz(config_dir, targz_path)
        size_str = _format_size(os.path.getsize(targz_path))
        sys.stdout.write(f"\r{CLEAR_LINE}Created archive ({file_count} files, {size_str})\n")
        sys.stdout.flush()

        # Step 2: Copy tar.gz to archiver container.
        remote_path = f"/data/_upload_{campaign_id}.tar.gz"
        dest = f"{namespace}/robovast:{remote_path}"
        cp_cmd = ["kubectl"] + ctx_args + ["cp", targz_path, dest, "-c", "archiver"]

        sys.stdout.write("Transferring archive to cluster...")
        sys.stdout.flush()
        start_time = time.time()
        result = subprocess.run(cp_cmd, capture_output=True, text=True, check=False)
        elapsed = time.time() - start_time
        if result.returncode != 0:
            sys.stdout.write(f"\r{CLEAR_LINE}Transfer failed\n")
            sys.stdout.flush()
            raise RuntimeError(
                f"Failed to copy archive to archiver: {result.stderr.strip()}"
            )
        rate = os.path.getsize(targz_path) / elapsed if elapsed > 0 else 0
        sys.stdout.write(
            f"\r{CLEAR_LINE}Transferred archive to cluster ({size_str}, {_format_size(rate)}/s)\n"
        )
        sys.stdout.flush()

        # Step 3: Build script with injected env vars and copy to archiver.
        with open(script_path, encoding="utf-8") as fh:
            script_content = fh.read()
        env_lines = ["import os"] + [
            f"os.environ[{k!r}] = {v!r}" for k, v in env_vars.items()
        ]
        file_script = "\n".join(env_lines) + "\n\n" + script_content

        remote_script = "/data/_upload_script.py"
        local_script = os.path.join(tmp, "_upload_script.py")
        with open(local_script, "w", encoding="utf-8") as fh:
            fh.write(file_script)

        script_dest = f"{namespace}/robovast:{remote_script}"
        cp_script_cmd = (
            ["kubectl"] + ctx_args + ["cp", local_script, script_dest, "-c", "archiver"]
        )
        result = subprocess.run(cp_script_cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to copy upload script to archiver: {result.stderr.strip()}"
            )

        # Step 4: Run the script.
        exec_args = [bucket, remote_path]
        if prefix:
            exec_args += ["--prefix", prefix]
        exec_cmd = (
            ["kubectl"] + ctx_args + [
                "exec", "-n", namespace, "robovast",
                "-c", "archiver",
                "--",
                "python", remote_script,
            ] + exec_args
        )

        sys.stdout.write("Uploading (internal)...")
        sys.stdout.flush()
        start_time = time.time()

        proc = subprocess.Popen(
            exec_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        stderr_chunks: list = []
        stdout_chunks: list = []

        def _relay_stderr():
            while True:
                chunk = proc.stderr.read(256)
                if not chunk:
                    break
                decoded = chunk.decode("utf-8", errors="replace")
                stderr_chunks.append(decoded)
                sys.stdout.write(f"\r{CLEAR_LINE}{decoded.rstrip()}")
                sys.stdout.flush()

        def _drain_stdout():
            stdout_chunks.append(proc.stdout.read())

        t_err = threading.Thread(target=_relay_stderr, daemon=True)
        t_out = threading.Thread(target=_drain_stdout, daemon=True)
        t_err.start()
        t_out.start()
        proc.wait()
        t_err.join(timeout=5)
        t_out.join(timeout=5)

        elapsed = time.time() - start_time

        if proc.returncode != 0:
            sys.stdout.write(
                f"\r{CLEAR_LINE}Upload failed (exit code {proc.returncode})\n"
            )
            sys.stdout.flush()
            logger.error("Upload script failed: %s", "".join(stderr_chunks))
            raise RuntimeError("Failed to upload configs via archiver")

        stdout_text = b"".join(stdout_chunks).decode("utf-8", errors="replace").strip()
        sys.stdout.write(
            f"\r{CLEAR_LINE}Uploaded {stdout_text or file_count} files ({elapsed:.1f}s)\n"
        )
        sys.stdout.flush()

        # Step 5: Clean up temporary files on archiver.
        rm_cmd = (
            ["kubectl"] + ctx_args + [
                "exec", "-n", namespace, "robovast",
                "-c", "archiver",
                "--",
                "rm", "-f", remote_path, remote_script,
            ]
        )
        subprocess.run(rm_cmd, capture_output=True, check=False)


# ---------------------------------------------------------------------------
# Helpers used by cluster_execution and upload_to_share to build the right
# script_path / env_vars / script_args for each backend.
# ---------------------------------------------------------------------------

def compress_args_for_config(cluster_config, campaign_id: str) -> tuple:
    """Return ``(script_path, env_vars, script_args)`` for :func:`compress_campaign`.

    Selects ``s3_to_targz.py`` or ``gcs_to_targz.py`` based on the cluster
    config's storage backend.

    Returns:
        tuple: (script_path: str, env_vars: dict, script_args: list)
    """
    _dir = os.path.dirname(os.path.abspath(__file__))
    if cluster_config.get_storage_backend() == "gcs":
        script_path = os.path.join(_dir, "gcs_to_targz.py")
        env_vars = {
            "ROBOVAST_GCS_BUCKET": cluster_config.get_s3_bucket(),
            "ROBOVAST_GCS_KEY_JSON": cluster_config.get_gcs_key_json(),
        }
        script_args = [campaign_id]
    else:
        script_path = os.path.join(_dir, "s3_to_targz.py")
        access_key, secret_key = cluster_config.get_s3_credentials()
        env_vars = {
            "S3_ACCESS_KEY": access_key,
            "S3_SECRET_KEY": secret_key,
        }
        endpoint = (
            cluster_config.get_s3_endpoint()
            if not cluster_config.uses_embedded_s3()
            else None
        )
        if endpoint:
            env_vars["S3_ENDPOINT"] = endpoint
        shared_bucket = cluster_config.get_s3_bucket()
        if shared_bucket:
            script_args = [
                shared_bucket,
                "--prefix", f"{campaign_id}/",
                "--archive-name", campaign_id,
            ]
        else:
            # Per-bucket mode: campaign_id is the bucket name.
            script_args = [campaign_id]
    return script_path, env_vars, script_args


def upload_args_for_config(cluster_config, campaign_id: str) -> tuple:
    """Return ``(script_path, env_vars, bucket, prefix)`` for :func:`upload_configs`.

    Selects ``targz_to_s3.py`` or ``targz_to_gcs.py`` based on the cluster
    config's storage backend.

    Returns:
        tuple: (script_path: str, env_vars: dict, bucket: str, prefix: str | None)
    """
    _dir = os.path.dirname(os.path.abspath(__file__))
    bucket_name = campaign_id.lower().replace("_", "-")
    if cluster_config.get_storage_backend() == "gcs":
        script_path = os.path.join(_dir, "targz_to_gcs.py")
        env_vars = {
            "ROBOVAST_GCS_KEY_JSON": cluster_config.get_gcs_key_json(),
        }
        bucket = cluster_config.get_s3_bucket()
        prefix = bucket_name
    else:
        script_path = os.path.join(_dir, "targz_to_s3.py")
        access_key, secret_key = cluster_config.get_s3_credentials()
        s3_region = cluster_config.get_s3_region()
        env_vars = {
            "S3_ACCESS_KEY": access_key,
            "S3_SECRET_KEY": secret_key,
            "S3_REGION": s3_region,
        }
        endpoint = (
            cluster_config.get_s3_endpoint()
            if not cluster_config.uses_embedded_s3()
            else None
        )
        if endpoint:
            env_vars["S3_ENDPOINT"] = endpoint
        shared_bucket = cluster_config.get_s3_bucket()
        bucket = shared_bucket if shared_bucket else bucket_name
        prefix = bucket_name if shared_bucket else None
    return script_path, env_vars, bucket, prefix
