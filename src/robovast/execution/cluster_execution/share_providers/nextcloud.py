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

"""Nextcloud share provider for ``cluster upload-to-share``."""

import base64
import os
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Callable

import click
import requests

from robovast.common.execution import is_campaign_dir

from .base import BaseShareProvider

__all__ = ["NextcloudShareProvider"]


class NextcloudShareProvider(BaseShareProvider):
    """Upload/download campaign archives to a public Nextcloud share (WebDAV).

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

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _parse_share_url(self) -> tuple[str, str]:
        """Return ``(webdav_collection_url, share_token)`` from the share URL.

        A Nextcloud public share URL looks like::

            https://cloud.example.com/s/<token>

        The corresponding WebDAV collection root is::

            https://cloud.example.com/public.php/webdav/
        """
        share_url = os.environ["ROBOVAST_SHARE_URL"].rstrip("/")
        parsed = urllib.parse.urlparse(share_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        token = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        webdav_url = f"{base}/public.php/webdav/"
        return webdav_url, token

    def _auth_headers(self) -> dict[str, str]:
        """Return a ``Authorization: Basic`` header using the share token as username."""
        _, token = self._parse_share_url()
        credentials = base64.b64encode(f"{token}:".encode()).decode()
        return {"Authorization": f"Basic {credentials}"}

    def _session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update(self._auth_headers())
        # Nextcloud requires this header on public.php/webdav/ to accept the
        # request without redirecting to the web login page.
        session.headers["X-Requested-With"] = "XMLHttpRequest"
        return session

    # ------------------------------------------------------------------
    # Download interface (``results download-from-share``)
    # ------------------------------------------------------------------

    def list_campaign_archives_with_size(self) -> list[tuple[str, int]]:
        """Return ``(filename, size_in_bytes)`` for each campaign ``*.tar.gz`` on the share.

        Uses WebDAV ``PROPFIND Depth: 1`` against the Nextcloud
        ``public.php/webdav/`` endpoint, authenticated with the share token
        as the HTTP Basic Auth username.  *size_in_bytes* is ``-1`` when the
        server does not return a ``getcontentlength`` value.
        """
        webdav_url, _ = self._parse_share_url()
        propfind_body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<D:propfind xmlns:D="DAV:">'
            "<D:prop><D:displayname/><D:getcontentlength/></D:prop>"
            "</D:propfind>"
        )
        with self._session() as session:
            resp = session.request(
                "PROPFIND",
                webdav_url,
                data=propfind_body,
                headers={"Depth": "1", "Content-Type": "application/xml"},
                timeout=30,
            )

        if resp.status_code not in (207,):
            raise click.UsageError(
                f"Nextcloud PROPFIND failed with HTTP {resp.status_code}: {resp.text[:300]}"
            )

        ns = {"D": "DAV:"}
        root = ET.fromstring(resp.text)
        results: list[tuple[str, int]] = []
        for response in root.findall("D:response", ns):
            href = response.findtext("D:href", default="", namespaces=ns)
            name = urllib.parse.unquote(href.rstrip("/").rsplit("/", 1)[-1])
            if not name.endswith(".tar.gz"):
                continue
            if not is_campaign_dir(name[: -len(".tar.gz")]):
                continue
            size = -1
            for propstat in response.findall("D:propstat", ns):
                length_el = propstat.find("D:prop/D:getcontentlength", ns)
                if length_el is not None and length_el.text:
                    try:
                        size = int(length_el.text)
                    except ValueError:
                        pass
                    break
            results.append((name, size))

        results.sort(key=lambda t: t[0])
        return results

    def list_campaign_archives(self) -> list[str]:
        """Return a list of campaign ``*.tar.gz`` filenames on the share."""
        return [name for name, _ in self.list_campaign_archives_with_size()]

    def download_archive(
        self,
        object_name: str,
        dest_path: str,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> None:
        """Download *object_name* from the Nextcloud share to *dest_path*.

        Args:
            object_name: Filename of the archive on the share.
            dest_path: Local destination path.
            progress_callback: Optional ``(bytes_received, total_bytes)`` callable.
        """
        webdav_url, _ = self._parse_share_url()
        file_url = webdav_url + urllib.parse.quote(object_name, safe="")
        with self._session() as session:
            with session.get(file_url, stream=True, timeout=60) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("Content-Length", 0))
                received = 0
                with open(dest_path, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=256 * 1024):
                        fh.write(chunk)
                        received += len(chunk)
                        if progress_callback and total:
                            progress_callback(received, total)
    def remove_archive(self, object_name: str) -> None:
        """Delete *object_name* from the Nextcloud share via WebDAV ``DELETE``.

        Args:
            object_name: Filename of the archive on the share (as returned by
                :meth:`list_campaign_archives`).
        """
        webdav_url, _ = self._parse_share_url()
        file_url = webdav_url + urllib.parse.quote(object_name, safe="")
        with self._session() as session:
            resp = session.delete(file_url, timeout=30)
        if resp.status_code not in (200, 204):
            raise click.UsageError(
                f"Nextcloud DELETE failed for '{object_name}': "
                f"HTTP {resp.status_code}: {resp.text[:300]}"
            )