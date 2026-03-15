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
"""
Upload a run archive to a WebDAV server using HTTP Basic Auth.

Runs inside the archiver sidecar (python:3.12-alpine). Installs requests at
startup if it is not already available.
The archive must already exist at /data/{campaign}.tar.gz.

Usage: python - <campaign>  (script from stdin)
  or:  python webdav_upload_script.py <campaign>

Environment variables (all set by WebDavShareProvider.build_pod_env):
  ROBOVAST_WEBDAV_URL       Base URL of the WebDAV collection (trailing slash optional)
  ROBOVAST_WEBDAV_USER      WebDAV username
  ROBOVAST_WEBDAV_PASSWORD  WebDAV password

Progress lines are written to stdout in the format:
  <campaign>  [████████░░░░░░░░░░░░]  xx.x%  X.X MiB  X.X MiB/s
"""

import base64
import os
import subprocess
import sys
import time
import urllib.parse

# ---------------------------------------------------------------------------
# Ensure requests is available (not bundled in the archiver image)
# ---------------------------------------------------------------------------
try:
    import requests  # noqa: E402
except ImportError:
    sys.stdout.write("Installing requests…\n")
    sys.stdout.flush()
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "requests"],
        check=True,
    )
    import requests  # noqa: E402

# ---------------------------------------------------------------------------
# Progress bar helpers
# ---------------------------------------------------------------------------
BAR_WIDTH = 20
CLEAR_EOL = "\033[K"


def _fmt_size(n):
    return f"{n / 1024 / 1024:.1f} MiB"


def _fmt_rate(bps):
    if bps >= 1024 * 1024:
        return f"{bps / 1024 / 1024:.1f} MiB/s"
    if bps >= 1024:
        return f"{bps / 1024:.1f} KiB/s"
    return f"{bps:.0f} B/s"


class _ProgressReader:
    """Wraps a file object and renders a progress bar as bytes are read."""

    def __init__(self, fh, file_size: int, campaign: str, offset: int = 0) -> None:
        self._fh = fh
        self._file_size = file_size   # total file size (for % display)
        self._send_size = file_size - offset  # bytes we will send in this session
        self._campaign = campaign
        self._sent = 0                # bytes sent in this session
        self._display_offset = offset # bytes already on server (resume)
        self._last_pct = -1.0
        self._start = time.monotonic()

    def read(self, size: int = -1) -> bytes:
        chunk = self._fh.read(size)
        if chunk:
            self._sent += len(chunk)
            displayed = self._display_offset + self._sent
            pct = displayed / self._file_size * 100 if self._file_size else 0.0
            if pct - self._last_pct >= 1.0 or displayed >= self._file_size:
                self._last_pct = pct
                elapsed = max(time.monotonic() - self._start, 1e-6)
                rate = self._sent / elapsed
                filled = int(BAR_WIDTH * displayed / self._file_size) if self._file_size else 0
                progress_bar = "█" * filled + "░" * (BAR_WIDTH - filled)
                line = (
                    f"{self._campaign}  [{progress_bar}]  {pct:5.1f}%  "
                    f"{_fmt_size(displayed)}/{_fmt_size(self._file_size)}  "
                    f"{_fmt_rate(rate)}"
                )
                sys.stdout.write("\r" + line + CLEAR_EOL)
                sys.stdout.flush()
        return chunk

    def __len__(self) -> int:
        return self._send_size  # tells requests how many bytes to stream


# ---------------------------------------------------------------------------
# WebDAV upload
# ---------------------------------------------------------------------------

def _get_remote_size(upload_url: str, auth_header: str) -> int:
    """Return the Content-Length of the remote file, or 0 if absent/unreachable."""
    try:
        resp = requests.head(
            upload_url,
            headers={"Authorization": auth_header},
            timeout=30,
            allow_redirects=True,
        )
        if resp.status_code in (200, 204):
            return int(resp.headers.get("Content-Length", 0))
    except Exception:
        pass
    return 0


def upload(campaign: str) -> None:
    archive_path = f"/data/{campaign}.tar.gz"

    if not os.path.isfile(archive_path):
        sys.stderr.write(f"ERROR: archive not found: {archive_path}\n")
        sys.exit(1)

    base_url = os.environ["ROBOVAST_WEBDAV_URL"]
    user = os.environ["ROBOVAST_WEBDAV_USER"]
    password = os.environ["ROBOVAST_WEBDAV_PASSWORD"]

    if not base_url.endswith("/"):
        base_url += "/"

    filename = os.path.basename(archive_path)
    upload_url = base_url + urllib.parse.quote(filename, safe="")

    total = os.path.getsize(archive_path)

    # Pre-bake Authorization header so it is sent with the first request.
    # requests' auth=(user, password) waits for a 401 challenge before adding
    # the header, which cannot work with a streamed body (the body can't be
    # replayed after the mid-stream 401).
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    auth_header = f"Basic {token}"

    # Check for an existing (possibly partial) upload to resume.
    remote_size = _get_remote_size(upload_url, auth_header)
    if remote_size == total:
        sys.stdout.write(
            f"{campaign}  already fully uploaded ({_fmt_size(total)})  ✓\n"
        )
        sys.stdout.flush()
        return

    offset = remote_size if 0 < remote_size < total else 0
    if offset > 0:
        sys.stdout.write(
            f"{campaign}  resuming from {_fmt_size(offset)} / {_fmt_size(total)}…\n"
        )
    else:
        sys.stdout.write(f"{campaign}  uploading via WebDAV to {upload_url}…\n")
    sys.stdout.flush()

    headers = {
        "Content-Length": str(total - offset),
        "Authorization": auth_header,
    }
    if offset > 0:
        headers["Content-Range"] = f"bytes {offset}-{total - 1}/{total}"

    with open(archive_path, "rb") as fh:
        if offset > 0:
            fh.seek(offset)
        reader = _ProgressReader(fh, total, campaign, offset=offset)
        resp = requests.put(
            upload_url,
            data=reader,
            headers=headers,
            timeout=None,
        )

    if resp.status_code not in (200, 201, 204):
        sys.stderr.write(
            f"\nERROR: WebDAV PUT returned HTTP {resp.status_code}: "
            f"{resp.text[:200]}\n"
        )
        sys.exit(1)

    # Verify the connection didn't drop mid-stream (server may return 200 even
    # when it only received partial data if the client closed the socket early).
    if reader._sent < reader._send_size:
        sys.stderr.write(
            f"\nERROR: Upload incomplete — sent {_fmt_size(reader._sent + offset)} "
            f"of {_fmt_size(total)} "
            f"({(reader._sent + offset) / total * 100:.1f}%). "
            "Re-run the command to resume.\n"
        )
        sys.exit(1)

    sys.stdout.write(
        "\r" + f"{campaign}  uploaded ({_fmt_size(total)})  ✓" + CLEAR_EOL + "\n"
    )
    sys.stdout.flush()


def main():
    if len(sys.argv) < 2:
        sys.stderr.write(
            "Usage: python - <campaign_id>  "
            "(ROBOVAST_WEBDAV_URL / _USER / _PASSWORD must be set)\n"
        )
        sys.exit(1)

    campaign = sys.argv[1]

    for var in ("ROBOVAST_WEBDAV_URL", "ROBOVAST_WEBDAV_USER", "ROBOVAST_WEBDAV_PASSWORD"):
        if not os.environ.get(var):
            sys.stderr.write(f"ERROR: {var} environment variable is not set\n")
            sys.exit(1)

    upload(campaign)


if __name__ == "__main__":
    main()
