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
Upload a run archive to a Google Cloud Storage bucket.

Runs inside the archiver sidecar (python:3.12-alpine + google-auth).
The archive must already exist at /data/{campaign}.tar.gz.

Usage: python - <campaign>  (script from stdin)
  or:  python gcs_upload_script.py <campaign>

Environment variables:
  ROBOVAST_GCS_BUCKET:    GCS bucket name (e.g. my-robovast-results)
  ROBOVAST_GCS_KEY_JSON:  Service-account key as a JSON string
  ROBOVAST_GCS_PREFIX:    (optional) Object-name prefix, e.g. "results/"

Interrupted uploads are automatically resumed.  The session URI is saved to
/data/{campaign}.gcs_session; if that file exists when the script starts, it
queries GCS for how many bytes were already received and continues from there.
Sessions expire after ~7 days (GCS default); the script detects a 404 and
starts a fresh upload automatically.

Progress lines are written to stdout in the format:
  <campaign>  [████████░░░░░░░░░░░░]  xx.x%  X.X MiB  X.X MiB/s
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# Progress bar helpers (match nextcloud_upload_script.py style)
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
    """Wraps a binary file object and prints a progress bar on each read.

    ``start_offset`` is the number of bytes already uploaded in a previous
    attempt so the progress bar reflects overall progress from the start.
    ``__len__`` returns only the remaining bytes so urllib sets the correct
    ``Content-Length`` on the PUT request.
    """

    CHUNK = 256 * 1024  # 256 KiB

    def __init__(self, fh, total, campaign, start_offset=0):
        self._fh = fh
        self.total = total
        self._sent = start_offset       # includes bytes already on GCS
        self._to_send = total - start_offset
        self._campaign = campaign
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
        # Rate reflects only bytes sent in *this* run
        this_run = self._sent - (self.total - self._to_send)
        rate = this_run / elapsed
        filled = int(BAR_WIDTH * self._sent / self.total)
        progressbar = "█" * filled + "░" * (BAR_WIDTH - filled)
        line = (
            f"{self._campaign}  [{progressbar}]  {pct:5.1f}%  "
            f"{_fmt_size(self._sent)}/{_fmt_size(self.total)}  {_fmt_rate(rate)}"
        )
        sys.stdout.write("\r" + line + CLEAR_EOL)
        sys.stdout.flush()

    def __len__(self):
        # urllib uses this to set Content-Length; only the remaining bytes
        return self._to_send


# ---------------------------------------------------------------------------
# GCS auth
# ---------------------------------------------------------------------------

def _get_access_token(key_json: dict) -> str:
    """Exchange a service-account key for a short-lived Bearer token."""
    try:
        import google.auth.transport.requests  # noqa: PLC0415
        from google.oauth2 import service_account  # noqa: PLC0415
    except ImportError as exc:
        sys.stderr.write(
            f"ERROR: google-auth is not installed in this environment: {exc}\n"
        )
        sys.exit(1)

    scopes = ["https://www.googleapis.com/auth/devstorage.read_write"]
    credentials = service_account.Credentials.from_service_account_info(
        key_json, scopes=scopes
    )
    credentials.refresh(google.auth.transport.requests.Request())
    return credentials.token


# ---------------------------------------------------------------------------
# GCS resumable upload — session management
# ---------------------------------------------------------------------------

def _session_file(campaign: str) -> str:
    return f"/data/{campaign}.gcs_session"


def _save_session(campaign: str, session_uri: str) -> None:
    with open(_session_file(campaign), "w") as fh:
        fh.write(session_uri)


def _load_session(campaign: str) -> str | None:
    path = _session_file(campaign)
    if os.path.isfile(path):
        with open(path) as fh:
            return fh.read().strip() or None
    return None


def _delete_session(campaign: str) -> None:
    path = _session_file(campaign)
    try:
        os.remove(path)
    except OSError:
        pass


