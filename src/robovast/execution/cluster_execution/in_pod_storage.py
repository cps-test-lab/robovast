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
import tempfile
import time

logger = logging.getLogger(__name__)

# Object metadata flag marking a file as executable, matching the convention used
# by the upload-to-share compression and the job init containers so the bit
# survives a round-trip.
_EXECUTABLE_META = {"executable": "yes"}

# GCS deletes one blob per request; batching keeps a prefix removal to a few
# round-trips. Same batch size the host-side ``bucket_ops`` uses.
_GCS_DELETE_BATCH = 100


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


def _delete_key_prefix(bucket: str, prefix: str) -> str:
    """Normalize *prefix* to a ``dir/`` key prefix safe to delete under.

    Raises on an empty prefix rather than deleting the bucket's whole contents, and
    appends the trailing slash so ``foo`` cannot also match ``foobar/``.
    """
    clean = (prefix or "").strip().strip("/")
    if not clean:
        raise ValueError(
            "refusing to delete with an empty prefix — that would wipe all of "
            f"'{bucket}'. Pass the prefix to remove.")
    return f"{clean}/"


# How often (seconds) the download progress logger emits a running count.
_DOWNLOAD_PROGRESS_INTERVAL = 5.0


def download_progress_logger(subject, interval=_DOWNLOAD_PROGRESS_INTERVAL):
    """Return a no-argument callback that logs the running download count.

    Passed to :meth:`StorageClient.download_prefix` as ``on_file``: called once per
    fetched file, emitting a throttled ``downloaded N so far`` line so a large transfer
    shows progress instead of sitting silent. The count is cumulative when the callback is
    shared across several ``download_prefix`` calls (a search-mode batch makes one per
    config), so the log reads as one continuous total.

    *subject* is the whole log subject (``"Batch 3"``, ``"Campaign foo"``), not a bare id:
    the callers differ enough — a batch's result download, the service's campaign fetch —
    that a hard-coded prefix would misdescribe one of them.

    .. todo::

       ``kubernetes_backend._download_progress_logger`` is an older private copy of this,
       predating any second caller. Collapse it onto this one; it was left alone here only
       to keep this change out of a file being edited concurrently.
    """
    state = {"count": 0, "last": time.monotonic()}

    def on_file():
        state["count"] += 1
        now = time.monotonic()
        if now - state["last"] >= interval:
            state["last"] = now
            logger.info("%s: downloaded %d file(s) so far...", subject, state["count"])

    return on_file


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

    def stat_object(self, bucket: str, key: str) -> "int | None":
        """Return one object's byte size, or ``None`` if it does not exist.

        :meth:`list_entries` cannot answer this: it treats its argument as a *prefix* and
        appends ``/``, so an exact object key matches nothing. Sizing one known key — "is
        the cached copy of this ``data.db`` still current?" — is a single metadata
        round-trip, where the alternative (listing the campaign prefix to find one key) is
        the whole-prefix cost the caller is trying to avoid.
        """
        raise NotImplementedError

    def download_object(self, bucket: str, key: str, dst: str) -> bool:
        """Stream one object to *dst*; return False if it does not exist.

        :meth:`read_object` answers the same question through memory, which is the wrong
        shape for a ``data.db`` that can be hundreds of MB. Written via
        :func:`_download_atomic`, so a concurrent reader never opens a partial file.
        """
        raise NotImplementedError

    def list_entries(self, bucket: str, prefix: str = "",
                     delimited: bool = False) -> "tuple[list[tuple[str, int]], list[str]]":
        """Return ``(objects, sub_prefixes)`` under *prefix*.

        ``objects`` is ``(key, size)`` pairs; ``sub_prefixes`` is empty unless
        *delimited*, in which case the store rolls everything below the next ``/`` into
        one entry and returns the immediate children only.

        The delimiter is not an optimization — it is what makes a **non-recursive**
        listing non-recursive at the store as well as in the API. Enumerating a
        campaign's every key to display four directory names would make
        ``list_files(recursive=False)`` true for the caller and false for the object
        store, in the deployment where campaigns are largest.

        Must not raise on a not-yet-created bucket — implementations return empty.
        """
        raise NotImplementedError

    def list_keys(self, bucket: str, prefix: str = "") -> list[str]:
        """Return object keys under *prefix* (no trailing-slash pseudo-dirs).

        Used by the controller to count completed per-run artifacts in the live
        batch prefix (run-level progress), so it must not raise on a
        not-yet-created bucket — returns ``[]`` in that case. A thin view of
        :meth:`list_entries` so there is one enumeration path.
        """
        objects, _ = self.list_entries(bucket, prefix)
        return [key for key, _ in objects]

    def delete_prefix(self, bucket: str, prefix: str) -> int:
        """Delete every object under *prefix*; return how many were removed.

        Used to drop an experiment-image build's staged context once the build no
        longer needs it (see ``cluster_image_build.discard_context``) — the one thing
        written here that is scratch rather than results.

        Implementations must **refuse an empty prefix**: on a shared-bucket deployment
        the campaign results live in the same bucket, so a prefix-less delete would
        take them with it. A missing bucket is not an error — nothing to delete.
        """
        raise NotImplementedError


