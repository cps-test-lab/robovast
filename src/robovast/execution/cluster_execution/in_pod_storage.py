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
"""Direct (in-pod) storage I/O for the in-cluster campaign controller.

Unlike :mod:`.archiver` / :mod:`.s3_client` (host-side, ``kubectl cp`` + port-
forward), this module talks to the storage backend **directly from inside the
cluster**, where the S3/GCS endpoint is reachable with full bandwidth. It is
used by :class:`~robovast.execution.cluster_execution.kubernetes_backend.KubernetesBackend`
running in the controller pod to:

* upload each batch's prepared config tree to the campaign's storage prefix, and
* download a finished batch's per-config/run results back for scoring.

The backend (S3 vs GCS), endpoint, credentials and bucket all come from the
reconstructed :class:`~robovast.execution.cluster_config.base_config.BaseConfig`
— the same object the host uses — so there is one source of truth for storage
access ("reuse the cluster's approach"). Buckets/prefixes are passed per call,
since per-batch search runs target different prefixes within a campaign.
"""

import logging
import os
import socket

logger = logging.getLogger(__name__)

# Object metadata flag marking a file as executable, matching the convention in
# targz_to_s3.py / ClusterS3Client so the executable bit survives a round-trip.
_EXECUTABLE_META = {"executable": "yes"}


def _iter_files(local_dir):
    """Yield ``(absolute_path, posix_relative_path)`` for every file under *local_dir*."""
    for root, _dirs, files in os.walk(local_dir):
        for name in files:
            abs_path = os.path.join(root, name)
            rel = os.path.relpath(abs_path, local_dir).replace(os.sep, "/")
            yield abs_path, rel


def _is_executable(path: str) -> bool:
    return bool(os.stat(path).st_mode & 0o111)


class StorageClient:
    """Common interface: upload a local dir to / download a prefix from storage."""

    def upload_dir(self, local_dir: str, bucket: str, prefix: str = "") -> int:
        raise NotImplementedError

    def upload_file(self, local_path: str, bucket: str, key: str) -> None:
        raise NotImplementedError

    def download_prefix(self, bucket: str, prefix: str, local_dir: str) -> int:
        raise NotImplementedError


class _S3StorageClient(StorageClient):
    """boto3-backed client for MinIO / S3 reachable from inside the cluster."""

    def __init__(self, *, endpoint, access_key, secret_key, region):
        import boto3  # pylint: disable=import-outside-toplevel
        from botocore.config import Config  # pylint: disable=import-outside-toplevel

        socket.setdefaulttimeout(120)
        self._s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
                connect_timeout=10,
                read_timeout=120,
                retries={"max_attempts": 3},
            ),
        )

    def _ensure_bucket(self, bucket: str):
        from botocore.exceptions import ClientError  # pylint: disable=import-outside-toplevel
        try:
            self._s3.head_bucket(Bucket=bucket)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchBucket"):
                self._s3.create_bucket(Bucket=bucket)
            else:
                raise

    def upload_dir(self, local_dir: str, bucket: str, prefix: str = "") -> int:
        self._ensure_bucket(bucket)
        prefix = prefix.rstrip("/")
        count = 0
        for abs_path, rel in _iter_files(local_dir):
            key = f"{prefix}/{rel}" if prefix else rel
            extra = {"Metadata": dict(_EXECUTABLE_META)} if _is_executable(abs_path) else None
            self._s3.upload_file(abs_path, bucket, key, ExtraArgs=extra)
            count += 1
        logger.debug("Uploaded %d files to s3://%s/%s", count, bucket, prefix)
        return count

    def upload_file(self, local_path: str, bucket: str, key: str) -> None:
        self._ensure_bucket(bucket)
        extra = {"Metadata": dict(_EXECUTABLE_META)} if _is_executable(local_path) else None
        self._s3.upload_file(local_path, bucket, key, ExtraArgs=extra)

    def download_prefix(self, bucket: str, prefix: str, local_dir: str) -> int:
        prefix = prefix.rstrip("/")
        key_prefix = f"{prefix}/" if prefix else ""
        paginator = self._s3.get_paginator("list_objects_v2")
        count = 0
        for page in paginator.paginate(Bucket=bucket, Prefix=key_prefix):
            for obj in page.get("Contents", []) or []:
                key = obj["Key"]
                rel = key[len(key_prefix):] if key_prefix else key
                if not rel or key.endswith("/"):
                    continue
                dst = os.path.join(local_dir, *rel.split("/"))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                self._s3.download_file(bucket, key, dst)
                head = self._s3.head_object(Bucket=bucket, Key=key)
                if (head.get("Metadata") or {}).get("executable") == "yes":
                    os.chmod(dst, os.stat(dst).st_mode | 0o111)
                count += 1
        logger.debug("Downloaded %d files from s3://%s/%s", count, bucket, prefix)
        return count


