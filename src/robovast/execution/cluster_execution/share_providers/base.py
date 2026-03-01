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

"""Abstract base class for share providers used by ``cluster download-to-share``."""

import os
from abc import ABC, abstractmethod

import click

__all__ = ["BaseShareProvider"]


class BaseShareProvider(ABC):
    """Base class for all share providers.

    A share provider encapsulates everything needed to upload a tar.gz archive
    from inside the archiver sidecar of the robovast pod to a remote storage
    service (Nextcloud, Google Drive, …).

    Subclasses must:

    * Set :attr:`SHARE_TYPE` to the provider name (matching the entry-point key).
    * Declare all required environment variables in :meth:`required_env_vars`.
    * Return the path to the pod-side upload Python script via
      :meth:`get_upload_script_path`.
    * Provide environment variables for the pod via :meth:`build_pod_env`.

    The constructor automatically validates that all required env vars are
    present; it raises :class:`click.UsageError` if any are missing.  Values
    are read from ``os.environ`` (which is already populated by
    ``python-dotenv`` before the provider is instantiated).

    Pod-side scripts run inside the ``robovast-archiver`` image
    (``python:3.12-alpine`` + ``pigz``, ``boto3``, ``google-auth``,
    ``google-api-python-client``).  No additional packages need to be
    pip-installed at runtime for the built-in providers.

    To add a **new provider**:

    1. Create a new file in this package (e.g. ``myshare.py``).
    2. Subclass :class:`BaseShareProvider`, fill in the four abstract members.
    3. Create a pod-side upload script (piped via stdin to ``python -``).
    4. Register the provider in ``pyproject.toml`` under
       ``[tool.poetry.plugins."robovast.share_providers"]``.
    """

    #: Short identifier for the provider, e.g. ``"nextcloud"`` or ``"gdrive"``.
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
    def get_upload_script_path(self) -> str:
        """Return the absolute path to the pod-side Python upload script.

        The script is piped via stdin to ``python -`` inside the archiver
        container (``robovast-archiver`` image).  It must be self-contained
        and only use packages available in that image: standard library,
        ``boto3``, ``google-auth``, ``google-api-python-client``.

        The script receives the run ID as ``sys.argv[1]``.
        Environment variables from :meth:`build_pod_env` are available via
        ``os.environ``.
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