class _S3StorageClient(StorageClient):
    """boto3-backed client for MinIO / S3 reachable from inside the cluster."""

    #: Timeout budgets, keyed by ``interactive``. The distinction matters because the two
    #: kinds of caller have opposite failure preferences, and one budget cannot serve both:
    #:
    #: * **bulk** — a campaign's rosbag upload/download. Minutes of legitimate transfer, so
    #:   a patient read timeout and several retries are correct; giving up loses real work.
    #: * **interactive** — a listing or a status read behind the web UI's 1 Hz SSE poll.
    #:   Here the whole budget must be *smaller than the poll interval's tolerance*: the
    #:   caller has a documented degraded answer available (``_campaign_index`` serves its
    #:   stale cache), so failing in seconds is strictly better than blocking. With the
    #:   bulk budget one such call could occupy a worker thread for
    #:   ``read_timeout × max_attempts × reconnect_attempts`` ≈ 18 minutes while the poll
    #:   kept launching more — which is how a single stalled tunnel took the whole API down.
    #:
    #: ``reconnect_attempts`` is separate from boto's own ``max_attempts`` because they do
    #: different things: boto re-runs a request on the *same* connection, which is useless
    #: once an off-cluster ``kubectl port-forward`` has gone stalled-but-alive (every
    #: attempt then read-times-out identically), so on those errors ``_resilient`` rebuilds
    #: the client against a fresh forward and re-runs the whole operation instead.
    _BUDGETS = {
        False: {"connect_timeout": 10, "read_timeout": 120,
                "max_attempts": 3, "reconnect_attempts": 3},
        True: {"connect_timeout": 5, "read_timeout": 10,
               "max_attempts": 1, "reconnect_attempts": 1},
    }

    def __init__(self, *, endpoint_resolver, access_key, secret_key, region,
                 interactive: bool = False):
        # ``endpoint_resolver(force_reconnect=False)`` returns the host-reachable S3
        # endpoint; passing ``True`` tears down a stalled port-forward and opens a
        # fresh one (see BaseConfig.get_driver_s3_endpoint). In-cluster it is a
        # constant, so reconnecting is a cheap no-op that just rebuilds the client.
        self._endpoint_resolver = endpoint_resolver
        self._access_key = access_key
        self._secret_key = secret_key
        self._region = region
        self._budget = self._BUDGETS[bool(interactive)]
        self._reconnect_attempts = self._budget["reconnect_attempts"]
        # The endpoint this client is currently bound to; passed back to the resolver
        # on a forced reconnect so a shared port-forward is torn down only once per
        # stall (see ClusterService._minio_port_forward_endpoint). ``None`` until the
        # first connect, so that first resolve behaves exactly as before.
        self._endpoint = None
        # No ``socket.setdefaulttimeout`` here: it is process-global, so a client built
        # for one campaign silently retimed every other socket in the service — the
        # kubernetes client, urllib, the port-forward probe. The per-client botocore
        # timeouts below cover the intent without the action at a distance.
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
                connect_timeout=self._budget["connect_timeout"],
                read_timeout=self._budget["read_timeout"],
                retries={"max_attempts": self._budget["max_attempts"]},
            ),
        )

        # botocore adds ``Expect: 100-continue`` to uploads, which makes MinIO
        # emit an interim ``HTTP/1.1 100 Continue`` response that urllib3
        # mis-parses ("Failed to parse headers ... MissingHeaderBodySeparatorDefect,
        # unparsed data: 'HTTP/1...'"). Strip the header so each request gets a
        # single response. Registered on ``before-send`` (after signing) so the
        # header is removed regardless of when botocore added it.
        self._s3.meta.events.register("before-send.s3.*", self._strip_expect_header)

    def _resilient(self, op, what: str = "talking to the object store"):
        """Run ``op()``, reconnecting to a fresh port-forward on a network timeout.

        The whole operation is re-run after a reconnect, not the single failed
        request — every S3 op here is idempotent (uploads overwrite,
        ``download_prefix`` skips same-size files, reads/lists are pure), so a
        clean re-run is safe and, for downloads, near-free for what already landed.

        **Only the bulk profile reaches the reconnect.** An interactive client has
        ``reconnect_attempts == 1``, so it fails on the first timeout and lets its caller
        serve a degraded answer; repairing the tunnel is the keep-alive's job
        (``ClusterService._pf_monitor_loop``), which finds the same stall in ~10 s without
        a request waiting on it. A bulk transfer is the case that cannot wait — it has real
        work in flight and no degraded answer — so it still asks for the repair itself, and
        the ``current``-coalescing in ``_minio_port_forward_endpoint`` keeps concurrent
        bulk clients from tearing down each other's fresh tunnel.

        Once the attempts are spent the botocore transport error is translated into
        :class:`~robovast.common.errors.ObjectStoreUnreachableError` — one sentence
        naming the endpoint, what was being attempted (*what*, phrased to follow
        "while") and the transport's own reason. This is the single place every S3
        operation here funnels through, so no caller has to enumerate botocore's
        transport exceptions to avoid a 90-line traceback; the one that did, for
        ``EndpointConnectionError`` only, still 500'd on a connection reset.
        """
        from botocore.exceptions import (  # pylint: disable=import-outside-toplevel
            ConnectionClosedError, ConnectTimeoutError, EndpointConnectionError,
            ReadTimeoutError)

        from robovast.common.errors import \
            ObjectStoreUnreachableError  # pylint: disable=import-outside-toplevel
        from robovast.common.shutdown import \
            is_shutting_down  # pylint: disable=import-outside-toplevel
        transient = (ReadTimeoutError, ConnectTimeoutError,
                     EndpointConnectionError, ConnectionClosedError)
        last = None
        for attempt in range(self._reconnect_attempts):
            try:
                return op()
            except transient as exc:
                last = exc
                if attempt + 1 >= self._reconnect_attempts:
                    break
                if is_shutting_down():
                    # A timeout during shutdown is the shutdown itself: the endpoint
                    # went away because the service is closing the port-forward.
                    # Reconnecting would re-open the tunnel the process is tearing
                    # down and leak the kubectl child past exit, so fail with the
                    # network error instead.
                    logger.debug("S3 endpoint unreachable during shutdown (%s); "
                                 "not reconnecting", type(exc).__name__)
                    break
                logger.warning(
                    "S3 endpoint timed out (%s); restarting port-forward and "
                    "retrying (attempt %d/%d)",
                    type(exc).__name__, attempt + 2, self._reconnect_attempts)
                self._connect(force_reconnect=True)
        # botocore's message already quotes the endpoint URL, but of the failed
        # *request* — for a port-forward that rotates, the client's current endpoint is
        # the one an operator can probe, so name it here.
        raise ObjectStoreUnreachableError(
            f"Object store at {self._endpoint} is unreachable while {what}: {last}. "
            "Check that the object store (MinIO) is running; off-cluster it is reached "
            "through a kubectl port-forward, which the service's keep-alive reopens "
            "within ~10 s of a stall — so retrying shortly may be all that is needed."
        ) from last

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
        return self._resilient(op, f"uploading {local_dir} to s3://{bucket}/{clean}")

    def upload_file(self, local_path: str, bucket: str, key: str) -> None:
        def op():
            self._ensure_bucket(bucket)
            extra = ({"Metadata": dict(_EXECUTABLE_META)}
                     if _is_executable(local_path) else None)
            self._s3.upload_file(local_path, bucket, key, ExtraArgs=extra)
        self._resilient(op, f"uploading {local_path} to s3://{bucket}/{key}")

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
        return self._resilient(op, f"downloading s3://{bucket}/{clean}")

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
        return self._resilient(op, f"reading s3://{bucket}/{key}")

    def stat_object(self, bucket: str, key: str) -> "int | None":
        from botocore.exceptions import ClientError  # pylint: disable=import-outside-toplevel

        def op():
            try:
                return int(self._s3.head_object(Bucket=bucket, Key=key)["ContentLength"])
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code in ("404", "NoSuchKey", "NoSuchBucket"):
                    return None
                raise
        return self._resilient(op, f"checking s3://{bucket}/{key}")

    def download_object(self, bucket: str, key: str, dst: str) -> bool:
        from botocore.exceptions import ClientError  # pylint: disable=import-outside-toplevel

        def op():
            try:
                _download_atomic(dst, lambda p: self._s3.download_file(bucket, key, p))
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code in ("404", "NoSuchKey", "NoSuchBucket"):
                    return False
                raise
            return True
        return self._resilient(op, f"downloading s3://{bucket}/{key}")

    def list_entries(self, bucket: str, prefix: str = "", delimited: bool = False):
        from botocore.exceptions import ClientError  # pylint: disable=import-outside-toplevel
        clean = prefix.rstrip("/")
        key_prefix = f"{clean}/" if clean else ""
        extra = {"Delimiter": "/"} if delimited else {}

        def op():
            paginator = self._s3.get_paginator("list_objects_v2")
            objects: list[tuple[str, int]] = []
            sub_prefixes: list[str] = []
            try:
                for page in paginator.paginate(Bucket=bucket, Prefix=key_prefix, **extra):
                    for obj in page.get("Contents", []) or []:
                        if not obj["Key"].endswith("/"):
                            objects.append((obj["Key"], int(obj.get("Size", 0))))
                    for common in page.get("CommonPrefixes", []) or []:
                        sub_prefixes.append(common["Prefix"])
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code in ("404", "NoSuchBucket"):
                    return [], []
                raise
            return objects, sub_prefixes
        return self._resilient(op, f"listing s3://{bucket}/{key_prefix}")

    def delete_prefix(self, bucket: str, prefix: str) -> int:
        from botocore.exceptions import ClientError  # pylint: disable=import-outside-toplevel
        key_prefix = _delete_key_prefix(bucket, prefix)

        def op():
            paginator = self._s3.get_paginator("list_objects_v2")
            deleted = 0
            try:
                # A listing page is at most 1000 keys, which is also the DeleteObjects
                # limit — so one batched delete per page needs no extra chunking.
                for page in paginator.paginate(Bucket=bucket, Prefix=key_prefix):
                    objects = [{"Key": o["Key"]} for o in page.get("Contents", []) or []]
                    if objects:
                        self._s3.delete_objects(Bucket=bucket,
                                                Delete={"Objects": objects})
                        deleted += len(objects)
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code in ("404", "NoSuchBucket"):
                    return 0
                raise
            logger.debug("Deleted %d objects under s3://%s/%s", deleted, bucket,
                         key_prefix)
            return deleted
        return self._resilient(op, f"deleting s3://{bucket}/{key_prefix}")


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

    def stat_object(self, bucket: str, key: str) -> "int | None":
        from google.cloud.exceptions import NotFound  # pylint: disable=import-outside-toplevel
        try:
            # ``bucket.blob()`` is a local handle carrying no metadata; ``get_blob`` is the
            # call that asks the store (and returns None for a missing object).
            blob = self._client.bucket(bucket).get_blob(key)
        except NotFound:
            return None
        return None if blob is None else int(blob.size or 0)

    def download_object(self, bucket: str, key: str, dst: str) -> bool:
        from google.cloud.exceptions import NotFound  # pylint: disable=import-outside-toplevel
        try:
            blob = self._client.bucket(bucket).get_blob(key)
            if blob is None:
                return False
            _download_atomic(dst, blob.download_to_filename)
        except NotFound:
            return False
        return True

    def list_entries(self, bucket: str, prefix: str = "", delimited: bool = False):
        from google.cloud.exceptions import NotFound  # pylint: disable=import-outside-toplevel
        gbucket = self._client.bucket(bucket)
        prefix = prefix.rstrip("/")
        key_prefix = f"{prefix}/" if prefix else ""
        try:
            blobs = self._client.list_blobs(gbucket, prefix=key_prefix,
                                            delimiter="/" if delimited else None)
            objects = [(b.name, int(b.size or 0)) for b in blobs
                       if not b.name.endswith("/")]
            # ``prefixes`` is only populated once the iterator has been consumed.
            return objects, sorted(blobs.prefixes) if delimited else []
        except NotFound:
            return [], []

    def delete_prefix(self, bucket: str, prefix: str) -> int:
        from google.cloud.exceptions import NotFound  # pylint: disable=import-outside-toplevel
        key_prefix = _delete_key_prefix(bucket, prefix)
        gbucket = self._client.bucket(bucket)
        try:
            blobs = list(self._client.list_blobs(gbucket, prefix=key_prefix))
        except NotFound:
            return 0
        for i in range(0, len(blobs), _GCS_DELETE_BATCH):
            with self._client.batch():
                for blob in blobs[i:i + _GCS_DELETE_BATCH]:
                    blob.delete()
        logger.debug("Deleted %d objects under gs://%s/%s", len(blobs), bucket,
                     key_prefix)
        return len(blobs)


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


