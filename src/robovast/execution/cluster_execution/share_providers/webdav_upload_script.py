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

    def __init__(self, fh, total: int, campaign: str) -> None:
        self._fh = fh
        self._total = total
        self._campaign = campaign
        self._sent = 0
        self._last_pct = -1.0
        self._start = time.monotonic()

    def read(self, size: int = -1) -> bytes:
        chunk = self._fh.read(size)
        if chunk:
            self._sent += len(chunk)
            pct = self._sent / self._total * 100 if self._total else 0.0
            if pct - self._last_pct >= 1.0 or self._sent >= self._total:
                self._last_pct = pct
                elapsed = max(time.monotonic() - self._start, 1e-6)
                rate = self._sent / elapsed
                filled = int(BAR_WIDTH * self._sent / self._total) if self._total else 0
                progress_bar = "█" * filled + "░" * (BAR_WIDTH - filled)
                line = (
                    f"{self._campaign}  [{progress_bar}]  {pct:5.1f}%  "
                    f"{_fmt_size(self._sent)}/{_fmt_size(self._total)}  "
                    f"{_fmt_rate(rate)}"
                )
                sys.stdout.write("\r" + line + CLEAR_EOL)
                sys.stdout.flush()
        return chunk

    def __len__(self) -> int:
        return self._total


# ---------------------------------------------------------------------------
# WebDAV upload
# ---------------------------------------------------------------------------

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

    sys.stdout.write(f"{campaign}  uploading via WebDAV to {upload_url}…\n")
    sys.stdout.flush()

    auth = (user, password)

    with open(archive_path, "rb") as fh:
        reader = _ProgressReader(fh, total, campaign)
        resp = requests.put(
            upload_url,
            data=reader,
            auth=auth,
            headers={"Content-Length": str(total)},
            timeout=None,
        )

    if resp.status_code not in (200, 201, 204):
        sys.stderr.write(
            f"\nERROR: WebDAV PUT returned HTTP {resp.status_code}: "
            f"{resp.text[:200]}\n"
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
