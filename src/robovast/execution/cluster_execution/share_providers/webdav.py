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

"""WebDAV share provider for ``cluster upload-to-share``."""

import base64
import os
import re
import urllib.parse

import click
import requests

from robovast.common.execution import is_campaign_dir

from .base import BaseShareProvider

__all__ = ["WebDavShareProvider"]


class WebDavShareProvider(BaseShareProvider):
    """Upload/download campaign archives to a WebDAV server with user/password login.

    Uses HTTP Basic Auth against a standard WebDAV endpoint.  Works with any
    WebDAV-compatible server (Nextcloud authenticated shares, Apache ``mod_dav``,
    Nginx with WebDAV module, ownCloud, etc.).

    Required ``.env`` variables:

    .. list-table::
       :header-rows: 1

       * - Variable
         - Description
       * - ``ROBOVAST_SHARE_TYPE``
         - Must be ``webdav``
       * - ``ROBOVAST_WEBDAV_URL``
         - Base URL of the WebDAV collection
           (e.g. ``https://nas.example.com/dav/results/``)
       * - ``ROBOVAST_WEBDAV_USER``
         - WebDAV username
       * - ``ROBOVAST_WEBDAV_PASSWORD``
         - WebDAV password
    """

    SHARE_TYPE = "webdav"

    # ------------------------------------------------------------------
    # BaseShareProvider interface
    # ------------------------------------------------------------------

    def required_env_vars(self) -> dict[str, str]:
        return {
            "ROBOVAST_WEBDAV_URL": (
                "Base URL of the WebDAV collection "
                "(e.g. https://nas.example.com/dav/results/)"
            ),
            "ROBOVAST_WEBDAV_USER": "WebDAV username",
            "ROBOVAST_WEBDAV_PASSWORD": "WebDAV password",
        }

    def get_upload_script_path(self) -> str:
        return os.path.join(
            os.path.dirname(__file__),
            "webdav_upload_script.py",
        )

    def build_pod_env(self) -> dict[str, str]:
        return {
            "ROBOVAST_WEBDAV_URL": os.environ["ROBOVAST_WEBDAV_URL"],
            "ROBOVAST_WEBDAV_USER": os.environ["ROBOVAST_WEBDAV_USER"],
            "ROBOVAST_WEBDAV_PASSWORD": os.environ["ROBOVAST_WEBDAV_PASSWORD"],
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        """Return a pre-baked ``Authorization: Basic`` header.

        Pre-baking avoids the issue where ``requests`` strips the
        ``Authorization`` header after following a redirect, which causes
        spurious 401 responses on servers that redirect (e.g. HTTP → HTTPS
        or path normalisation redirects).
        """
        user = os.environ["ROBOVAST_WEBDAV_USER"]
        password = os.environ["ROBOVAST_WEBDAV_PASSWORD"]
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    def _session(self) -> requests.Session:
        """Return a ``requests.Session`` with auth headers pre-set."""
        session = requests.Session()
        session.headers.update(self._auth_headers())
        return session

    def _base_url(self) -> str:
        url = os.environ["ROBOVAST_WEBDAV_URL"]
        if not url.endswith("/"):
            url += "/"
        return url

    def _file_url(self, object_name: str) -> str:
        return self._base_url() + urllib.parse.quote(object_name, safe="")

    # ------------------------------------------------------------------
    # Optional download interface
    # ------------------------------------------------------------------

    def list_campaign_archives_with_size(self) -> list[tuple[str, int]]:
        """List all campaign ``*.tar.gz`` files on the share.

        Recognizes archives whose base name (without ``.tar.gz``) matches the
        campaign naming convention: ``<name>-YYYY-MM-DD-HHMMSS``.

        Tries WebDAV ``PROPFIND`` first; falls back to parsing the Apache/nginx
        HTML directory index (``GET``) when the server returns 405 (Method Not
        Allowed), as some basic WebDAV hosts (e.g. Hetzner Storage Box) disable
        ``PROPFIND`` on path prefixes.

        Returns:
            List of ``(filename, size_in_bytes)`` tuples sorted by filename.
        """
        propfind_body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<D:propfind xmlns:D="DAV:">'
            "<D:prop><D:displayname/><D:getcontentlength/></D:prop>"
            "</D:propfind>"
        )
        with self._session() as session:
            resp = session.request(
                "PROPFIND",
                self._base_url(),
                data=propfind_body,
                headers={"Depth": "1", "Content-Type": "application/xml"},
                timeout=30,
            )

        if resp.status_code == 207:
            return self._parse_propfind_response(resp.text)

        if resp.status_code == 405:
            # Server does not support PROPFIND — fall back to HTML listing
            return self._list_via_html_index()

        raise click.UsageError(
            f"WebDAV PROPFIND failed with HTTP {resp.status_code}: {resp.text[:200]}"
        )

    def _parse_propfind_response(self, xml_text: str) -> list[tuple[str, int]]:
        """Parse a DAV:multistatus XML response and return campaign archive entries."""
        from xml.etree import ElementTree as ET  # pylint: disable=import-outside-toplevel

        ns = {"D": "DAV:"}
        root = ET.fromstring(xml_text)
        results: list[tuple[str, int]] = []
        for response in root.findall("D:response", ns):
            href = response.findtext("D:href", default="", namespaces=ns)
            name = urllib.parse.unquote(href.rstrip("/").rsplit("/", 1)[-1])
            if not name.endswith(".tar.gz"):
                continue
            if not is_campaign_dir(name[:-len(".tar.gz")]):
                continue
            size_text = response.findtext(
                "D:propstat/D:prop/D:getcontentlength",
                default="-1",
                namespaces=ns,
            )
            try:
                size = int(size_text)
            except (TypeError, ValueError):
                size = -1
            results.append((name, size))

        results.sort(key=lambda t: t[0])
        return results

    def _list_via_html_index(self) -> list[tuple[str, int]]:
        """List campaign archives by parsing the HTML directory index.

        Falls back to this when the server does not support ``PROPFIND``
        (e.g. Hetzner Storage Box).  Matches any ``*.tar.gz`` href whose base
        name (without ``.tar.gz``) passes :func:`is_campaign_dir` and returns
        ``(name, -1)`` tuples (sizes not available from HTML listings).
        """
        with self._session() as session:
            resp = session.get(self._base_url(), timeout=30)

        if not resp.ok:
            raise click.UsageError(
                f"WebDAV directory listing (GET) failed with HTTP "
                f"{resp.status_code}: {resp.text[:200]}"
            )

        # Match any href pointing to a *.tar.gz file, then filter by campaign naming
        pattern = re.compile(r'href=["\']([^"\']+\.tar\.gz)["\']', re.IGNORECASE)
        results = []
        for href_val in pattern.findall(resp.text):
            base = urllib.parse.unquote(href_val.rstrip("/").rsplit("/", 1)[-1])
            if base.endswith(".tar.gz") and is_campaign_dir(base[:-len(".tar.gz")]):
                results.append((base, -1))
        results.sort(key=lambda t: t[0])
        return results

    def download_archive(
        self,
        object_name: str,
        dest_path: str,
        progress_callback=None,
    ) -> None:
        """Download *object_name* from the WebDAV share to *dest_path*.

        Args:
            object_name: Filename of the archive on the server.
            dest_path: Local destination path.
            progress_callback: Optional ``(bytes_received, total_bytes)`` callable.
        """
        url = self._file_url(object_name)
        with self._session() as session:
            with session.get(url, stream=True, timeout=60) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("Content-Length", 0))
                received = 0
                with open(dest_path, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=1024 * 256):
                        fh.write(chunk)
                        received += len(chunk)
                        if progress_callback and total:
                            progress_callback(received, total)

    def remove_archive(self, object_name: str) -> None:
        """Remove *object_name* from the WebDAV share via HTTP DELETE.

        Args:
            object_name: Filename of the archive to remove.
        """
        url = self._file_url(object_name)
        with self._session() as session:
            resp = session.delete(url, timeout=30)
        if resp.status_code not in (200, 204):
            raise click.UsageError(
                f"WebDAV DELETE of '{object_name}' failed with "
                f"HTTP {resp.status_code}: {resp.text[:200]}"
            )
