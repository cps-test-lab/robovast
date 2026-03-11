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
Extract a tar.gz archive and upload its contents to an S3 bucket.

Runs inside the archiver sidecar.  This is the inverse of
:mod:`~robovast.execution.cluster_execution.s3_to_targz`: instead of
streaming S3 → tar.gz it streams tar.gz → S3.

The tar.gz is decompressed with ``pigz`` (parallel gzip) and each member
is uploaded to the target S3 bucket, preserving the relative path as the
S3 key.  Files with the UNIX executable bit set get an ``executable=yes``
metadata tag (matching the convention used by ``ClusterS3Client``).

Usage::

    python targz_to_s3.py <bucket_name> <archive_path> [--prefix <prefix>]
    python - <bucket_name> <archive_path> [--prefix <prefix>]

The ``--prefix`` flag is used in **shared-bucket mode** (e.g. GCS) where
all campaigns share a single bucket.  When set, uploaded keys are prefixed
with ``<prefix>/`` so that each campaign occupies its own key namespace.

Environment variables (optional):
  S3_ENDPOINT:   default http://localhost:9000
  S3_ACCESS_KEY: default minioadmin
  S3_SECRET_KEY: default minioadmin
"""

import io
import os
import socket
import subprocess
import sys
import tarfile

import boto3
from botocore.config import Config

S3_ENDPOINT = os.environ.get("S3_ENDPOINT", "http://localhost:9000")
S3_ACCESS_KEY = os.environ.get("S3_ACCESS_KEY", "minioadmin")
S3_SECRET_KEY = os.environ.get("S3_SECRET_KEY", "minioadmin")
S3_REGION = os.environ.get("S3_REGION", "us-east-1")


def main():
    import argparse  # pylint: disable=import-outside-toplevel
    parser = argparse.ArgumentParser(description="Upload tar.gz contents to S3")
    parser.add_argument("bucket", help="S3 bucket name")
    parser.add_argument("archive", help="Path to the tar.gz archive")
    parser.add_argument("--prefix", default=None,
                        help="Prepend this prefix to all S3 keys (shared-bucket mode)")
    args = parser.parse_args()

    bucket_name = args.bucket
    archive_path = args.archive
    prefix = args.prefix.rstrip("/") + "/" if args.prefix else ""

    # Debug: print resolved credentials and endpoint so hangs can be diagnosed.
    masked_secret = (S3_SECRET_KEY[:4] + "****") if len(S3_SECRET_KEY) > 4 else "****"
    sys.stderr.write(
        f"[targz_to_s3] endpoint={S3_ENDPOINT!r}  region={S3_REGION!r}\n"
        f"[targz_to_s3] access_key={S3_ACCESS_KEY!r}  secret_key={masked_secret!r}\n"
        f"[targz_to_s3] bucket={bucket_name!r}  prefix={prefix!r}  archive={archive_path!r}\n"
    )
    sys.stderr.flush()

    if not os.path.isfile(archive_path):
        sys.stderr.write(f"Archive not found: {archive_path}\n")
        sys.exit(1)

    # Hard cap on ALL socket operations (DNS + TCP connect + reads) so the
    # process cannot hang indefinitely when the pod lacks egress to GCS.
    socket.setdefaulttimeout(30)

    s3 = boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=S3_ACCESS_KEY,
        aws_secret_access_key=S3_SECRET_KEY,
        region_name=S3_REGION,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            connect_timeout=10,
            read_timeout=60,
            retries={"max_attempts": 1},
        ),
    )
    sys.stderr.write("[targz_to_s3] S3 client created, testing connectivity...\n")
    sys.stderr.flush()

    # Pre-flight check: a lightweight request that will time out quickly if
    # storage.googleapis.com is not reachable from inside the pod.
    try:
        s3.head_bucket(Bucket=bucket_name)
        sys.stderr.write(f"[targz_to_s3] bucket reachable: {bucket_name!r}\n")
    except s3.exceptions.NoSuchBucket:
        sys.stderr.write(f"[targz_to_s3] bucket absent — creating {bucket_name!r}\n")
        sys.stderr.flush()
        s3.create_bucket(Bucket=bucket_name)
        sys.stderr.write(f"[targz_to_s3] bucket created: {bucket_name!r}\n")
    except Exception as exc:
        sys.stderr.write(
            f"[targz_to_s3] PRE-FLIGHT FAILED — cannot reach {S3_ENDPOINT!r}: {exc}\n"
            f"[targz_to_s3] Check that the pod has egress access to storage.googleapis.com\n"
        )
        sys.stderr.flush()
        sys.exit(1)

    sys.stderr.write("[targz_to_s3] starting upload...\n")
    sys.stderr.flush()

    # The bucket must already exist (created by the caller via port-forward).
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

                    s3_key = prefix + member.name
                    extra_args = {}
                    if member.mode & 0o111:
                        extra_args["Metadata"] = {"executable": "yes"}

                    # Read into BytesIO because streaming tar members
                    # are not seekable, which boto3 requires.
                    buf = io.BytesIO(fileobj.read())
                    try:
                        s3.upload_fileobj(
                            buf, bucket_name, s3_key,
                            ExtraArgs=extra_args if extra_args else None,
                        )
                    except Exception as exc:
                        sys.stderr.write(
                            f"\n[targz_to_s3] UPLOAD FAILED for key {s3_key!r}: {exc}\n"
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
