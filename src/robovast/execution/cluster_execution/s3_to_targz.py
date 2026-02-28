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

Runs inside the archiver sidecar. Connects to MinIO at localhost:9000,
lists all objects in the given bucket, streams them into a gzipped tarball,
and writes to /data/{bucket_name}.tar.gz.

Usage: python - run-xxx  (script from stdin, bucket name from argv)
  or:  python s3_to_targz.py run-xxx

Environment variables (optional):
  S3_ENDPOINT: default http://localhost:9000
  S3_ACCESS_KEY: default minioadmin
  S3_SECRET_KEY: default minioadmin
"""

import os
import sys
import tarfile

import boto3
from botocore.config import Config

S3_ENDPOINT = os.environ.get("S3_ENDPOINT", "http://localhost:9000")
S3_ACCESS_KEY = os.environ.get("S3_ACCESS_KEY", "minioadmin")
S3_SECRET_KEY = os.environ.get("S3_SECRET_KEY", "minioadmin")


def main():
    if len(sys.argv) < 2:
        sys.stderr.write("Usage: python - <bucket_name> or python s3_to_targz.py <bucket_name>\n")
        sys.exit(1)

    bucket_name = sys.argv[1]
    output_path = f"/data/{bucket_name}.tar.gz"

    s3 = boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=S3_ACCESS_KEY,
        aws_secret_access_key=S3_SECRET_KEY,
        config=Config(signature_version="s3v4"),
    )

    paginator = s3.get_paginator("list_objects_v2")

    with open(output_path, "wb") as f:
        with tarfile.open(fileobj=f, mode="w:gz") as tar:
            for page in paginator.paginate(Bucket=bucket_name):
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    size = obj["Size"]
                    # Use bucket name (run-<id>) as top-level folder in the archive
                    tar_name = f"{bucket_name}/{key}"
                    tarinfo = tarfile.TarInfo(name=tar_name)
                    tarinfo.size = size
                    body = s3.get_object(Bucket=bucket_name, Key=key)["Body"]
                    tar.addfile(tarinfo, body)


if __name__ == "__main__":
    main()
