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

"""Compress + upload a campaign to a share, in-process in the controller pod.

This replaces the host-driven ``upload-to-share`` flow that used to ``kubectl
exec`` into an archiver sidecar. The controller pod already reaches the campaign
storage in-cluster, so it compresses and uploads itself — no second pod, no
``kubectl``. Compression is **cluster-specific** and owned by the cluster config
(:meth:`~robovast.execution.cluster_config.base_config.BaseConfig.compress_campaign`);
this module stays generic and just orchestrates compress → upload → retry.

Share credentials are injected into the controller pod at launch (resolved from
the host ``.env`` by :mod:`.controller_launcher`), so they are already present in
``os.environ`` here. :func:`load_provider_from_env` reads them (with optional
overrides supplied by a retrigger command).
"""

import logging
import os

logger = logging.getLogger(__name__)

#: Directory the tar.gz is written to / read from inside the controller pod.
#: The launcher injects ``ROBOVAST_ARCHIVE_DIR``; default to a workspace path.
_DEFAULT_ARCHIVE_DIR = "/workspace/archive"


def _archive_dir() -> str:
    return os.environ.get("ROBOVAST_ARCHIVE_DIR") or _DEFAULT_ARCHIVE_DIR


def share_type_configured() -> bool:
    """Return ``True`` if a share provider is configured in the environment."""
    return bool(os.environ.get("ROBOVAST_SHARE_TYPE", "").strip())


def load_provider_from_env(overrides: dict | None = None):
    """Instantiate the configured share provider from the environment.

    Args:
        overrides: Optional ``{ENV_VAR: value}`` applied to ``os.environ`` before
            the provider is built — used by the retrigger command to supply
            corrected credentials without relaunching the controller.

    Returns:
        An instantiated :class:`~.share_providers.base.BaseShareProvider`, or
        ``None`` when ``ROBOVAST_SHARE_TYPE`` is unset.

    Raises:
        ValueError: when the configured share type has no registered provider.
        click.UsageError: when required provider env vars are missing (raised by
            the provider constructor).
    """
    from .share_providers import \
        load_share_provider_plugins  # pylint: disable=import-outside-toplevel

    if overrides:
        for key, value in overrides.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)

    share_type = os.environ.get("ROBOVAST_SHARE_TYPE", "").strip()
    if not share_type:
        return None

    providers = load_share_provider_plugins()
    if share_type not in providers:
        available = ", ".join(sorted(providers)) or "(none installed)"
        raise ValueError(
            f"Unknown share type '{share_type}'. Available providers: {available}"
        )
    return providers[share_type]()


def verify_share_access(provider) -> None:
    """Run the provider's pre-flight credential check (raises on failure)."""
    logger.info("Verifying share credentials (%s) before starting the campaign...",
                provider.SHARE_TYPE)
    provider.verify_access()
    logger.info("Share credentials OK.")


def upload_campaign(cluster_config, campaign_id: str, provider,
                    progress_cb=None) -> bool:
    """Compress *campaign_id* from storage and upload it via *provider*.

    1. Compress: ``cluster_config.compress_campaign`` (storage-specific — S3 vs
       GCS lives in the cluster config) writes
       ``$ROBOVAST_ARCHIVE_DIR/<campaign>.tar.gz``.
    2. Upload: call the provider's in-process ``upload_archive``. The provider's
       resolved env (URLs, tokens, key JSON/PEM) is already in ``os.environ`` —
       injected at launch (and possibly overridden by a retrigger).
    3. Remove the local archive on success.

    Args:
        progress_cb: Optional ``(bytes_sent, total_bytes)`` callable forwarded to
            the provider so the controller can publish upload progress.

    Returns ``True`` on success; logs and returns ``False`` on any failure (so
    the controller can keep the pod alive for a retrigger).
    """
    archive_dir = _archive_dir()
    os.makedirs(archive_dir, exist_ok=True)

    # 1. Compress straight from storage into the local archive dir. The cluster
    #    config owns the storage-specific compression.
    logger.info("Compressing campaign %s for upload...", campaign_id)
    try:
        archive_path = cluster_config.compress_campaign(campaign_id, archive_dir)
    except Exception:  # pylint: disable=broad-except
        logger.exception("Campaign compression failed.")
        return False

    # 2. Upload the archive in-process via the configured share provider.
    object_name = os.path.basename(archive_path)
    logger.info("Uploading %s to %s...", object_name, provider.SHARE_TYPE)
    ok = True
    try:
        provider.upload_archive(archive_path, object_name, progress_callback=progress_cb)
    except Exception:  # pylint: disable=broad-except
        logger.exception("Upload to %s failed.", provider.SHARE_TYPE)
        ok = False

    # 3. Best-effort cleanup of the local archive (storage keeps the canonical copy).
    if ok:
        try:
            if os.path.isfile(archive_path):
                os.remove(archive_path)
        except OSError:
            logger.debug("Could not remove local archive %s", archive_path, exc_info=True)

    return ok