# -- the campaign index -----------------------------------------------------
#
# A campaign's durable home is the object store, but nothing there could *list* the
# campaigns: ``StorageClient`` has no bucket listing, and with a per-campaign-bucket
# deployment each campaign is a bucket named ``campaign_id.lower().replace("_","-")``
# — a lossy transform, so recovering the id by inverting it invents ids. So a campaign
# publishes a marker under one known prefix, and that prefix is what gets listed.
#
# The marker is a **zero-byte object whose key is the whole record**. There is no body,
# hence nowhere to put a status: ``_execution/outcome.json`` stays the one canonical
# terminal record, and the index cannot become a second source of truth for it.

#: Key prefix holding one marker per campaign.
CAMPAIGN_INDEX_PREFIX = "campaign-index"

#: Bucket for the markers when the deployment has no shared bucket of its own. Same
#: situation, and the same resolution, as an image build's staged context — see
#: :func:`~robovast.execution.cluster_execution.cluster_image_build.build_context_bucket`.
CAMPAIGN_INDEX_BUCKET = "robovast-campaign-index"


def campaign_index_bucket(cluster_config) -> str:
    """The bucket holding the campaign index.

    The deployment's shared bucket when it has one; otherwise a dedicated bucket of our
    own, which is defensible only on the ``s3`` backend. The reasoning is
    :func:`~robovast.execution.cluster_execution.cluster_image_build.build_context_bucket`'s
    verbatim — an index belongs to no campaign, so a per-campaign-bucket deployment has
    none to hand it, and on GCS a bucket name is global to all of Google Cloud while that
    client creates no buckets, so a missing shared bucket is a configuration error to
    report rather than a name to guess.
    """
    shared = cluster_config.get_s3_bucket()
    if shared:
        return shared
    backend = cluster_config.get_storage_backend()
    if backend != "s3":
        raise ValueError(
            f"campaign discovery on the '{backend}' storage backend needs a bucket "
            "configured for this deployment (there is no private namespace to create one "
            "in). Set it at 'vast exec cluster setup' (GCS: -o gcs_bucket=… or "
            "ROBOVAST_GCS_BUCKET).")
    return CAMPAIGN_INDEX_BUCKET


