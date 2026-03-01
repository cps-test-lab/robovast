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

"""Nextcloud share provider for ``cluster download-to-share``."""

import os

from .base import BaseShareProvider

__all__ = ["NextcloudShareProvider"]


class NextcloudShareProvider(BaseShareProvider):
    """Upload run archives to a public Nextcloud share (WebDAV).

    The share must be a public link that allows file uploads without a
    password.  In the Nextcloud web UI, create a share with "Allow upload
    and editing" enabled and copy the link.

    Required ``.env`` variables:

    .. list-table::
       :header-rows: 1

       * - Variable
         - Description
       * - ``ROBOVAST_SHARE_TYPE``
         - Must be ``nextcloud``
       * - ``ROBOVAST_SHARE_URL``
         - Public share URL (e.g.
           ``https://cloud.example.com/s/AbCdEfGhIjKlMn``)
    """

    SHARE_TYPE = "nextcloud"

    def required_env_vars(self) -> dict[str, str]:
        return {
            "ROBOVAST_SHARE_URL": (
                "Nextcloud public share URL "
                "(e.g. https://cloud.example.com/s/AbCdEfGhIjKlMn)"
            ),
        }

    def get_upload_script_path(self) -> str:
        return os.path.join(
            os.path.dirname(__file__),
            "nextcloud_upload_script.py",
        )

    def build_pod_env(self) -> dict[str, str]:
        return {
            "ROBOVAST_SHARE_URL": os.environ["ROBOVAST_SHARE_URL"],
        }
