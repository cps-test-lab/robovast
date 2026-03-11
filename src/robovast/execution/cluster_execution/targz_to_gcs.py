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
Extract a tar.gz archive and upload its contents to a GCS bucket.

Runs inside the archiver sidecar.  This is the GCP-native counterpart of
:mod:`~robovast.execution.cluster_execution.targz_to_s3`: instead of using
the S3-compatible boto3 path it uploads directly via the GCS XML API with a
short-lived Bearer token obtained from a service-account key.

This avoids the S3-compatibility limitations (e.g. the lack of a
``DeleteObjects`` bulk API) and works reliably from inside a GKE pod that has
egress to ``storage.googleapis.com``.

The tar.gz is decompressed with ``pigz`` (parallel gzip) and each member is
uploaded to the target GCS bucket, preserving the relative path as the object
name.  Files with the UNIX executable bit set receive the custom metadata
attribute ``x-goog-meta-executable: yes`` (matching the convention used by
``ClusterS3Client`` / ``GcsStorageClient``).

Usage::

    python targz_to_gcs.py <bucket_name> <archive_path> [--prefix <prefix>]
    python - <bucket_name> <archive_path> [--prefix <prefix>]

The ``--prefix`` flag is used in **shared-bucket mode** where all campaigns
share a single bucket.  When set, uploaded object names are prefixed with
``<prefix>/`` so that each campaign occupies its own key namespace.

Environment variables (required):
  ROBOVAST_GCS_KEY_JSON:  Service-account key as a JSON string (injected by the host)

Environment variables (optional):
  ROBOVAST_GCS_WORKERS:   Number of parallel upload threads (default: 1, streaming mode)
"""

import io
import json
import os
import socket
import subprocess
import sys
import tarfile
import urllib.error
import urllib.parse
import urllib.request

GCS_KEY_JSON = os.environ.get("ROBOVAST_GCS_KEY_JSON", "")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _get_access_token(key_json: dict) -> str:
    """Exchange a service-account key dict for a short-lived Bearer token."""
    try:
        import google.auth.transport.requests  # noqa: PLC0415
        import google.oauth2.service_account  # noqa: PLC0415
    except ImportError as exc:
        sys.stderr.write(f"ERROR: google-auth is not installed: {exc}\n")
        sys.exit(1)

    scopes = ["https://www.googleapis.com/auth/devstorage.read_write"]
    creds = google.oauth2.service_account.Credentials.from_service_account_info(
        key_json, scopes=scopes
    )
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


# ---------------------------------------------------------------------------
# Single-object upload
# ---------------------------------------------------------------------------

def _upload_blob(
    bucket: str,
    object_name: str,
    data: bytes,
    token: str,
    executable: bool = False,
) -> None:
    """Upload *data* to *bucket*/*object_name* via the GCS XML API.

    Args:
        bucket:      GCS bucket name.
        object_name: Object name (key) within the bucket.
        data:        Raw bytes to upload.
        token:       OAuth2 Bearer token.
        executable:  When True, sets the ``x-goog-meta-executable: yes``
                     custom metadata attribute.
    """
    bucket_enc = urllib.parse.quote(bucket, safe="")
    object_enc = urllib.parse.quote(object_name, safe="")
    url = f"https://storage.googleapis.com/{bucket_enc}/{object_enc}"

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/octet-stream",
    }
    if executable:
        headers["x-goog-meta-executable"] = "yes"

    req = urllib.request.Request(url, data=data, method="PUT", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120):
            pass
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"GCS upload failed for {object_name!r}: HTTP {exc.code} {exc.reason}"
        ) from exc


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    import argparse  # pylint: disable=import-outside-toplevel

    parser = argparse.ArgumentParser(description="Upload tar.gz contents to GCS")
    parser.add_argument("bucket", help="GCS bucket name")
    parser.add_argument("archive", help="Path to the tar.gz archive")
    parser.add_argument(
        "--prefix", default=None,
        help="Prepend this prefix to all object names (shared-bucket mode)",
    )
    args = parser.parse_args()

    bucket_name = args.bucket
    archive_path = args.archive
    prefix = args.prefix.rstrip("/") + "/" if args.prefix else ""

    key_json_str = GCS_KEY_JSON
    if not key_json_str:
        sys.stderr.write("ERROR: ROBOVAST_GCS_KEY_JSON environment variable is not set.\n")
        sys.exit(1)

    try:
        key_json = json.loads(key_json_str)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"ERROR: ROBOVAST_GCS_KEY_JSON is not valid JSON: {exc}\n")
        sys.exit(1)

    sys.stderr.write(
        f"[targz_to_gcs] bucket={bucket_name!r}  prefix={prefix!r}  archive={archive_path!r}\n"
    )
    sys.stderr.flush()

    if not os.path.isfile(archive_path):
        sys.stderr.write(f"Archive not found: {archive_path}\n")
        sys.exit(1)

    # Hard cap on ALL socket operations (DNS + TCP connect + reads) so the
    # process cannot hang indefinitely when the pod lacks egress to GCS.
    socket.setdefaulttimeout(30)

    sys.stderr.write("[targz_to_gcs] authenticating with GCS...\n")
    sys.stderr.flush()
    token = _get_access_token(key_json)
    sys.stderr.write("[targz_to_gcs] authenticated, starting upload...\n")
    sys.stderr.flush()

    # Decompress with pigz (parallel, multi-core) and pipe into tar reader.
    with open(archive_path, "rb") as archive_fh:
        pigz = subprocess.Popen(
            ["pigz", "-d", "-c"],
            stdin=archive_fh,
            stdout=subprocess.PIPE,
        )

        uploaded = 0
        try:
            with tarfile.open(fileobj=pigz.stdout, mode="r|") as tar:
                for member in tar:
                    if not member.isfile():
                        continue
                    fileobj = tar.extractfile(member)
                    if fileobj is None:
                        continue

                    gcs_key = prefix + member.name
                    executable = bool(member.mode & 0o111)

                    # Read into bytes — streaming tar members are not seekable.
                    data = fileobj.read()
                    try:
                        _upload_blob(bucket_name, gcs_key, data, token, executable=executable)
                    except Exception as exc:
                        sys.stderr.write(
                            f"\n[targz_to_gcs] UPLOAD FAILED for key {gcs_key!r}: {exc}\n"
                        )
                        sys.stderr.flush()
                        raise
                    uploaded += 1
                    sys.stderr.write(f"\ruploaded {uploaded} files")
                    sys.stderr.flush()
        finally:
            pigz.wait()

    if pigz.returncode != 0:
        sys.stderr.write(f"\npigz exited with code {pigz.returncode}\n")
        sys.exit(pigz.returncode)

    sys.stderr.write(f"\ruploaded {uploaded} files (done)\n")
    # Print final count on stdout for the caller to parse
    sys.stdout.write(f"{uploaded}\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
