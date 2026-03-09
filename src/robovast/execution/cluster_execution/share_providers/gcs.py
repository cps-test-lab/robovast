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

"""Google Cloud Storage share provider for ``cluster upload-to-share``."""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Callable

import click

from .base import BaseShareProvider

__all__ = ["GcsShareProvider"]


class GcsShareProvider(BaseShareProvider):
    """Upload campaign archives to a Google Cloud Storage bucket.

    Authentication uses a service-account key file.  Create a service account
    with the *Storage Object Creator* role on the target bucket, generate a
    JSON key, download the file, and point ``ROBOVAST_GCS_KEY_FILE`` at it.

    Required ``.env`` variables:

    .. list-table::
       :header-rows: 1

       * - Variable
         - Description
       * - ``ROBOVAST_SHARE_TYPE``
         - Must be ``gcs``
       * - ``ROBOVAST_GCS_BUCKET``
         - Target GCS bucket name (e.g. ``my-robovast-results``)
       * - ``ROBOVAST_GCS_KEY_FILE``
         - Path to the service-account JSON key file
           (**required for** ``cluster upload-to-share`` **only**;
           not needed for ``results download-from-share`` on public buckets)

    Optional ``.env`` variables:

    .. list-table::
       :header-rows: 1

       * - Variable
         - Description
       * - ``ROBOVAST_GCS_PREFIX``
         - Object-name prefix inside the bucket (e.g. ``results/``).
           Defaults to the bucket root.
    """

    SHARE_TYPE = "gcs"

    def required_env_vars(self) -> dict[str, str]:
        # ROBOVAST_GCS_KEY_FILE is only required for upload (cluster upload-to-share);
        # download uses the public GCS HTTP API and needs no credentials.
        return {
            "ROBOVAST_GCS_BUCKET": (
                "GCS bucket name (e.g. my-robovast-results)"
            ),
        }

    def get_upload_script_path(self) -> str:
        return os.path.join(
            os.path.dirname(__file__),
            "gcs_upload_script.py",
        )

    def build_pod_env(self) -> dict[str, str]:
        key_file = os.environ.get("ROBOVAST_GCS_KEY_FILE", "")
        if not key_file:
            raise click.UsageError(
                "ROBOVAST_GCS_KEY_FILE is required for cluster upload-to-share.\n"
                "Set it to the path of a service-account JSON key file with "
                "Storage Object Creator access on the bucket."
            )
        if not os.path.isfile(key_file):
            raise click.UsageError(
                f"ROBOVAST_GCS_KEY_FILE: file not found: {key_file}"
            )
        try:
            with open(key_file) as fh:
                key_json = fh.read()
            json.loads(key_json)  # validate
        except (OSError, json.JSONDecodeError) as exc:
            raise click.UsageError(
                f"ROBOVAST_GCS_KEY_FILE: could not read key file {key_file!r}: {exc}"
            ) from exc

        env = {
            "ROBOVAST_GCS_BUCKET": os.environ["ROBOVAST_GCS_BUCKET"],
            "ROBOVAST_GCS_KEY_JSON": key_json,
        }
        prefix = os.environ.get("ROBOVAST_GCS_PREFIX", "")
        if prefix:
            env["ROBOVAST_GCS_PREFIX"] = prefix
        return env

    # ------------------------------------------------------------------
    # Download interface (public bucket, no auth required)
    # ------------------------------------------------------------------

    def list_campaign_archives(self) -> list[str]:
        """List all ``campaign-*.tar.gz`` objects in the configured GCS bucket.

        Uses the public GCS XML API (no credentials required for public buckets).
        Handles GCS list pagination via the ``NextContinuationToken`` marker.
        """
        bucket = os.environ["ROBOVAST_GCS_BUCKET"]
        prefix = os.environ.get("ROBOVAST_GCS_PREFIX", "") + "campaign"

        found: list[str] = []
        continuation_token: str | None = None
        ns = {"s3": "http://doc.s3.amazonaws.com/2006-03-01"}

        while True:
            params: dict[str, str] = {"prefix": prefix}
            if continuation_token:
                params["continuation-token"] = continuation_token

            url = (
                f"https://storage.googleapis.com/{urllib.parse.quote(bucket, safe='')}"
                f"?{urllib.parse.urlencode(params)}"
            )
            try:
                with urllib.request.urlopen(url, timeout=30) as resp:
                    body = resp.read()
            except urllib.error.HTTPError as exc:
                raise click.UsageError(
                    f"Could not list GCS bucket '{bucket}': HTTP {exc.code} {exc.reason}\n"
                    "Make sure the bucket is publicly readable."
                ) from exc

            root = ET.fromstring(body)
            for content in root.findall("s3:Contents", ns):
                key_el = content.find("s3:Key", ns)
                if key_el is not None and key_el.text and key_el.text.endswith(".tar.gz"):
                    found.append(key_el.text)

            # Check for next page
            token_el = root.find("s3:NextContinuationToken", ns)
            if token_el is not None and token_el.text:
                continuation_token = token_el.text
            else:
                break

        return found

    def download_archive(
        self,
        object_name: str,
        dest_path: str,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> None:
        """Stream *object_name* from the public GCS bucket to *dest_path*.

        Uses chunked streaming so that archives of any size (including 100 GB+)
        are written incrementally without loading the file into memory.

        Args:
            object_name: GCS object key (as returned by :meth:`list_campaign_archives`).
            dest_path: Local file path to write the downloaded content to.
            progress_callback: Optional ``(bytes_received, total_bytes)`` callable
                called after each chunk.  *total_bytes* is 0 if unknown.
        """
        bucket = os.environ["ROBOVAST_GCS_BUCKET"]
        url = (
            f"https://storage.googleapis.com/"
            f"{urllib.parse.quote(bucket, safe='')}"
            f"/{urllib.parse.quote(object_name, safe='/')}"
        )

        CHUNK = 256 * 1024  # 256 KiB
        received = 0

        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                with open(dest_path, "wb") as fh:
                    while True:
                        chunk = resp.read(CHUNK)
                        if not chunk:
                            break
                        fh.write(chunk)
                        received += len(chunk)
                        if progress_callback is not None:
                            progress_callback(received, total)
        except urllib.error.HTTPError as exc:
            raise click.UsageError(
                f"Failed to download '{object_name}' from GCS bucket '{bucket}': "
                f"HTTP {exc.code} {exc.reason}"
            ) from exc
        except urllib.error.URLError as exc:
            raise click.UsageError(
                f"Failed to download '{object_name}': {exc.reason}"
            ) from exc
