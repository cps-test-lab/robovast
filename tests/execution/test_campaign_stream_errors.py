# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A campaign export says the object store is unreachable, in one sentence.

The export streams a campaign's stored objects straight into an archive, so it talks
S3 outside ``ObjectStorage`` and does not get that class's translation for free. Left
raw, a lost connection reaches the operator as a botocore traceback naming the single
object key it happened to die on -- and, because the streaming pass runs in the tar
writer thread, at the point where the stream closes rather than where the transfer is.

Covered here: the failure must be recognised wherever in the transfer it happens (the
listing, the fetch, the *body read* mid-object), and a store that ANSWERED must not be
translated -- what it answered is the caller's question.
"""

import io
import tarfile

import pytest

from robovast.execution.cluster_config import base_config


def _endpoint_gone(endpoint="http://store.example.com:9000"):
    from botocore.exceptions import EndpointConnectionError
    return EndpointConnectionError(endpoint_url=f"{endpoint}/bucket/key")


def _client_error(code):
    from botocore.exceptions import ClientError
    return ClientError({"Error": {"Code": code, "Message": code}}, "GetObject")


class _FakeS3:
    """The three calls the streaming/sizing passes make, each independently faultable."""

    def __init__(self, *, pages=None, get_object=None, links=None):
        self._pages = pages if pages is not None else [{"Contents": []}]
        self._get_object = get_object
        self._links = links

    def get_paginator(self, _op):
        outer = self

        class _P:
            def paginate(self, **_kwargs):
                if isinstance(outer._pages, BaseException):
                    raise outer._pages
                return iter(outer._pages)
        return _P()

    def get_object(self, Bucket, Key):  # noqa: N803 - boto3's own kwarg names
        del Bucket
        if Key.endswith("_transient/job_links.yaml"):
            if isinstance(self._links, BaseException):
                raise self._links
            if self._links is None:
                raise _client_error("NoSuchKey")
            return {"Body": io.BytesIO(self._links)}
        if isinstance(self._get_object, BaseException):
            raise self._get_object
        return self._get_object


class _FailingBody:
    """An object body whose connection dies partway through the copy into the tar."""

    #: tarfile copies in 16 KiB blocks and treats a SHORT read as its own "unexpected end
    #: of data", so a body that fails partway must hand back whole blocks until it dies --
    #: which is what a real ``StreamingBody`` does before raising from ``read``.
    BLOCK = 16 * 1024

    def __init__(self, exc):
        self._first, self._exc = b"x" * self.BLOCK, exc

    def read(self, _size=-1):
        if self._first is not None:
            chunk, self._first = self._first, None
            return chunk
        raise self._exc


@pytest.fixture(name="tar")
def _tar():
    with tarfile.open(fileobj=io.BytesIO(), mode="w") as handle:
        yield handle


def _stream(tar, s3, monkeypatch, endpoint="http://store.example.com:9000"):
    monkeypatch.setattr(base_config, "_s3_client",
                        lambda *args, **kwargs: s3)
    base_config._s3_add_members(  # pylint: disable=protected-access
        tar, "bucket", "campaign-1", endpoint=endpoint,
        access_key="ak", secret_key="sk", prefix="campaign-1")


def test_a_lost_connection_while_listing_names_the_store_and_not_botocore(tar, monkeypatch):
    """The listing pass is the first thing the export does, and the first that can fail."""
    from robovast.common.errors import ObjectStoreUnreachableError

    with pytest.raises(ObjectStoreUnreachableError) as excinfo:
        _stream(tar, _FakeS3(pages=_endpoint_gone()), monkeypatch)

    message = str(excinfo.value)
    assert "http://store.example.com:9000" in message
    assert "streaming a campaign's stored objects" in message
    # The whole point: recorded and logged as the sentence, with no traceback tail
    # (``failure_detail`` honours the flag).
    assert excinfo.value.include_traceback is False


def test_a_connection_lost_mid_object_is_the_same_failure(tar, monkeypatch):
    """The body is read *inside* the block, by ``addfile``, not by ``get_object``.

    A translation wrapped around only the requests would leave exactly this case --
    the store answered, then stopped mid-transfer -- reaching the operator raw.
    """
    from botocore.exceptions import ConnectionClosedError

    from robovast.common.errors import ObjectStoreUnreachableError

    body = _FailingBody(ConnectionClosedError(endpoint_url="http://store.example.com:9000"))
    s3 = _FakeS3(pages=[{"Contents": [{"Key": "campaign-1/2/run.csv",
                                       "Size": 2 * _FailingBody.BLOCK}]}],
                 get_object={"Body": body, "Metadata": {}})

    with pytest.raises(ObjectStoreUnreachableError):
        _stream(tar, s3, monkeypatch)


def test_a_store_that_answered_is_not_reported_as_unreachable(tar, monkeypatch):
    """``NoSuchKey`` means the export asked for the wrong thing, which is a different bug.

    Calling it "unreachable" would send an operator to check a store that is running.
    """
    from botocore.exceptions import ClientError

    s3 = _FakeS3(pages=[{"Contents": [{"Key": "campaign-1/2/run.csv", "Size": 1}]}],
                 get_object=_client_error("NoSuchKey"))

    with pytest.raises(ClientError):
        _stream(tar, s3, monkeypatch)


def test_an_unreadable_job_link_manifest_is_not_a_campaign_without_links(tar, monkeypatch):
    """A refused manifest read must not finish the archive quietly.

    Absent means unpacked; anything else means the links this campaign HAS are missing
    from the archive, and an export that swallowed it would report success.
    """
    from botocore.exceptions import ClientError

    s3 = _FakeS3(links=_client_error("AccessDenied"))

    with pytest.raises(ClientError):
        _stream(tar, s3, monkeypatch)


def test_an_absent_job_link_manifest_still_exports_cleanly(tar, monkeypatch):
    """The unpacked campaign: no manifest, no links, no complaint."""
    _stream(tar, _FakeS3(links=None), monkeypatch)


def test_the_sizing_pass_reports_an_unreachable_store_too(monkeypatch):
    """It is a separate listing pass, run before the transfer to size the progress bar."""
    from robovast.common.errors import ObjectStoreUnreachableError

    monkeypatch.setattr(base_config, "_s3_client",
                        lambda *args, **kwargs: _FakeS3(pages=_endpoint_gone()))
    with pytest.raises(ObjectStoreUnreachableError) as excinfo:
        base_config._s3_campaign_bytes(  # pylint: disable=protected-access
            "bucket", endpoint="http://store.example.com:9000",
            access_key="ak", secret_key="sk", prefix="campaign-1")
    assert "listing a campaign's stored objects" in str(excinfo.value)
