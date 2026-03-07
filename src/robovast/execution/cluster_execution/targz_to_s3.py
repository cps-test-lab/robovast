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

Usage: python targz_to_s3.py <bucket_name> <archive_path>
  or:  python - <bucket_name> <archive_path>   (script piped via stdin)

Environment variables (optional):
  S3_ENDPOINT:   default http://localhost:9000
  S3_ACCESS_KEY: default minioadmin
  S3_SECRET_KEY: default minioadmin
"""

import io
import os
import subprocess
import sys
import tarfile

import boto3
from botocore.config import Config

S3_ENDPOINT = os.environ.get("S3_ENDPOINT", "http://localhost:9000")
S3_ACCESS_KEY = os.environ.get("S3_ACCESS_KEY", "minioadmin")
S3_SECRET_KEY = os.environ.get("S3_SECRET_KEY", "minioadmin")


def main():
    if len(sys.argv) < 3:
        sys.stderr.write(
            "Usage: python targz_to_s3.py <bucket_name> <archive_path>\n"
        )
        sys.exit(1)

    bucket_name = sys.argv[1]
    archive_path = sys.argv[2]

    if not os.path.isfile(archive_path):
        sys.stderr.write(f"Archive not found: {archive_path}\n")
        sys.exit(1)

    s3 = boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=S3_ACCESS_KEY,
        aws_secret_access_key=S3_SECRET_KEY,
        config=Config(signature_version="s3v4"),
    )

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

                    s3_key = member.name
                    extra_args = {}
                    if member.mode & 0o111:
                        extra_args["Metadata"] = {"executable": "yes"}

                    # Read into BytesIO because streaming tar members
                    # are not seekable, which boto3 requires.
                    buf = io.BytesIO(fileobj.read())
                    s3.upload_fileobj(
                        buf, bucket_name, s3_key,
                        ExtraArgs=extra_args if extra_args else None,
                    )
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