def mark_campaign_indexed(storage: StorageClient, cluster_config,
                          campaign_id: str, created_at: str) -> None:
    """Publish *campaign_id*'s index marker. Idempotent (an overwrite is the same key).

    *created_at* rides in the key because the **listing** needs it: the service orders
    every candidate campaign by start time before it paginates, so a start time that cost
    an object read would mean one round-trip per campaign on every cold listing. In the
    key, a single list answers the whole ordering pass, and the store is touched again
    only for the page actually rendered.

    The id comes **first** in the key so that removing a campaign is one ``delete_prefix``
    of ``campaign-index/<campaign_id>/`` — no need for a single-key delete primitive, and
    no need for the remover to know a ``created_at`` it does not have.

    The body is empty, so this uploads an empty temp file rather than reaching for a
    write-bytes primitive that ``StorageClient`` does not have and that this one marker
    is not reason enough to add: ``upload_file`` already carries bucket creation and the
    S3 reconnect-and-retry that a hand-rolled put would have to repeat.
    """
    bucket = campaign_index_bucket(cluster_config)
    with tempfile.NamedTemporaryFile(prefix="robovast-index-") as empty:
        storage.upload_file(empty.name, bucket, _index_key(campaign_id, created_at))


def list_indexed_campaigns(storage: StorageClient,
                           cluster_config) -> list[tuple[str, str]]:
    """Every indexed campaign as ``(campaign_id, created_at)``, from one listing.

    The id is a key segment **verbatim** — no sanitising was applied on the way in, so
    none has to be undone on the way out. A key that does not parse is skipped with a
    warning rather than guessed at: a malformed marker is one campaign that cannot be
    listed, not a reason to fail the listing.
    """
    bucket = campaign_index_bucket(cluster_config)
    out = []
    for key in storage.list_keys(bucket, CAMPAIGN_INDEX_PREFIX):
        rest = key[len(CAMPAIGN_INDEX_PREFIX) + 1:]
        campaign_id, sep, created_at = rest.partition("/")
        if not sep or not campaign_id:
            logger.warning("Skipping malformed campaign-index key %r", key)
            continue
        out.append((campaign_id, created_at))
    return out


