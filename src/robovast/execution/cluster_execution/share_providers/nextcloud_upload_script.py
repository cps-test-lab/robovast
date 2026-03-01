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
"""
Upload a run archive to a public Nextcloud share via WebDAV.

Runs inside the archiver sidecar (python:3.12-alpine, stdlib + boto3 only).
The archive must already exist at /data/{run_id}.tar.gz.

Usage: python - <run_id>  (script from stdin)
  or:  python nextcloud_upload_script.py <run_id>

Environment variables:
  ROBOVAST_SHARE_URL: Nextcloud public share URL
                      (e.g. https://cloud.example.com/s/AbCdEfGhIjKlMn)

Progress lines are written to stdout in the format:
  <run_id>  [████████░░░░░░░░░░░░]  xx.x%  X.X MiB  X.X MiB/s
"""

import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# Progress bar helpers (match download_results.py style)
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
    """Wraps a binary file object and prints a progress bar on each read."""

    CHUNK = 256 * 1024  # 256 KiB

    def __init__(self, fh, total, run_id):
        self._fh = fh
        self.total = total
        self._sent = 0
        self._run_id = run_id
        self._last_pct = -1
        self._start = time.monotonic()

    def read(self, n=-1):  # called by urllib internals
        data = self._fh.read(self.CHUNK if n == -1 else n)
        self._sent += len(data)
        self._render()
        return data

    def _render(self):
        if self.total <= 0:
            return
        pct = self._sent / self.total * 100
        if pct - self._last_pct < 1.0 and self._sent < self.total:
            return
        self._last_pct = pct
        elapsed = max(time.monotonic() - self._start, 1e-6)
        rate = self._sent / elapsed
        filled = int(BAR_WIDTH * self._sent / self.total)
        bar = "█" * filled + "░" * (BAR_WIDTH - filled)
        line = (
            f"{self._run_id}  [{bar}]  {pct:5.1f}%  "
            f"{_fmt_size(self._sent)}/{_fmt_size(self.total)}  {_fmt_rate(rate)}"
        )
        sys.stdout.write("\r" + line + CLEAR_EOL)
        sys.stdout.flush()

    # Required so urllib can inspect the request body size
    def __len__(self):
        return self.total


# ---------------------------------------------------------------------------
# WebDAV upload
# ---------------------------------------------------------------------------

def _build_webdav_url(share_url: str, filename: str) -> str:
    """Convert a public share URL to a WebDAV PUT URL.

    Nextcloud public share URLs look like:
      https://cloud.example.com/s/<token>

    The corresponding WebDAV endpoint for uploading a file is:
      https://cloud.example.com/public.php/webdav/<filename>

    The share token is used as the basic-auth username; the password is empty
    for password-less shares.
    """
    parsed = urllib.parse.urlparse(share_url.rstrip("/"))
    base = f"{parsed.scheme}://{parsed.netloc}"
    token = parsed.path.rstrip("/").split("/")[-1]
    webdav_url = f"{base}/public.php/webdav/{urllib.parse.quote(filename)}"
    return webdav_url, token


def upload(run_id: str, share_url: str) -> None:
    archive_path = f"/data/{run_id}.tar.gz"

    if not os.path.isfile(archive_path):
        sys.stderr.write(f"ERROR: archive not found: {archive_path}\n")
        sys.exit(1)

    total = os.path.getsize(archive_path)
    filename = os.path.basename(archive_path)
    webdav_url, token = _build_webdav_url(share_url, filename)

    # Basic auth: token as username, empty password for password-less shares
    credentials = urllib.parse.quote(token, safe="") + ":"
    auth_header = "Basic " + __import__("base64").b64encode(
        credentials.encode()
    ).decode()

    sys.stdout.write(f"{run_id}  uploading to Nextcloud...\n")
    sys.stdout.flush()

    with open(archive_path, "rb") as fh:
        reader = _ProgressReader(fh, total, run_id)
        req = urllib.request.Request(
            webdav_url,
            data=reader,
            method="PUT",
            headers={
                "Authorization": auth_header,
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/octet-stream",
                "Content-Length": str(total),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                status = resp.status
        except urllib.error.HTTPError as exc:
            sys.stdout.write("\n")
            sys.stderr.write(
                f"ERROR: HTTP {exc.code} uploading {filename} to Nextcloud: {exc.reason}\n"
            )
            sys.exit(1)
        except urllib.error.URLError as exc:
            sys.stdout.write("\n")
            sys.stderr.write(f"ERROR: Upload failed: {exc.reason}\n")
            sys.exit(1)

    sys.stdout.write("\r" + f"{run_id}  uploaded ({_fmt_size(total)})  ✓" + CLEAR_EOL + "\n")
    sys.stdout.flush()


def main():
    if len(sys.argv) < 2:
        sys.stderr.write(
            "Usage: python - <run_id>  (ROBOVAST_SHARE_URL must be set)\n"
        )
        sys.exit(1)

    run_id = sys.argv[1]
    share_url = os.environ.get("ROBOVAST_SHARE_URL", "")
    if not share_url:
        sys.stderr.write("ERROR: ROBOVAST_SHARE_URL environment variable is not set\n")
        sys.exit(1)

    upload(run_id, share_url)


if __name__ == "__main__":
    main()
