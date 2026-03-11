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
"""Thin host-side storage lifecycle operations.

Pure functions — no long-lived connection objects.  Each function opens its
own connection (boto3 or ``google.cloud.storage``), performs the operation,
and closes immediately.

For embedded MinIO, a ``kubectl port-forward`` is opened for the duration of
the call and torn down afterwards.  For external S3 / GCS the connection is
direct.

Public API
----------
* :func:`list_campaigns` — list campaign IDs present in storage.
* :func:`campaign_exists` — check whether a campaign already has data.
* :func:`delete_campaign` — permanently remove a single campaign's data.
* :func:`cleanup_campaigns` — remove one or all campaigns, return count.
"""

import contextlib
import logging
import socket
import subprocess
import time
from typing import Optional

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from robovast.common.execution import is_campaign_dir

logger = logging.getLogger(__name__)

_S3_PORT = 9000
_GCS_BATCH_SIZE = 100


# ---------------------------------------------------------------------------
# Internal: connection helpers
# ---------------------------------------------------------------------------

def _find_available_port(start_port: int = 18080, max_attempts: int = 100) -> int:
    for port in range(start_port, start_port + max_attempts):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("localhost", port))
                return port
        except OSError:
            continue
    raise RuntimeError(
        f"No available port in range {start_port}–{start_port + max_attempts}"
    )


@contextlib.contextmanager
def _s3_connection(cluster_config, namespace: str, context: Optional[str]):
    """Yield a boto3 S3 client, opening a port-forward for embedded MinIO."""
    uses_embedded = cluster_config.uses_embedded_s3()
    access_key, secret_key = cluster_config.get_s3_credentials()
    region = cluster_config.get_s3_region()
    endpoint = cluster_config.get_host_s3_endpoint()

    pf_proc = None
    if uses_embedded:
        local_port = _find_available_port()
        endpoint = f"http://localhost:{local_port}"
        ctx_args = ["--context", context] if context else []
        cmd = (
            ["kubectl"] + ctx_args + [
                "port-forward", "-n", namespace,
                "pod/robovast", f"{local_port}:{_S3_PORT}",
            ]
        )
        pf_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(20):
            time.sleep(0.5)
            try:
                with socket.create_connection(("localhost", local_port), timeout=1):
                    break
            except OSError:
                continue
        else:
            pf_proc.terminate()
            raise RuntimeError("kubectl port-forward did not become ready in time")

    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
        region_name=region,
    )
    try:
        yield s3
    finally:
        if pf_proc is not None:
            pf_proc.terminate()
            try:
                pf_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pf_proc.kill()


@contextlib.contextmanager
def _gcs_connection(cluster_config):
    """Yield a ``google.cloud.storage.Client`` using the config's key file."""
    from google.cloud import storage as gcs_storage  # pylint: disable=import-outside-toplevel

    key_file = cluster_config.get_gcs_key_file()
    if key_file:
        gcs = gcs_storage.Client.from_service_account_json(key_file)
    else:
        gcs = gcs_storage.Client()
    try:
        yield gcs
    finally:
        pass  # google.cloud.storage.Client has no explicit close


def _is_gcs(cluster_config) -> bool:
    return cluster_config.get_storage_backend() == "gcs"


def _delete_s3_prefix(s3, bucket: str, prefix: str) -> None:
    """Delete all objects in *bucket* whose key starts with *prefix*."""
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        objects = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if objects:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": objects})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_campaigns(
    cluster_config,
    namespace: str = "default",
    context: Optional[str] = None,
) -> list:
    """Return a sorted list of campaign IDs present in storage.

    Args:
        cluster_config: :class:`~robovast.execution.cluster_config.base_config.BaseConfig`
            instance.
        namespace:      Kubernetes namespace (used only for embedded MinIO
                        port-forward).
        context:        Kubernetes context (or ``None`` for the active context).

    Returns:
        list[str]: Sorted campaign IDs.
    """
    if _is_gcs(cluster_config):
        gcs_bucket = cluster_config.get_s3_bucket()
        with _gcs_connection(cluster_config) as gcs:
            blobs_iter = gcs.list_blobs(gcs_bucket, delimiter="/")
            _ = list(blobs_iter)  # consume so blobs_iter.prefixes is populated
            return sorted(
                p.rstrip("/")
                for p in blobs_iter.prefixes
                if is_campaign_dir(p.rstrip("/"))
            )

    shared_bucket = cluster_config.get_s3_bucket()
    with _s3_connection(cluster_config, namespace, context) as s3:
        if shared_bucket:
            paginator = s3.get_paginator("list_objects_v2")
            campaigns: set = set()
            for page in paginator.paginate(Bucket=shared_bucket, Delimiter="/"):
                for prefix_obj in page.get("CommonPrefixes", []):
                    prefix = prefix_obj["Prefix"].rstrip("/")
                    if is_campaign_dir(prefix):
                        campaigns.add(prefix)
            return sorted(campaigns)
        else:
            response = s3.list_buckets()
            return sorted(
                b["Name"]
                for b in response.get("Buckets", [])
                if is_campaign_dir(b["Name"])
            )


