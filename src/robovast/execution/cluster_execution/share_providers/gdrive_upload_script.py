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
Upload a run archive to a Google Drive folder via a service account.

Runs inside the archiver sidecar (robovast-archiver image).
google-auth and google-api-python-client are pre-installed in the image.

The archive must already exist at /data/{run_id}.tar.gz.

Usage: python - <run_id>  (script from stdin)
  or:  python gdrive_upload_script.py <run_id>

Environment variables:
  GDRIVE_FOLDER_ID: Google Drive folder ID (target parent folder)
  GDRIVE_SA_JSON:   Contents of the service account JSON key file

Progress lines are written to stdout in the format:
  <run_id>  [████████░░░░░░░░░░░░]  xx.x%  X.X MiB  X.X MiB/s
"""

import json
import os
import sys
import time

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# ---------------------------------------------------------------------------
# Progress bar helpers (match download_results.py style)
# ---------------------------------------------------------------------------
BAR_WIDTH = 20
CLEAR_EOL = "\033[K"
_DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def _fmt_size(n):
    return f"{n / 1024 / 1024:.1f} MiB"


def _fmt_rate(bps):
    if bps >= 1024 * 1024:
        return f"{bps / 1024 / 1024:.1f} MiB/s"
    if bps >= 1024:
        return f"{bps / 1024:.1f} KiB/s"
    return f"{bps:.0f} B/s"


def _render_progress(run_id, sent, total, start_time):
    if total <= 0:
        return
    elapsed = max(time.monotonic() - start_time, 1e-6)
    rate = sent / elapsed
    pct = sent / total * 100
    filled = int(BAR_WIDTH * sent / total)
    bar = "█" * filled + "░" * (BAR_WIDTH - filled)
    line = (
        f"{run_id}  [{bar}]  {pct:5.1f}%  "
        f"{_fmt_size(sent)}/{_fmt_size(total)}  {_fmt_rate(rate)}"
    )
    sys.stdout.write("\r" + line + CLEAR_EOL)
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Google Drive upload with resumable upload + progress
# ---------------------------------------------------------------------------
_CHUNK_SIZE = 5 * 1024 * 1024  # 5 MiB (Drive API minimum chunk for resumable)


def upload(run_id: str, folder_id: str, sa_json_content: str) -> None:
    archive_path = f"/data/{run_id}.tar.gz"

    if not os.path.isfile(archive_path):
        sys.stderr.write(f"ERROR: archive not found: {archive_path}\n")
        sys.exit(1)

    total = os.path.getsize(archive_path)
    filename = os.path.basename(archive_path)

    sa_info = json.loads(sa_json_content)
    credentials = service_account.Credentials.from_service_account_info(
        sa_info, scopes=_DRIVE_SCOPES
    )
    service = build("drive", "v3", credentials=credentials, cache_discovery=False)

    # Check for an existing file with the same name in the target folder.
    query = (
        f"name = {filename!r} and '{folder_id}' in parents and trashed = false"
    )
    existing = service.files().list(
        q=query,
        fields="files(id, name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute().get("files", [])
    if existing:
        sys.stderr.write(
            f"ERROR: {filename} already exists in the target Google Drive folder "
            f"(file id: {existing[0]['id']}).\n"
            "Use --force to overwrite (re-create the archive) or delete the "
            "existing file manually.\n"
        )
        sys.exit(1)

    file_metadata = {"name": filename, "parents": [folder_id]}

    sys.stdout.write(f"{run_id}  uploading to Google Drive...\n")
    sys.stdout.flush()

    start_time = time.monotonic()
    last_pct = -1.0

    with open(archive_path, "rb") as fh:
        media = MediaIoBaseUpload(
            fh,
            mimetype="application/gzip",
            chunksize=_CHUNK_SIZE,
            resumable=True,
        )
        request = service.files().create(
            body=file_metadata, media_body=media, supportsAllDrives=True
        )

        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                sent = int(status.resumable_progress)
                pct = sent / total * 100 if total > 0 else 0
                if pct - last_pct >= 1.0:
                    last_pct = pct
                    _render_progress(run_id, sent, total, start_time)

    _render_progress(run_id, total, total, start_time)
    sys.stdout.write("\r" + f"{run_id}  uploaded ({_fmt_size(total)})  ✓" + CLEAR_EOL + "\n")
    sys.stdout.flush()


def main():
    if len(sys.argv) < 2:
        sys.stderr.write(
            "Usage: python - <run_id>  "
            "(GDRIVE_FOLDER_ID and GDRIVE_SA_JSON must be set)\n"
        )
        sys.exit(1)

    run_id = sys.argv[1]

    folder_id = os.environ.get("GDRIVE_FOLDER_ID", "")
    if not folder_id:
        sys.stderr.write("ERROR: GDRIVE_FOLDER_ID environment variable is not set\n")
        sys.exit(1)

    sa_json = os.environ.get("GDRIVE_SA_JSON", "")
    if not sa_json:
        sys.stderr.write("ERROR: GDRIVE_SA_JSON environment variable is not set\n")
        sys.exit(1)

    upload(run_id, folder_id, sa_json)


if __name__ == "__main__":
    main()
