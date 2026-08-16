# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``delete_campaign`` on the per-campaign-bucket path, which had no test at all.

That gap let a rename land a crash: making ``_bucket_name`` public as ``bucket_name``
turned the local ``bucket_name = bucket_name(campaign_id)`` into a self-shadowing
assignment, so the *only* line that computes the bucket to delete raised
``UnboundLocalError`` before touching S3. Pylint saw it; nothing else did.

Deletion is also the operation where a silent misfire is least acceptable, so the test
pins *which* bucket is deleted rather than only that the call returns.
"""

# pylint: disable=redefined-outer-name  # the pytest fixture idiom

import contextlib
from unittest.mock import MagicMock

import pytest

from robovast.execution.cluster_execution import bucket_ops

#: `delete_campaign` refuses ids that do not look like campaigns -- a guard against
#: deleting unrelated buckets -- so the fixture id has to be a real one.
CAMPAIGN = "basic-nav-rst-2026-08-14-14350734"


class _Config:
    """A cluster config in per-campaign-bucket mode (embedded MinIO)."""

    def __init__(self, shared=None, backend="minio"):
        self._shared = shared
        self._backend = backend

    def get_s3_bucket(self):
        return self._shared

    def get_storage_backend(self):
        return self._backend


@pytest.fixture
def s3(monkeypatch):
    """Capture the S3 calls without a cluster, a port-forward or boto3."""
    client = MagicMock()
    page = {"Contents": [{"Key": f"{CAMPAIGN}/run-0/log.txt"}]}
    client.get_paginator.return_value.paginate.return_value = [page]

    @contextlib.contextmanager
    def _fake_connection(cluster_config, namespace, context):
        del cluster_config, namespace, context
        yield client

    monkeypatch.setattr(bucket_ops, "_s3_connection", _fake_connection)
    return client


def test_deleting_a_campaign_names_the_campaigns_own_bucket(s3):
    """The regression: this raised UnboundLocalError before reaching any S3 call."""
    bucket_ops.delete_campaign(CAMPAIGN, _Config(), "robovast", None)

    expected = bucket_ops.bucket_name(CAMPAIGN)
    s3.delete_bucket.assert_called_once_with(Bucket=expected)
    s3.delete_objects.assert_called_once_with(
        Bucket=expected, Delete={"Objects": [{"Key": f"{CAMPAIGN}/run-0/log.txt"}]})


def test_a_shared_bucket_is_pruned_by_prefix_and_never_deleted(s3):
    """The other branch, and the reason the two must not be confused: a shared bucket
    holds every campaign, so deleting it would take the rest with it."""
    bucket_ops.delete_campaign(CAMPAIGN, _Config(shared="robovast-results"), "ns", None)

    s3.delete_bucket.assert_not_called()


def test_the_bucket_helper_is_importable_under_its_public_name():
    """The rename that caused the crash made this name part of the module's surface --
    `cluster_service` and the cleanup verbs call it across module boundaries."""
    assert callable(bucket_ops.bucket_name)
    assert not hasattr(bucket_ops, "_bucket_name"), "the private alias should be gone"


def test_gcs_mode_does_not_reach_the_s3_path(monkeypatch):
    """`delete_campaign` returns before `_s3_connection` on the GCS lane. If the S3
    branch ran too, the UnboundLocalError above would have fired for GCS users as well.
    """
    gcs_client = MagicMock()
    gcs_client.list_blobs.return_value = []

    @contextlib.contextmanager
    def _fake_gcs(cluster_config):
        del cluster_config
        yield gcs_client

    def _boom(*args, **kwargs):
        raise AssertionError("the GCS lane fell through into the S3 path")

    monkeypatch.setattr(bucket_ops, "_gcs_connection", _fake_gcs)
    monkeypatch.setattr(bucket_ops, "_s3_connection", _boom)
    # The module defers `google.cloud.storage`; the fake connection replaces the only
    # importer, but `delete_campaign` still reads the bucket name off the config.
    config = _Config(backend="gcs")
    config.get_gcs_bucket = lambda: "robovast-gcs"

    bucket_ops.delete_campaign(CAMPAIGN, config, "ns", None)
