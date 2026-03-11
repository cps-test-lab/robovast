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
Download a GCS bucket prefix in parallel, then compress to tar.gz.

Phase 1 — parallel download: all objects under <campaign>/ are fetched
           concurrently using a thread pool (default: 16 workers) and
           written to a temporary directory inside /data/.
Phase 2 — compress: ``tar cf - | pigz`` converts the temp dir into
           ``/data/<campaign>.tar.gz`` using all available CPU cores.

This two-phase approach is significantly faster than single-threaded streaming
because network I/O is parallelised and pigz can use all cores for compression.

Usage::

    python - <campaign>   (script from stdin)
    python gcs_to_targz.py <campaign>

Environment variables:
  ROBOVAST_GCS_BUCKET:   GCS bucket name (e.g. my-robovast-results)
  ROBOVAST_GCS_KEY_JSON: Service-account key as a JSON string
  ROBOVAST_GCS_WORKERS:  Number of parallel download threads (default: 16)
"""

import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request

DOWNLOAD_WORKERS = int(os.environ.get("ROBOVAST_GCS_WORKERS", "16"))
_CHUNK = 4 * 1024 * 1024  # 4 MiB per read chunk


# ---------------------------------------------------------------------------
# GCS auth
# ---------------------------------------------------------------------------

def _get_access_token(key_json: dict) -> str:
    """Exchange a service-account key dict for a short-lived Bearer token."""
    try:
        import google.auth.transport.requests  # noqa: PLC0415
        import google.oauth2.service_account  # noqa: PLC0415
    except ImportError as exc:
        sys.stderr.write(f"ERROR: google-auth is not installed: {exc}\n")
        sys.exit(1)

    scopes = ["https://www.googleapis.com/auth/devstorage.read_only"]
    creds = google.oauth2.service_account.Credentials.from_service_account_info(
        key_json, scopes=scopes
    )
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


# ---------------------------------------------------------------------------
# GCS list
# ---------------------------------------------------------------------------

def _list_blobs(bucket: str, prefix: str, token: str) -> list:
    """Return a list of ``(name, size)`` tuples for all objects under *prefix*."""
    blobs = []
    page_token = None

    while True:
        params: dict = {"prefix": prefix}
        if page_token:
            params["pageToken"] = page_token

        url = (
            f"https://storage.googleapis.com/storage/v1/b/"
            f"{urllib.parse.quote(bucket, safe='')}/o"
            f"?{urllib.parse.urlencode(params)}"
        )
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as exc:
            sys.stderr.write(
                f"ERROR: GCS list failed for bucket '{bucket}' prefix '{prefix}': "
                f"HTTP {exc.code} {exc.reason}\n"
            )
            sys.exit(1)

        for item in data.get("items", []):
            blobs.append((item["name"], int(item.get("size", 0))))

        next_page = data.get("nextPageToken")
        if not next_page:
            break
        page_token = next_page

    return blobs


# ---------------------------------------------------------------------------
# Single blob download
# ---------------------------------------------------------------------------

def _download_blob(bucket: str, blob_name: str, dest_path: str, token: str) -> None:
    """Download one GCS object to *dest_path*, streaming in 4 MiB chunks."""
    url = (
        f"https://storage.googleapis.com/storage/v1/b/"
        f"{urllib.parse.quote(bucket, safe='')}/o/"
        f"{urllib.parse.quote(blob_name, safe='')}"
        f"?alt=media"
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    with urllib.request.urlopen(req, timeout=120) as resp:
        with open(dest_path, "wb") as fh:
            while True:
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                fh.write(chunk)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    import argparse  # pylint: disable=import-outside-toplevel

    parser = argparse.ArgumentParser(
        description="Download GCS prefix in parallel, compress to /data/<campaign>.tar.gz"
    )
    parser.add_argument("campaign", help="Campaign ID (GCS prefix and archive name)")
    args = parser.parse_args()

    campaign = args.campaign
    bucket = os.environ.get("ROBOVAST_GCS_BUCKET", "")
    if not bucket:
        sys.stderr.write("ERROR: ROBOVAST_GCS_BUCKET environment variable is not set.\n")
        sys.exit(1)

    key_json_str = os.environ.get("ROBOVAST_GCS_KEY_JSON", "")
    if not key_json_str:
        sys.stderr.write("ERROR: ROBOVAST_GCS_KEY_JSON environment variable is not set.\n")
        sys.exit(1)

    try:
        key_json = json.loads(key_json_str)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"ERROR: ROBOVAST_GCS_KEY_JSON is not valid JSON: {exc}\n")
        sys.exit(1)

    prefix = f"{campaign}/"
    output_path = f"/data/{campaign}.tar.gz"

    token = _get_access_token(key_json)

    blobs = _list_blobs(bucket, prefix, token)
    # Filter out bare "folder" pseudo-objects GCS sometimes emits
    blobs = [(name, size) for name, size in blobs if name != prefix]
    if not blobs:
        sys.stderr.write(
            f"ERROR: No objects found under prefix '{prefix}' in bucket '{bucket}'.\n"
        )
        sys.exit(1)

    total = len(blobs)
    total_bytes = sum(size for _, size in blobs)
    sys.stdout.write(
        f"{campaign}: {total} object(s)  {total_bytes / 1024 / 1024:.1f} MiB"
        f"  ({DOWNLOAD_WORKERS} parallel workers)\n"
    )
    sys.stdout.flush()

    # ------------------------------------------------------------------
    # Phase 1: parallel download into a temp directory on /data/
    # ------------------------------------------------------------------
    tmpdir = tempfile.mkdtemp(dir="/data", prefix=f".gcs_dl_{campaign}_")
    try:
        campaign_dir = os.path.join(tmpdir, campaign)
        os.makedirs(campaign_dir, exist_ok=True)

        done_count = 0
        lock = threading.Lock()

        def _download_one(blob_name_size):
            nonlocal done_count
            blob_name, _size = blob_name_size
            relative = blob_name[len(prefix):]
            if not relative:
                return
            dest = os.path.join(campaign_dir, relative)
            _download_blob(bucket, blob_name, dest, token)
            with lock:
                done_count += 1
                n = done_count
            sys.stdout.write(f"\r{campaign}  downloading {n}/{total}...")
            sys.stdout.flush()

        with concurrent.futures.ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
            futures = {pool.submit(_download_one, b): b for b in blobs}
            for fut in concurrent.futures.as_completed(futures):
                fut.result()  # re-raise any download exception immediately

        sys.stdout.write(
            f"\r{campaign}  downloaded {total} file(s), compressing...\n"
        )
        sys.stdout.flush()

        # ------------------------------------------------------------------
        # Phase 2: tar + pigz running in parallel via OS pipe
        # ------------------------------------------------------------------
        with open(output_path, "wb") as out_f:
            tar_proc = subprocess.Popen(
                ["tar", "cf", "-", "-C", tmpdir, campaign],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            pigz_proc = subprocess.Popen(
                ["pigz", "-c"],
                stdin=tar_proc.stdout,
                stdout=out_f,
                stderr=subprocess.PIPE,
            )
            # Allow tar_proc to receive SIGPIPE if pigz exits early
            tar_proc.stdout.close()
            _pigz_stderr = pigz_proc.communicate()[1]
            tar_proc.wait()

        if tar_proc.returncode != 0:
            sys.stderr.write(f"ERROR: tar exited with code {tar_proc.returncode}\n")
            sys.exit(tar_proc.returncode)
        if pigz_proc.returncode != 0:
            msg = _pigz_stderr.decode(errors="replace").strip()
            sys.stderr.write(
                f"ERROR: pigz exited with code {pigz_proc.returncode}: {msg}\n"
            )
            sys.exit(pigz_proc.returncode)

        sys.stdout.write(f"{campaign}: wrote {output_path}\n")
        sys.stdout.flush()

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