def unmark_campaign_indexed(storage: StorageClient, cluster_config,
                            campaign_id: str) -> None:
    """Drop *campaign_id*'s marker, so a deleted campaign stops being listed.

    Deletes the campaign's whole index sub-prefix, which also clears a stale marker from
    an earlier launch of the same id — a single-key delete would leave that behind to
    resurrect the campaign in the next listing.
    """
    if not campaign_id:
        raise ValueError("campaign_id is required to unmark a campaign")
    bucket = campaign_index_bucket(cluster_config)
    storage.delete_prefix(bucket, f"{CAMPAIGN_INDEX_PREFIX}/{campaign_id}")


def _index_key(campaign_id: str, created_at: str) -> str:
    return f"{CAMPAIGN_INDEX_PREFIX}/{campaign_id}/{created_at}"


def storage_client_for(cluster_config, *, interactive: bool = False) -> StorageClient:
    """Build a :class:`StorageClient` from a reconstructed cluster config.

    Selects S3 (MinIO) or GCS based on ``cluster_config.get_storage_backend()``,
    using its endpoint / credentials — the same values the host and the job
    init/entrypoint containers use.

    Pass ``interactive=True`` for a client serving a *request*, not a transfer — a
    listing or a status read behind the web UI's poll. It trades the patient
    transfer-sized timeout budget for one that fails in seconds, so a stalled tunnel
    degrades the answer instead of occupying a worker thread for minutes; see
    :attr:`_S3StorageClient._BUDGETS`. The default stays the bulk budget, because
    losing a campaign's upload to an impatient timeout is the worse failure.
    """
    if cluster_config.get_storage_backend() == "gcs":
        return _GcsStorageClient(key_json=cluster_config.get_gcs_key_json())
    access_key, secret_key = cluster_config.get_s3_credentials()
    return _S3StorageClient(
        endpoint_resolver=cluster_config.get_driver_s3_endpoint,
        access_key=access_key,
        secret_key=secret_key,
        region=cluster_config.get_s3_region(),
        interactive=interactive,
    )