def _initiate_resumable_upload(bucket: str, object_name: str, total: int, token: str) -> str:
    """POST to the GCS resumable-upload initiation endpoint.

    Returns the session URI.
    """
    url = (
        f"https://storage.googleapis.com/upload/storage/v1/b/"
        f"{urllib.parse.quote(bucket, safe='')}/o"
        f"?uploadType=resumable"
    )
    body = json.dumps({"name": object_name}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": "application/octet-stream",
            "X-Upload-Content-Length": str(total),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            session_uri = resp.headers.get("Location")
    except urllib.error.HTTPError as exc:
        sys.stderr.write(
            f"ERROR: HTTP {exc.code} initiating resumable upload: {exc.reason}\n"
        )
        sys.exit(1)

    if not session_uri:
        sys.stderr.write("ERROR: GCS did not return a resumable upload session URI\n")
        sys.exit(1)

    return session_uri


def _query_upload_progress(session_uri: str, total: int) -> int | None:
    """Ask GCS how many bytes it has received for an in-progress session.

    Returns:
        Byte offset to resume from (0 = nothing received yet, ``total`` =
        already complete), or ``None`` if the session has expired (404).
    """
    req = urllib.request.Request(
        session_uri,
        data=b"",
        method="PUT",
        headers={
            "Content-Range": f"bytes */{total}",
            "Content-Length": "0",
        },
    )
    try:
        # 200 / 201 — upload already complete
        with urllib.request.urlopen(req, timeout=30):
            return total
    except urllib.error.HTTPError as exc:
        if exc.code == 308:  # Resume Incomplete
            range_header = exc.headers.get("Range", "")
            if range_header:
                # Range: bytes=0-<last_received_byte>
                last_byte = int(range_header.split("-")[-1])
                return last_byte + 1
            return 0  # session exists but nothing received yet
        if exc.code == 404:
            return None  # session expired
        sys.stderr.write(
            f"ERROR: HTTP {exc.code} querying upload progress: {exc.reason}\n"
        )
        sys.exit(1)


def _get_session(campaign: str, bucket: str, object_name: str, total: int, token: str) -> tuple[str, int]:
    """Return (session_uri, resume_offset).

    Tries to reuse a saved session first.  Falls back to a fresh session if
    none is saved or if the saved session has expired.
    """
    saved_uri = _load_session(campaign)
    if saved_uri:
        offset = _query_upload_progress(saved_uri, total)
        if offset is None:
            sys.stdout.write(f"{campaign}  previous session expired — starting fresh\n")
            sys.stdout.flush()
            _delete_session(campaign)
        elif offset == total:
            # Upload was already finished in a previous run; nothing to do.
            return saved_uri, total
        else:
            sys.stdout.write(
                f"{campaign}  resuming from {_fmt_size(offset)} / {_fmt_size(total)}\n"
            )
            sys.stdout.flush()
            return saved_uri, offset

    # Fresh session
    session_uri = _initiate_resumable_upload(bucket, object_name, total, token)
    _save_session(campaign, session_uri)
    return session_uri, 0


# ---------------------------------------------------------------------------
# Main upload logic
# ---------------------------------------------------------------------------

def upload(campaign: str, bucket: str, key_json: dict, prefix: str = "") -> None:
    archive_path = f"/data/{campaign}.tar.gz"

    if not os.path.isfile(archive_path):
        sys.stderr.write(f"ERROR: archive not found: {archive_path}\n")
        sys.exit(1)

    total = os.path.getsize(archive_path)
    filename = os.path.basename(archive_path)
    object_name = f"{prefix}{filename}" if prefix else filename

    sys.stdout.write(f"{campaign}  authenticating with GCS...\n")
    sys.stdout.flush()
    token = _get_access_token(key_json)

    sys.stdout.write(f"{campaign}  uploading to gs://{bucket}/{object_name}...\n")
    sys.stdout.flush()

    session_uri, offset = _get_session(campaign, bucket, object_name, total, token)

    if offset == total:
        sys.stdout.write(
            f"{campaign}  already uploaded ({_fmt_size(total)})  ✓\n"
        )
        sys.stdout.flush()
        _delete_session(campaign)
        return

    with open(archive_path, "rb") as fh:
        if offset > 0:
            fh.seek(offset)
        reader = _ProgressReader(fh, total, campaign, start_offset=offset)
        last_byte = total - 1
        req = urllib.request.Request(
            session_uri,
            data=reader,
            method="PUT",
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(total - offset),
                "Content-Range": f"bytes {offset}-{last_byte}/{total}",
            },
        )
        try:
            urllib.request.urlopen(req, timeout=600)
        except urllib.error.HTTPError as exc:
            sys.stdout.write("\n")
            sys.stderr.write(
                f"ERROR: HTTP {exc.code} uploading {filename} to GCS: {exc.reason}\n"
            )
            sys.exit(1)
        except urllib.error.URLError as exc:
            sys.stdout.write("\n")
            sys.stderr.write(f"ERROR: Upload failed: {exc.reason}\n")
            sys.exit(1)

    _delete_session(campaign)
    sys.stdout.write(
        "\r" + f"{campaign}  uploaded ({_fmt_size(total)})  ✓" + CLEAR_EOL + "\n"
    )
    sys.stdout.flush()


def main():
    if len(sys.argv) < 2:
        sys.stderr.write(
            "Usage: python - <campaign_id>  "
            "(ROBOVAST_GCS_BUCKET and ROBOVAST_GCS_KEY_JSON must be set)\n"
        )
        sys.exit(1)

    campaign = sys.argv[1]

    bucket = os.environ.get("ROBOVAST_GCS_BUCKET", "")
    if not bucket:
        sys.stderr.write("ERROR: ROBOVAST_GCS_BUCKET environment variable is not set\n")
        sys.exit(1)

    key_json_str = os.environ.get("ROBOVAST_GCS_KEY_JSON", "")
    if not key_json_str:
        sys.stderr.write(
            "ERROR: ROBOVAST_GCS_KEY_JSON environment variable is not set\n"
        )
        sys.exit(1)

    try:
        key_json = json.loads(key_json_str)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"ERROR: ROBOVAST_GCS_KEY_JSON is not valid JSON: {exc}\n")
        sys.exit(1)

    prefix = os.environ.get("ROBOVAST_GCS_PREFIX", "")

    upload(campaign, bucket, key_json, prefix)


if __name__ == "__main__":
    main()
