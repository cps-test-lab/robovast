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

"""Google Drive share provider for ``cluster download-to-share``."""

import os
import re

import click

from .base import BaseShareProvider

__all__ = ["GDriveShareProvider"]

_GDRIVE_FOLDER_ID_RE = re.compile(r"/folders/([a-zA-Z0-9_-]+)")


def _extract_folder_id(url_or_id: str) -> str:
    """Extract the folder ID from a Google Drive folder URL or return it as-is."""
    match = _GDRIVE_FOLDER_ID_RE.search(url_or_id)
    if match:
        return match.group(1)
    # Assume the value is already a raw folder ID
    return url_or_id.strip()


class GDriveShareProvider(BaseShareProvider):
    """Upload run archives to a Google Drive folder via a service account.

    The service account must have write access to the target folder (share
    the folder with the service account email).  The folder may be a shared
    drive or a regular "My Drive" folder.

    Required ``.env`` variables:

    .. list-table::
       :header-rows: 1

       * - Variable
         - Description
       * - ``ROBOVAST_SHARE_TYPE``
         - Must be ``gdrive``
       * - ``ROBOVAST_SHARE_URL``
         - Google Drive folder URL or bare folder ID
           (e.g.
           ``https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUv``
           or just ``1AbCdEfGhIjKlMnOpQrStUv``)
       * - ``ROBOVAST_GDRIVE_SERVICE_ACCOUNT_JSON``
         - Path to the service account JSON key file on the **local** machine.
           The file contents are read and forwarded into the pod at runtime.
    """

    SHARE_TYPE = "gdrive"

    def required_env_vars(self) -> dict[str, str]:
        return {
            "ROBOVAST_SHARE_URL": (
                "Google Drive folder URL or bare folder ID "
                "(the service account must have write access to this folder)"
            ),
            "ROBOVAST_GDRIVE_SERVICE_ACCOUNT_JSON": (
                "Local path to the Google service account JSON key file"
            ),
        }

    def get_upload_script_path(self) -> str:
        return os.path.join(
            os.path.dirname(__file__),
            "gdrive_upload_script.py",
        )

    def build_pod_env(self) -> dict[str, str]:
        sa_json_path = os.environ["ROBOVAST_GDRIVE_SERVICE_ACCOUNT_JSON"]
        if not os.path.isfile(sa_json_path):
            raise click.UsageError(
                f"Google Drive service account JSON file not found: {sa_json_path}\n"
                "Check the ROBOVAST_GDRIVE_SERVICE_ACCOUNT_JSON variable in your .env file."
            )
        with open(sa_json_path, encoding="utf-8") as fh:
            sa_json_content = fh.read()

        folder_id = _extract_folder_id(os.environ["ROBOVAST_SHARE_URL"])
        return {
            "GDRIVE_FOLDER_ID": folder_id,
            "GDRIVE_SA_JSON": sa_json_content,
        }