def campaign_exists(
    campaign_id: str,
    cluster_config,
    namespace: str = "default",
    context: Optional[str] = None,
) -> bool:
    """Return ``True`` if *campaign_id* already has data in storage.

    Args:
        campaign_id:    Campaign identifier.
        cluster_config: :class:`~robovast.execution.cluster_config.base_config.BaseConfig`.
        namespace:      Kubernetes namespace.
        context:        Kubernetes context.

    Returns:
        bool
    """
    if _is_gcs(cluster_config):
        gcs_bucket = cluster_config.get_s3_bucket()
        with _gcs_connection(cluster_config) as gcs:
            blobs = gcs.list_blobs(gcs_bucket, prefix=f"{campaign_id}/", max_results=1)
            return any(True for _ in blobs)

    shared_bucket = cluster_config.get_s3_bucket()
    with _s3_connection(cluster_config, namespace, context) as s3:
        if shared_bucket:
            response = s3.list_objects_v2(
                Bucket=shared_bucket,
                Prefix=f"{campaign_id}/",
                MaxKeys=1,
            )
            return response.get("KeyCount", 0) > 0
        else:
            try:
                s3.head_bucket(Bucket=campaign_id)
                return True
            except ClientError as exc:
                code = exc.response["Error"]["Code"]
                if code in ("404", "NoSuchBucket"):
                    return False
                raise


def delete_campaign(
    campaign_id: str,
    cluster_config,
    namespace: str = "default",
    context: Optional[str] = None,
) -> None:
    """Permanently delete all data for *campaign_id* from storage.

    In shared-bucket mode (external S3 / GCS) only the campaign's key prefix
    is deleted; the shared bucket itself is left intact.  In per-bucket mode
    (embedded MinIO) the entire bucket is deleted.

    Args:
        campaign_id:    Campaign identifier.
        cluster_config: :class:`~robovast.execution.cluster_config.base_config.BaseConfig`.
        namespace:      Kubernetes namespace.
        context:        Kubernetes context.
    """
    if _is_gcs(cluster_config):
        gcs_bucket = cluster_config.get_s3_bucket()
        with _gcs_connection(cluster_config) as gcs:
            prefix = f"{campaign_id}/"
            blobs = list(gcs.list_blobs(gcs_bucket, prefix=prefix))
            for i in range(0, len(blobs), _GCS_BATCH_SIZE):
                chunk = blobs[i : i + _GCS_BATCH_SIZE]
                with gcs.batch():
                    for blob in chunk:
                        blob.delete()
        logger.debug("Deleted GCS prefix '%s/' from bucket '%s'", campaign_id, gcs_bucket)
        return

    shared_bucket = cluster_config.get_s3_bucket()
    with _s3_connection(cluster_config, namespace, context) as s3:
        if shared_bucket:
            _delete_s3_prefix(s3, shared_bucket, f"{campaign_id}/")
            logger.debug(
                "Deleted prefix '%s/' from shared S3 bucket '%s'", campaign_id, shared_bucket
            )
        else:
            paginator = s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=campaign_id):
                objects = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
                if objects:
                    s3.delete_objects(Bucket=campaign_id, Delete={"Objects": objects})
            s3.delete_bucket(Bucket=campaign_id)
            logger.debug("Deleted S3 bucket '%s'", campaign_id)


def cleanup_campaigns(
    cluster_config,
    namespace: str = "default",
    context: Optional[str] = None,
    campaign_id: Optional[str] = None,
) -> int:
    """Remove one or all campaigns from storage.

    Args:
        cluster_config: :class:`~robovast.execution.cluster_config.base_config.BaseConfig`.
        namespace:      Kubernetes namespace.
        context:        Kubernetes context.
        campaign_id:    If given, remove only this campaign.  If ``None``,
                        remove all campaigns returned by :func:`list_campaigns`.

    Returns:
        int: Number of campaigns successfully removed.
    """
    all_campaigns = list_campaigns(cluster_config, namespace, context)
    if campaign_id:
        to_remove = [c for c in all_campaigns if c == campaign_id]
        if not to_remove:
            logger.info("No campaign matching '%s' found in storage.", campaign_id)
            return 0
    else:
        to_remove = all_campaigns

    if not to_remove:
        logger.info("No campaigns found to remove.")
        return 0

    removed = 0
    for name in to_remove:
        try:
            delete_campaign(name, cluster_config, namespace, context)
            removed += 1
            logger.info("Removed campaign '%s' from storage.", name)
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Failed to remove campaign '%s': %s", name, exc)
    return removed
