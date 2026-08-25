# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""On the cluster, a file read is a *file* read.

``/results`` on the cluster resolves into the object store, and the inherited
filesystem implementation would get there through ``_data_dir`` → ``fetch_campaign`` →
``download_prefix``: reading a 2 KB ``outcome.json`` would pull every rosbag the
campaign produced, in the deployment where campaigns are largest. These tests assert
the override does a single-object read, and that a non-recursive listing is
non-recursive **at the store** — a delimited call, not an enumerate-and-fold.
"""

import pytest

from robovast.execution.cluster_execution.cluster_service import ClusterService


class _FakeStorage:
    """Records what was asked of the store, and refuses what must never be asked."""

    def __init__(self, objects):
        self.objects = objects           # key -> bytes
        self.calls = []

    def read_object(self, bucket, key):
        self.calls.append(("read_object", key))
        return self.objects.get(key)

    def list_entries(self, bucket, prefix="", delimited=False):
        self.calls.append(("list_entries", prefix, delimited))
        clean = prefix.rstrip("/")
        key_prefix = f"{clean}/" if clean else ""
        matching = [k for k in self.objects if k.startswith(key_prefix)]
        if not delimited:
            return [(k, len(self.objects[k])) for k in matching], []
        objects, prefixes = [], set()
        for k in matching:
            rest = k[len(key_prefix):]
            if "/" in rest:
                prefixes.add(key_prefix + rest.split("/", 1)[0] + "/")
            else:
                objects.append((k, len(self.objects[k])))
        return objects, sorted(prefixes)

    def download_prefix(self, *a, **kw):                       # pragma: no cover
        raise AssertionError("a single file read must not fetch the whole campaign")


@pytest.fixture(name="svc")
def _svc(monkeypatch):
    storage = _FakeStorage({
        "camp-1/_execution/outcome.json": b'{"status": "passed"}',
        "camp-1/_execution/controller.log": b"line 0\nline 1\nline 2\n",
        "camp-1/nav/0/test.xml": b"<testsuite/>",
        "camp-1/nav/0/scene/scene.bin": b"\x00\x01",
    })
    svc = ClusterService.__new__(ClusterService)
    monkeypatch.setattr(ClusterService, "_campaign_object_location",
                        lambda self, cid: (storage, "bucket", f"{cid}/"))
    monkeypatch.setattr(ClusterService, "fetch_campaign",
                        lambda *a, **kw: pytest.fail(
                            "read/list must never fall back to fetch_campaign"))
    return svc, storage


def test_read_is_one_object_not_the_campaign(svc):
    service, storage = svc
    data = service.read_file_bytes("/results/camp-1/_execution/outcome.json")
    assert data == b'{"status": "passed"}'
    assert storage.calls == [("read_object", "camp-1/_execution/outcome.json")]


def test_text_view_pages_the_single_object(svc):
    service, _ = svc
    page = service.read_file("/results/camp-1/_execution/controller.log", lines=2)
    assert page.total_lines == 3
    assert page.content == "line 0\nline 1"
    assert page.address == "/results/camp-1/_execution/controller.log"


def test_missing_object_is_404(svc):
    service, _ = svc
    with pytest.raises(KeyError):
        service.read_file_bytes("/results/camp-1/nope.txt")


def test_non_recursive_listing_is_delimited_at_the_store(svc):
    service, storage = svc
    listing = service.list_files("/results/camp-1/")
    assert sorted(listing.entries) == ["_execution/", "nav/"]
    # The delimiter is the point: the store rolled everything below the next '/' into
    # one entry rather than handing back every key for us to fold.
    assert storage.calls == [("list_entries", "camp-1/", True)]


def test_recursive_listing_drops_the_delimiter(svc):
    service, storage = svc
    listing = service.list_files("/results/camp-1/nav/", recursive=True)
    assert listing.entries == ["0/scene/scene.bin", "0/test.xml"]
    assert storage.calls == [("list_entries", "camp-1/nav", False)]


def test_detailed_listing_reports_object_sizes(svc):
    service, _ = svc
    listing = service.list_files("/results/camp-1/nav/0/", detail=True)
    sizes = {e.name: e.bytes for e in listing.detailed}
    assert sizes["test.xml"] == len(b"<testsuite/>")
    # A common prefix is not an object, so it has no size to report — None, not 0.
    assert sizes["scene"] is None


def test_missing_prefix_is_404_not_an_empty_listing(svc):
    """An object store has no empty directories, so nothing under the prefix means the
    directory is absent — collapsing that into an empty list would report "there is
    nothing here" for a campaign that was never published."""
    service, _ = svc
    with pytest.raises(KeyError):
        service.list_files("/results/camp-1/nope/")


def test_an_escape_never_reaches_the_store(svc):
    service, storage = svc
    with pytest.raises(ValueError):
        service.read_file_bytes("/results/camp-1/../camp-2/secret")
    assert storage.calls == []