class _GcsStorageClient(StorageClient):
    """google-cloud-storage client for a shared GCS bucket (prefix per campaign)."""

    def __init__(self, *, key_json: str):
        import json  # pylint: disable=import-outside-toplevel

        from google.cloud import storage  # pylint: disable=import-outside-toplevel
        from google.oauth2 import \
            service_account  # pylint: disable=import-outside-toplevel

        info = json.loads(key_json)
        creds = service_account.Credentials.from_service_account_info(info)
        self._client = storage.Client(project=info.get("project_id"), credentials=creds)

    def upload_dir(self, local_dir: str, bucket: str, prefix: str = "") -> int:
        gbucket = self._client.bucket(bucket)
        prefix = prefix.rstrip("/")
        count = 0
        for abs_path, rel in _iter_files(local_dir):
            name = f"{prefix}/{rel}" if prefix else rel
            blob = gbucket.blob(name)
            if _is_executable(abs_path):
                blob.metadata = dict(_EXECUTABLE_META)
            blob.upload_from_filename(abs_path)
            count += 1
        logger.debug("Uploaded %d files to gs://%s/%s", count, bucket, prefix)
        return count

    def upload_file(self, local_path: str, bucket: str, key: str) -> None:
        blob = self._client.bucket(bucket).blob(key)
        if _is_executable(local_path):
            blob.metadata = dict(_EXECUTABLE_META)
        blob.upload_from_filename(local_path)

    def download_prefix(self, bucket: str, prefix: str, local_dir: str) -> int:
        gbucket = self._client.bucket(bucket)
        prefix = prefix.rstrip("/")
        key_prefix = f"{prefix}/" if prefix else ""
        count = 0
        for blob in self._client.list_blobs(gbucket, prefix=key_prefix):
            rel = blob.name[len(key_prefix):] if key_prefix else blob.name
            if not rel or blob.name.endswith("/"):
                continue
            dst = os.path.join(local_dir, *rel.split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            blob.download_to_filename(dst)
            if (blob.metadata or {}).get("executable") == "yes":
                os.chmod(dst, os.stat(dst).st_mode | 0o111)
            count += 1
        logger.debug("Downloaded %d files from gs://%s/%s", count, bucket, prefix)
        return count


def campaign_storage_location(cluster_config, campaign_id: str) -> tuple[str, str]:
    """Return ``(bucket, campaign_prefix)`` for a campaign's storage location.

    * Per-campaign bucket (embedded MinIO): ``(<campaign-bucket>, "")``.
    * Shared bucket (e.g. GCS): ``(<shared-bucket>, "<campaign-bucket>/")``.

    ``campaign_prefix`` has a trailing slash (or is empty). There is no per-batch
    component: batches of one campaign share this flat prefix and are kept apart by
    batch-namespaced job tags, so the layout matches a local campaign.
    """
    shared = cluster_config.get_s3_bucket()
    campaign_bucket = campaign_id.lower().replace("_", "-")
    if shared:
        return shared, f"{campaign_bucket}/"
    return campaign_bucket, ""


def storage_client_for(cluster_config) -> StorageClient:
    """Build a :class:`StorageClient` from a reconstructed cluster config.

    Selects S3 (MinIO) or GCS based on ``cluster_config.get_storage_backend()``,
    using its endpoint / credentials — the same values the host and the job
    init/entrypoint containers use.
    """
    if cluster_config.get_storage_backend() == "gcs":
        return _GcsStorageClient(key_json=cluster_config.get_gcs_key_json())
    access_key, secret_key = cluster_config.get_s3_credentials()
    return _S3StorageClient(
        endpoint=cluster_config.get_s3_endpoint(),
        access_key=access_key,
        secret_key=secret_key,
        region=cluster_config.get_s3_region(),
    )
