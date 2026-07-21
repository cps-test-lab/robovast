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

Unlike the host-side ``kubectl cp`` + port-forward tooling, this module talks to
the storage backend **directly from inside the cluster**, where the S3/GCS
endpoint is reachable with full bandwidth. It is
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
import tempfile

logger = logging.getLogger(__name__)

# Object metadata flag marking a file as executable, matching the convention used
# by the upload-to-share compression and the job init containers so the bit
# survives a round-trip.
_EXECUTABLE_META = {"executable": "yes"}


def _iter_files(local_dir):
    """Yield ``(absolute_path, posix_relative_path)`` for every file under *local_dir*.

    Skips **broken symlinks** — an interrupted/stopped campaign can leave a ``job``
    link (``<config>/<run>/job`` → ``_jobs/<batch>/job-<idx>``) whose target was never
    produced, and a dangling link has nothing to upload. ``os.walk`` reports such a
    link as a *file* (its target can't be resolved to a directory), so without this
    guard ``os.stat`` on it would abort the whole upload. A resolvable ``job`` link is
    a directory and is never yielded as a file, so this changes nothing for a complete
    campaign.
    """
    for root, _dirs, files in os.walk(local_dir):
        for name in files:
            abs_path = os.path.join(root, name)
            if not os.path.exists(abs_path):  # follows symlinks; False for a dangling one
                logger.warning("Skipping unreadable path during upload: %s", abs_path)
                continue
            rel = os.path.relpath(abs_path, local_dir).replace(os.sep, "/")
            yield abs_path, rel


def _is_executable(path: str) -> bool:
    try:
        return bool(os.stat(path).st_mode & 0o111)
    except OSError:
        # Raced with a vanishing/dangling path; not executable, and not fatal.
        return False


def _same_size(path: str, size) -> bool:
    """Whether *path* already exists locally with byte size *size*.

    Used to skip re-downloading immutable objects. ``size`` is ``None`` when the
    listing did not report one, in which case we can't compare and re-fetch.
    """
    if size is None:
        return False
    try:
        return os.path.getsize(path) == size
    except OSError:
        return False


def _download_atomic(dst: str, fetch) -> None:
    """Fetch to a unique temp file beside *dst*, then atomically ``os.replace`` it in.

    Several service requests can race to populate the same cache dir — the results
    explorer fires one ``FROM runs`` query per sub-view on first load, and each
    re-fetches the campaign. Writing straight to *dst* lets one request open a
    half-written ``data.db`` that another is still streaming; SQLite then reports
    "no such table: runs" until the next reload. Renaming a fully-written temp file
    over *dst* is atomic on a POSIX filesystem, so a reader always sees either the
    previous complete file or the new complete file, never a partial one.

    *fetch* is a callable that writes the object to the path it is given.
    """
    d = os.path.dirname(dst)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".dl-", suffix=".part")
    os.close(fd)
    try:
        fetch(tmp)
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


class StorageClient:
    """Common interface: upload a local dir to / download a prefix from storage."""

    def upload_dir(self, local_dir: str, bucket: str, prefix: str = "") -> int:
        raise NotImplementedError

    def upload_file(self, local_path: str, bucket: str, key: str) -> None:
        raise NotImplementedError

    def download_prefix(self, bucket: str, prefix: str, local_dir: str,
                        force: bool = False, on_file=None) -> int:
        """Download every object under *prefix* into *local_dir*.

        Objects in the durable home are immutable, so by default a file that
        already exists locally with a matching size is left untouched — this
        makes repeat fetches (e.g. re-rendering a campaign's notebooks) a
        near-noop instead of a full re-download. Pass ``force=True`` to
        overwrite unconditionally (used after re-postprocessing, which mutates
        objects in place).

        *on_file*, if given, is a no-argument callable invoked once per file
        actually fetched (skipped same-size files don't count) — used by the
        controller to log download progress during an otherwise-silent fetch of
        a large batch.
        """
        raise NotImplementedError

    def read_object(self, bucket: str, key: str) -> "bytes | None":
        """Return a single object's bytes, or ``None`` if it does not exist.

        A targeted single-key read (vs :meth:`download_prefix`) for small
        control-plane objects like ``_execution/outcome.json`` — the service reads
        just that one key to surface a failed campaign's reason.
        """
        raise NotImplementedError

    def list_keys(self, bucket: str, prefix: str = "") -> list[str]:
        """Return object keys under *prefix* (no trailing-slash pseudo-dirs).

        Used by the controller to count completed per-run artifacts in the live
        batch prefix (run-level progress), so it must not raise on a
        not-yet-created bucket — implementations return ``[]`` in that case.
        """
        raise NotImplementedError


