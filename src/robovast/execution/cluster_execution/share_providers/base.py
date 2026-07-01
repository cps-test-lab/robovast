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

"""Abstract base class for share providers."""

import logging
import os
from abc import ABC, abstractmethod

import click

logger = logging.getLogger(__name__)

__all__ = ["BaseShareProvider", "UploadProgressReader"]


class UploadProgressReader:
    """Wrap a binary file object, reporting progress to a callback as it is read.

    Pass an instance as the request body to ``urllib``/``requests``: each
    ``read`` advances the byte count and invokes ``progress_callback(sent,
    total)`` — the same ``(bytes_sent, total_bytes)`` shape used by the
    ``download_archive`` callbacks, so the controller can render upload and
    download progress identically.

    ``start_offset`` accounts for bytes already present on the remote (resumed
    uploads): the callback reports the cumulative ``sent`` (offset + bytes read
    this session) against ``total``, while :meth:`__len__` returns only the
    remaining bytes so the HTTP client sets the correct ``Content-Length``.
    """

    CHUNK = 256 * 1024  # 256 KiB

    def __init__(self, fh, total, progress_callback=None, start_offset=0):
        self._fh = fh
        self._total = total
        self._sent = start_offset
        self._to_send = total - start_offset
        self._cb = progress_callback

    def read(self, n=-1):  # called by urllib/requests internals
        data = self._fh.read(self.CHUNK if n == -1 else n)
        self._sent += len(data)
        if self._cb is not None and self._total > 0:
            self._cb(self._sent, self._total)
        return data

    def __len__(self):
        # urllib/requests use this to set Content-Length; only the bytes that
        # will actually be streamed in this session (excludes a resume offset).
        return self._to_send


