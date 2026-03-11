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
Stream S3 bucket contents to a tar.gz file.

Runs inside the archiver sidecar. Connects to the S3 backend, lists all
objects in the given bucket (optionally under a key prefix), streams them into
a gzipped tarball using pigz (parallel gzip) for multi-core compression, and
writes to ``/data/{archive_name}.tar.gz``.

Usage::

    python - <bucket_name> [--prefix <prefix>] [--archive-name <name>]
    python s3_to_targz.py <bucket_name> [--prefix <prefix>]

The ``--prefix`` flag is used in **shared-bucket mode** (e.g. GCS) where all
campaigns live under distinct key prefixes inside a single bucket.  When given,
only objects whose key starts with ``<prefix>/`` are included, and the prefix
is stripped from the archive paths.

The ``--archive-name`` flag overrides the name used for the output tar.gz
(default: ``<bucket_name>.tar.gz``, or ``<prefix>.tar.gz`` when set).

Environment variables (optional):
  S3_ENDPOINT: default http://localhost:9000
  S3_ACCESS_KEY: default minioadmin
  S3_SECRET_KEY: default minioadmin
"""

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
    import argparse  # pylint: disable=import-outside-toplevel
    parser = argparse.ArgumentParser(description="Stream S3 bucket to tar.gz")
    parser.add_argument("bucket", help="S3 bucket name")
    parser.add_argument("--prefix", default=None,
                        help="Only include objects under this key prefix (shared-bucket mode)")
    parser.add_argument("--archive-name", default=None,
                        help="Override output archive name (without .tar.gz)")
    args = parser.parse_args()

    bucket_name = args.bucket
    prefix = args.prefix.rstrip("/") + "/" if args.prefix else None
    archive_label = args.archive_name or (args.prefix or bucket_name)
    output_path = f"/data/{archive_label}.tar.gz"

    s3 = boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=S3_ACCESS_KEY,
        aws_secret_access_key=S3_SECRET_KEY,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )

    paginate_kwargs = {"Bucket": bucket_name}
    if prefix:
        paginate_kwargs["Prefix"] = prefix

    paginator = s3.get_paginator("list_objects_v2")

    # Stream uncompressed tar into pigz for parallel multi-core compression.
    with open(output_path, "wb") as out_f:
        pigz = subprocess.Popen(
            ["pigz", "-c"],
            stdin=subprocess.PIPE,
            stdout=out_f,
        )
        try:
            with tarfile.open(fileobj=pigz.stdin, mode="w|") as tar:
                for page in paginator.paginate(**paginate_kwargs):
                    for obj in page.get("Contents", []):
                        key = obj["Key"]
                        size = obj["Size"]

                        if prefix:
                            # Strip the prefix, use campaign name as top-level folder.
                            relative_key = key[len(prefix):]
                            tar_name = f"{archive_label}/{relative_key}"
                        else:
                            # Use bucket name as top-level folder in the archive.
                            tar_name = f"{bucket_name}/{key}"

                        tarinfo = tarfile.TarInfo(name=tar_name)
                        tarinfo.size = size
                        response = s3.get_object(Bucket=bucket_name, Key=key)
                        if response.get("Metadata", {}).get("executable") == "yes":
                            tarinfo.mode = 0o755
                        else:
                            tarinfo.mode = 0o644
                        tar.addfile(tarinfo, response["Body"])
        finally:
            pigz.stdin.close()
            pigz.wait()

    if pigz.returncode != 0:
        sys.stderr.write(f"pigz exited with code {pigz.returncode}\n")
        sys.exit(pigz.returncode)


if __name__ == "__main__":
    main()
