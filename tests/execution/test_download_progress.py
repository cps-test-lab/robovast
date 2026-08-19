# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A transfer that takes minutes has to say how far along it is.

Pulling a campaign out of the object store is GBs over a port-forward, and for as long as it
runs the caller sees one request that has not answered yet. ``download_prefix`` therefore
reports ``(files_done, files_total, bytes_done, bytes_total)``, and these tests pin the two
properties that make the resulting bar trustworthy:

* the **denominator counts what is missing**, not what exists — a re-view of a cached
  campaign must not advertise work it is about to skip; and
* asking for it is **opt-in**, so the batch downloader (which only logs a running count) does
  not pay for a listing nobody displays.
"""

from robovast.execution.cluster_execution import in_pod_storage


class _FakeStorage(in_pod_storage.StorageClient):
    """A store backed by a dict, with the base class's real ``count_pending`` on top.

    Inherits rather than reimplements: ``count_pending`` is the shared helper under test, and
    a fake that answered it itself would test nothing.
    """

    def __init__(self, objects):
        self.objects = objects           # key -> bytes
        self.listings = 0

    def list_entries(self, bucket, prefix="", delimited=False):
        self.listings += 1
        clean = prefix.rstrip("/")
        key_prefix = f"{clean}/" if clean else ""
        return ([(k, len(v)) for k, v in self.objects.items() if k.startswith(key_prefix)],
                [])


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_count_pending_counts_only_what_is_missing(tmp_path):
    storage = _FakeStorage({
        "camp/a.txt": b"1234",
        "camp/nested/b.txt": b"12345678",
        "camp/c.txt": b"1",
    })
    _write(tmp_path / "nested" / "b.txt", b"12345678")  # already cached, at the right size

    files, total = storage.count_pending("bucket", "camp", str(tmp_path))

    assert (files, total) == (2, 5)


def test_count_pending_is_zero_for_a_fully_cached_prefix(tmp_path):
    """The re-view case. A bar drawn from a total that ignored the cache would sit at 0%
    through a call that does no work at all."""
    storage = _FakeStorage({"camp/a.txt": b"1234", "camp/b.txt": b"12345678"})
    _write(tmp_path / "a.txt", b"1234")
    _write(tmp_path / "b.txt", b"12345678")

    assert storage.count_pending("bucket", "camp", str(tmp_path)) == (0, 0)


def test_count_pending_ignores_the_cache_when_forced(tmp_path):
    """``force=True`` re-downloads everything (re-postprocessing rewrites objects in place),
    so the denominator has to grow back to the full prefix."""
    storage = _FakeStorage({"camp/a.txt": b"1234"})
    _write(tmp_path / "a.txt", b"1234")

    assert storage.count_pending("bucket", "camp", str(tmp_path), force=True) == (1, 4)


def test_progress_reporter_throttles_but_always_reports_the_last_file():
    """The throttle exists because a campaign holds tens of thousands of files. Swallowing the
    *final* update would leave the bar parked below 100% for the whole tail of the request."""
    seen = []
    # A minute-long interval: every call but the last one is inside it.
    report = in_pod_storage.download_progress_reporter(
        lambda *args: seen.append(args), interval=60.0)

    for done in range(1, 5):
        report(done, 4, done * 10, 40)

    assert seen == [(1, 4, 10, 40), (4, 4, 40, 40)]


def test_progress_reporter_passes_an_unknown_total_through():
    """``total=None`` is how a caller says "indeterminate"; inventing a denominator here would
    make the UI draw a bar that means nothing."""
    seen = []
    report = in_pod_storage.download_progress_reporter(
        lambda *args: seen.append(args), interval=0.0)

    report(3, None, 300, None)

    assert seen == [(3, None, 300, None)]


class _FakeS3:
    """The two boto3 calls ``download_prefix`` makes, plus a paginator over a dict."""

    def __init__(self, objects):
        self.objects = objects

    def get_paginator(self, _name):
        pages = [{"Contents": [{"Key": k, "Size": len(v)}
                               for k, v in self.objects.items()]}]
        return type("_P", (), {"paginate": lambda _s, **kw: pages})()

    def download_file(self, _bucket, key, path):
        with open(path, "wb") as fh:
            fh.write(self.objects[key])

    def head_object(self, Bucket=None, Key=None):  # noqa: N803 - boto3's own spelling  # pylint: disable=invalid-name
        return {"Metadata": {}}


def _s3_client(objects, listings):
    """An ``_S3StorageClient`` wired to fakes, counting the listings it performs."""
    client = in_pod_storage._S3StorageClient.__new__(in_pod_storage._S3StorageClient)
    client._s3 = _FakeS3(objects)
    client._resilient = lambda op, _desc: op()

    def list_entries(bucket, prefix="", delimited=False):
        listings.append(prefix)
        return [(k, len(v)) for k, v in objects.items()], []

    client.list_entries = list_entries
    return client


def test_download_prefix_reports_progress_against_a_real_total(tmp_path):
    listings = []
    client = _s3_client({"camp/a.txt": b"1234", "camp/b.txt": b"12345678"}, listings)
    seen = []

    n = client.download_prefix("bucket", "camp", str(tmp_path),
                               on_progress=lambda *args: seen.append(args))

    assert n == 2
    assert seen == [(1, 2, 4, 12), (2, 2, 12, 12)]
    assert (tmp_path / "b.txt").read_bytes() == b"12345678"


def test_download_prefix_does_not_list_when_no_progress_is_wanted(tmp_path):
    """The denominator costs a listing pass. A caller that only logs a running count — the
    batch downloader — must not be charged for it."""
    listings = []
    client = _s3_client({"camp/a.txt": b"1234"}, listings)
    calls = []

    client.download_prefix("bucket", "camp", str(tmp_path),
                           on_file=lambda: calls.append(1))

    assert calls == [1]
    assert not listings


def test_download_prefix_lists_once_per_transfer(tmp_path):
    """Counted outside the retried operation: a reconnect re-runs the download, and paying
    for the denominator again on every attempt is the cost this seam is meant to be cheap
    against."""
    listings = []
    client = _s3_client({"camp/a.txt": b"1234"}, listings)
    attempts = []

    def flaky(op, _desc):
        attempts.append(1)
        op()                    # first attempt, as if it had failed mid-way
        return op()             # the retry

    client._resilient = flaky
    client.download_prefix("bucket", "camp", str(tmp_path), on_progress=lambda *a: None)

    assert attempts == [1]
    assert listings == ["camp"]