class _S3StorageClient(StorageClient):
    """boto3-backed client for MinIO / S3 reachable from inside the cluster."""

    # boto's own ``retries`` re-run a request against the *same* connection, which
    # is useless when an off-cluster driver's ``kubectl port-forward`` tunnel has
    # gone stalled-but-alive — every attempt then read-times-out identically. On
    # these errors we instead rebuild the client against a freshly-restarted
    # forward (``_reconnect``) and re-run the whole operation, up to this many times.
    _RECONNECT_ATTEMPTS = 3

    def __init__(self, *, endpoint_resolver, access_key, secret_key, region):
        # ``endpoint_resolver(force_reconnect=False)`` returns the host-reachable S3
        # endpoint; passing ``True`` tears down a stalled port-forward and opens a
        # fresh one (see BaseConfig.get_driver_s3_endpoint). In-cluster it is a
        # constant, so reconnecting is a cheap no-op that just rebuilds the client.
        self._endpoint_resolver = endpoint_resolver
        self._access_key = access_key
        self._secret_key = secret_key
        self._region = region
        # The endpoint this client is currently bound to; passed back to the resolver
        # on a forced reconnect so a shared port-forward is torn down only once per
        # stall (see ClusterService._minio_port_forward_endpoint). ``None`` until the
        # first connect, so that first resolve behaves exactly as before.
        self._endpoint = None
        socket.setdefaulttimeout(120)
        self._connect(force_reconnect=False)

    def _connect(self, *, force_reconnect: bool) -> None:
        import boto3  # pylint: disable=import-outside-toplevel
        from botocore.config import Config  # pylint: disable=import-outside-toplevel

        # Tell the resolver which endpoint we were using: concurrent clients that all
        # timed out on the same stalled forward would otherwise each tear down the
        # fresh tunnel a sibling just opened (thundering-herd mutual teardown →
        # "connection refused"). The resolver no-ops a restart that another client
        # already performed since ``self._endpoint`` was issued.
        endpoint = self._endpoint_resolver(force_reconnect, self._endpoint)
        self._endpoint = endpoint
        self._s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=self._access_key,
            aws_secret_access_key=self._secret_key,
            region_name=self._region,
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

        # botocore adds ``Expect: 100-continue`` to uploads, which makes MinIO
        # emit an interim ``HTTP/1.1 100 Continue`` response that urllib3
        # mis-parses ("Failed to parse headers ... MissingHeaderBodySeparatorDefect,
        # unparsed data: 'HTTP/1...'"). Strip the header so each request gets a
        # single response. Registered on ``before-send`` (after signing) so the
        # header is removed regardless of when botocore added it.
        self._s3.meta.events.register("before-send.s3.*", self._strip_expect_header)

    def _resilient(self, op):
        """Run ``op()``, reconnecting to a fresh port-forward on a network timeout.

        The whole operation is re-run after a reconnect, not the single failed
        request — every S3 op here is idempotent (uploads overwrite,
        ``download_prefix`` skips same-size files, reads/lists are pure), so a
        clean re-run is safe and, for downloads, near-free for what already landed.
        """
        from botocore.exceptions import (  # pylint: disable=import-outside-toplevel
            ConnectionClosedError, ConnectTimeoutError, EndpointConnectionError,
            ReadTimeoutError)
        transient = (ReadTimeoutError, ConnectTimeoutError,
                     EndpointConnectionError, ConnectionClosedError)
        last = None
        for attempt in range(self._RECONNECT_ATTEMPTS):
            try:
                return op()
            except transient as exc:
                last = exc
                if attempt + 1 >= self._RECONNECT_ATTEMPTS:
                    break
                logger.warning(
                    "S3 endpoint timed out (%s); restarting port-forward and "
                    "retrying (attempt %d/%d)",
                    type(exc).__name__, attempt + 2, self._RECONNECT_ATTEMPTS)
                self._connect(force_reconnect=True)
        raise last

    @staticmethod
    def _strip_expect_header(request, **kwargs):
        """Drop the ``Expect: 100-continue`` header from an outgoing S3 request.

        Returning ``None`` lets botocore send the request normally.
        """
        request.headers.pop("Expect", None)

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
        def op():
            self._ensure_bucket(bucket)
            clean = prefix.rstrip("/")
            count = 0
            for abs_path, rel in _iter_files(local_dir):
                key = f"{clean}/{rel}" if clean else rel
                extra = ({"Metadata": dict(_EXECUTABLE_META)}
                         if _is_executable(abs_path) else None)
                self._s3.upload_file(abs_path, bucket, key, ExtraArgs=extra)
                count += 1
            logger.debug("Uploaded %d files to s3://%s/%s", count, bucket, clean)
            return count
        return self._resilient(op)

    def upload_file(self, local_path: str, bucket: str, key: str) -> None:
        def op():
            self._ensure_bucket(bucket)
            extra = ({"Metadata": dict(_EXECUTABLE_META)}
                     if _is_executable(local_path) else None)
            self._s3.upload_file(local_path, bucket, key, ExtraArgs=extra)
        self._resilient(op)

    def download_prefix(self, bucket: str, prefix: str, local_dir: str,
                        force: bool = False, on_file=None) -> int:
        clean = prefix.rstrip("/")
        key_prefix = f"{clean}/" if clean else ""

        def op():
            paginator = self._s3.get_paginator("list_objects_v2")
            count = 0
            for page in paginator.paginate(Bucket=bucket, Prefix=key_prefix):
                for obj in page.get("Contents", []) or []:
                    key = obj["Key"]
                    rel = key[len(key_prefix):] if key_prefix else key
                    if not rel or key.endswith("/"):
                        continue
                    dst = os.path.join(local_dir, *rel.split("/"))
                    if not force and _same_size(dst, obj.get("Size")):
                        continue
                    _download_atomic(
                        dst, lambda p, k=key: self._s3.download_file(bucket, k, p))
                    # ``head_object`` (an extra round-trip) only to read the
                    # executable flag — done only for files we actually fetched.
                    head = self._s3.head_object(Bucket=bucket, Key=key)
                    if (head.get("Metadata") or {}).get("executable") == "yes":
                        os.chmod(dst, os.stat(dst).st_mode | 0o111)
                    count += 1
                    if on_file is not None:
                        on_file()
            logger.debug("Downloaded %d files from s3://%s/%s", count, bucket, clean)
            return count
        return self._resilient(op)

    def read_object(self, bucket: str, key: str) -> "bytes | None":
        from botocore.exceptions import ClientError  # pylint: disable=import-outside-toplevel

        def op():
            try:
                return self._s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code in ("404", "NoSuchKey", "NoSuchBucket"):
                    return None
                raise
        return self._resilient(op)

    def list_keys(self, bucket: str, prefix: str = "") -> list[str]:
        from botocore.exceptions import ClientError  # pylint: disable=import-outside-toplevel
        clean = prefix.rstrip("/")
        key_prefix = f"{clean}/" if clean else ""

        def op():
            paginator = self._s3.get_paginator("list_objects_v2")
            keys = []
            try:
                for page in paginator.paginate(Bucket=bucket, Prefix=key_prefix):
                    for obj in page.get("Contents", []) or []:
                        if not obj["Key"].endswith("/"):
                            keys.append(obj["Key"])
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code in ("404", "NoSuchBucket"):
                    return []
                raise
            return keys
        return self._resilient(op)


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

    def download_prefix(self, bucket: str, prefix: str, local_dir: str,
                        force: bool = False, on_file=None) -> int:
        gbucket = self._client.bucket(bucket)
        prefix = prefix.rstrip("/")
        key_prefix = f"{prefix}/" if prefix else ""
        count = 0
        for blob in self._client.list_blobs(gbucket, prefix=key_prefix):
            rel = blob.name[len(key_prefix):] if key_prefix else blob.name
            if not rel or blob.name.endswith("/"):
                continue
            dst = os.path.join(local_dir, *rel.split("/"))
            if not force and _same_size(dst, blob.size):
                continue
            _download_atomic(dst, blob.download_to_filename)
            if (blob.metadata or {}).get("executable") == "yes":
                os.chmod(dst, os.stat(dst).st_mode | 0o111)
            count += 1
            if on_file is not None:
                on_file()
        logger.debug("Downloaded %d files from gs://%s/%s", count, bucket, prefix)
        return count

    def read_object(self, bucket: str, key: str) -> "bytes | None":
        from google.cloud.exceptions import NotFound  # pylint: disable=import-outside-toplevel
        try:
            return self._client.bucket(bucket).blob(key).download_as_bytes()
        except NotFound:
            return None

    def list_keys(self, bucket: str, prefix: str = "") -> list[str]:
        from google.cloud.exceptions import NotFound  # pylint: disable=import-outside-toplevel
        gbucket = self._client.bucket(bucket)
        prefix = prefix.rstrip("/")
        key_prefix = f"{prefix}/" if prefix else ""
        try:
            return [b.name for b in self._client.list_blobs(gbucket, prefix=key_prefix)
                    if not b.name.endswith("/")]
        except NotFound:
            return []


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
        endpoint_resolver=cluster_config.get_driver_s3_endpoint,
        access_key=access_key,
        secret_key=secret_key,
        region=cluster_config.get_s3_region(),
    )