class BaseShareProvider(ABC):
    """Base class for all share providers.

    A share provider encapsulates everything needed to upload a tar.gz archive
    to a remote storage service (Nextcloud, Google Drive, …) and to download it
    back. Both directions run **in-process** in the controller pod (no archiver
    sidecar, no ``kubectl exec``): see
    :mod:`robovast.execution.cluster_execution.in_pod_upload`.

    Subclasses must:

    * Set :attr:`SHARE_TYPE` to the provider name (matching the entry-point key).
    * Declare all required environment variables in :meth:`required_env_vars`.
    * Implement :meth:`upload_archive` (the in-process transfer).
    * Provide environment variables for the controller pod via
      :meth:`build_pod_env` (the launcher resolves host credentials — e.g. a key
      *file* into inline JSON/PEM — and injects them into the pod environment).

    The constructor automatically validates that all required env vars are
    present; it raises :class:`click.UsageError` if any are missing.  Values
    are read from ``os.environ`` (which is already populated by
    ``python-dotenv`` before the provider is instantiated).

    To add a **new provider**:

    1. Create a new file in this package (e.g. ``myshare.py``).
    2. Subclass :class:`BaseShareProvider`, fill in the abstract members.
    3. Register the provider in ``pyproject.toml`` under
       ``[tool.poetry.plugins."robovast.share_providers"]``.
    """

    #: Short identifier for the provider, e.g. ``"nextcloud"`` .
    SHARE_TYPE: str = ""

    def __init__(self) -> None:
        self._validate_env()

    def _validate_env(self) -> None:
        """Raise :class:`click.UsageError` if any required env vars are absent."""
        missing = {
            var: desc
            for var, desc in self.required_env_vars().items()
            if not os.environ.get(var)
        }
        if missing:
            lines = [
                f"Missing environment variable(s) required for share type "
                f"'{self.SHARE_TYPE}':",
            ]
            for var, desc in missing.items():
                lines.append(f"  {var}  — {desc}")
            lines.append(
                "\nSet these variables in a .env file in your project directory."
            )
            raise click.UsageError("\n".join(lines))

    @abstractmethod
    def required_env_vars(self) -> dict[str, str]:
        """Return a mapping of environment-variable name → human-readable description.

        All listed variables must be non-empty strings in the environment when
        the provider is instantiated.  The base class validates them
        automatically and raises :class:`click.UsageError` if any are missing.

        Example::

            return {
                "ROBOVAST_SHARE_URL": "Public share URL of the target folder",
            }
        """

    @abstractmethod
    def upload_archive(
        self,
        archive_path: str,
        object_name: str,
        progress_callback=None,
    ) -> None:
        """Upload the local *archive_path* to the share as *object_name*.

        Runs in-process in the controller pod. Credentials and target settings
        are read from ``os.environ`` (populated by :meth:`build_pod_env` at
        launch). Implementations should be resumable where the backend allows it.

        Args:
            archive_path: Absolute path to the local ``<campaign>.tar.gz``.
            object_name: Destination object/file name on the share (the archive
                basename, optionally with a provider prefix).
            progress_callback: Optional ``(bytes_sent, total_bytes)`` callable,
                invoked periodically during the transfer — the same shape as the
                :meth:`download_archive` callback. Use :class:`UploadProgressReader`
                to drive it from a streamed request body.

        Raise on failure (the caller treats any exception as a failed upload and
        keeps the controller alive for a retrigger).
        """

    @abstractmethod
    def build_pod_env(self) -> dict[str, str]:
        """Return environment variables to inject into the pod exec call.

        These variables will be set for the upload script executed inside the
        archiver container.  Include everything the script needs: URLs, tokens,
        credentials, etc.

        The return value is merged into the pod's environment via the
        ``--env`` flag of ``kubectl exec``.

        Returns:
            dict[str, str]: Mapping of variable name to value.
        """

    # ------------------------------------------------------------------
    # Pre-flight credential check (used by the controller before a campaign)
    # ------------------------------------------------------------------

    def verify_access(self) -> None:
        """Verify the share is reachable with the configured credentials.

        Called by the in-cluster controller **before** any batches start, so a
        campaign that could never be delivered fails fast instead of wasting
        compute. Implementations should perform the cheapest authenticated
        operation that proves write access (a HEAD/PROPFIND, a token exchange,
        an SFTP ``stat``) and raise on failure.

        The default is a non-blocking warning: providers that cannot cheaply
        check access do not gate the campaign.
        """
        logger.warning(
            "Share provider '%s' does not implement a pre-flight credential "
            "check; skipping verification.", self.SHARE_TYPE,
        )

    # ------------------------------------------------------------------
    # Optional download interface (used by ``results download``)
    # ------------------------------------------------------------------

    def list_campaign_archives(self) -> list[str]:
        """Return a list of campaign ``*.tar.gz`` object names on the share.

        Archives whose base name (without ``.tar.gz``) matches the campaign
        naming convention (``<campaign-name>-YYYY-MM-DD-HHMMSS``) are returned.

        Raise :class:`NotImplementedError` if the provider does not support
        downloading (default).  Implementations should return bare object names
        (keys), not full URLs.

        The default implementation delegates to
        :meth:`list_campaign_archives_with_size` and discards the size.
        Override :meth:`list_campaign_archives_with_size` to provide sizes.
        """
        return [name for name, _ in self.list_campaign_archives_with_size()]

    def list_campaign_archives_with_size(self) -> list[tuple[str, int]]:
        """Return a list of ``(object_name, size_in_bytes)`` for each
        ``campaign-*.tar.gz`` object on the share.

        *size_in_bytes* is ``-1`` when the provider cannot determine the file
        size.  Raise :class:`NotImplementedError` if the provider does not
        support listing at all (default).

        Implementations should return bare object names (keys), not full URLs.
        """
        raise NotImplementedError(
            f"Provider '{self.SHARE_TYPE}' does not support 'results list-share'."
        )

    def download_archive(
        self,
        object_name: str,
        dest_path: str,
        progress_callback=None,
        resume_offset: int = 0,
    ) -> None:
        """Download *object_name* from the share to the local *dest_path*.

        Args:
            object_name: The object/file name on the share (as returned by
                :meth:`list_campaign_archives`).
            dest_path: Absolute local path to write the downloaded file to.
            progress_callback: Optional callable ``(bytes_received, total_bytes)``
                called periodically during the download.
            resume_offset: Byte offset to resume downloading from.  When
                non-zero the provider should skip the first *resume_offset*
                bytes and **append** to *dest_path*.

        Raise :class:`NotImplementedError` if the provider does not support
        downloading (default).
        """
        _ = object_name, dest_path, progress_callback, resume_offset
        raise NotImplementedError(
            f"Provider '{self.SHARE_TYPE}' does not support 'results download'."
        )

    # ------------------------------------------------------------------
    # Optional remove interface (used by ``results remove-from-share``)
    # ------------------------------------------------------------------

    def remove_archive(self, object_name: str) -> None:
        """Remove *object_name* from the share.

        Args:
            object_name: The object/file name on the share (as returned by
                :meth:`list_campaign_archives`).

        Raise :class:`NotImplementedError` if the provider does not support
        removal (default).
        """
        _ = object_name
        raise NotImplementedError(
            f"Provider '{self.SHARE_TYPE}' does not support 'results remove-from-share'."
        )

    # ------------------------------------------------------------------
    # Optional existence check (used by ``cluster upload-to-share``)
    # ------------------------------------------------------------------

    def archive_exists_on_share(self, object_name: str) -> bool:
        """Return ``True`` if *object_name* already exists on the share.

        Used by ``cluster upload-to-share`` to skip uploads when the archive
        is already present (unless ``--force`` is given).  Only meaningful for
        providers that support remote listing or HTTP HEAD checks.

        The default implementation always returns ``False`` (no skip), so
        providers that do not override this method will always re-upload.

        Args:
            object_name: Filename of the archive on the server
                (e.g. ``campaign-2025-02-27-123456.tar.gz``).

        Returns:
            ``True`` if the archive already exists on the share, ``False``
            otherwise.
        """
        _ = object_name
        return False
