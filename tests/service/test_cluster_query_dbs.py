# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""On the cluster, a query is a *query* — it fetches two databases, not a campaign.

The companion of ``test_cluster_file_reads``: that one pins a file read to a single
object, this one pins a SQL query to the two databases ``data_query._open_db`` actually
opens (``_execution/data.db`` + ``campaign.db``). Reaching them through ``_data_dir`` →
``fetch_campaign`` → ``download_prefix`` would pull every rosbag the campaign produced to
answer ``SELECT COUNT(*)``.

``data.db`` is the one campaign object that is **mutable** — re-postprocessing rewrites it
in place — so the cache check has to be by size. An existence check would pin the first
version a service ever saw and serve stale metrics forever, which is why that has its own
test here.
"""

import pytest

from robovast.service.cluster_service import ClusterService


class _FakeStorage:
    """Records what was asked of the store, and refuses what must never be asked."""

    def __init__(self, objects):
        self.objects = objects           # key -> bytes
        self.calls = []

    def stat_object(self, bucket, key):
        self.calls.append(("stat_object", key))
        data = self.objects.get(key)
        return None if data is None else len(data)

    def download_object(self, bucket, key, dst):
        self.calls.append(("download_object", key))
        data = self.objects.get(key)
        if data is None:
            return False
        with open(dst, "wb") as fh:
            fh.write(data)
        return True

    def download_prefix(self, *a, **kw):                       # pragma: no cover
        raise AssertionError("a query must not fetch the whole campaign")

    def read_object(self, *a, **kw):                           # pragma: no cover
        raise AssertionError("a database is streamed to disk, not read through memory")


def _service(monkeypatch, tmp_path, objects):
    storage = _FakeStorage(objects)
    svc = ClusterService.__new__(ClusterService)
    svc._fetch_locks = {}
    svc._fetch_locks_guard = __import__("threading").Lock()
    svc._last_fetch = {}
    svc._work_progress = {}
    svc._work_progress_guard = __import__("threading").Lock()
    # ``interactive=`` only selects the timeout budget (fail-fast for polled request paths
    # vs patient for bulk transfers), which nothing here observes — accept and ignore it.
    monkeypatch.setattr(ClusterService, "_campaign_object_location",
                        lambda self, cid, *, interactive=False: (storage, "bucket", f"{cid}/"))
    monkeypatch.setattr(ClusterService, "_cache_dir",
                        lambda self, cid: tmp_path / cid)
    monkeypatch.setattr(ClusterService, "fetch_campaign",
                        lambda *a, **kw: pytest.fail(
                            "a query must never fall back to fetch_campaign"))
    return svc, storage


@pytest.fixture(name="svc")
def _svc(monkeypatch, tmp_path):
    return _service(monkeypatch, tmp_path, {
        "camp-1/_execution/data.db": b"DATA-DB-BYTES",
        "camp-1/campaign.db": b"CAMPAIGN-DB",
        # Present in the store and irrelevant to a query: touching either of these is the
        # bug this whole seam exists to prevent.
        "camp-1/nav/0/rosbag2/bag.mcap": b"\x00" * 4096,
        "camp-1/nav/0/test.xml": b"<testsuite/>",
    })


def test_query_dir_fetches_only_the_two_databases(svc):
    service, storage = svc
    dest = service._query_dir("camp-1")

    assert (dest / "_execution" / "data.db").read_bytes() == b"DATA-DB-BYTES"
    assert (dest / "campaign.db").read_bytes() == b"CAMPAIGN-DB"
    assert [c for c in storage.calls if c[0] == "download_object"] == [
        ("download_object", "camp-1/_execution/data.db"),
        ("download_object", "camp-1/campaign.db"),
    ]
    # Nothing under a run directory was even sized.
    assert not any("rosbag2" in key or "test.xml" in key for _, key in storage.calls)


def test_second_query_reuses_the_cache(svc):
    service, storage = svc
    service._query_dir("camp-1")
    storage.calls.clear()

    service._query_dir("camp-1")

    # Sized to check currency, but not transferred again.
    assert [c[0] for c in storage.calls] == ["stat_object", "stat_object"]


def test_rewritten_database_is_refetched(svc):
    """Re-postprocessing rewrites ``data.db``; a size check notices, existence would not."""
    service, storage = svc
    service._query_dir("camp-1")
    storage.objects["camp-1/_execution/data.db"] = b"REBUILT-BY-POSTPROCESSING"
    storage.calls.clear()

    dest = service._query_dir("camp-1")

    assert ("download_object", "camp-1/_execution/data.db") in storage.calls
    assert (dest / "_execution" / "data.db").read_bytes() == b"REBUILT-BY-POSTPROCESSING"
    # The unchanged one was left alone.
    assert ("download_object", "camp-1/campaign.db") not in storage.calls


def test_missing_database_is_not_an_error_here(monkeypatch, tmp_path):
    """A campaign with no ``data.db`` yet is queryable through ``campaign.db`` alone.

    ``_open_db`` owns that decision and raises its own clear message when neither exists,
    so this layer must not pre-empt it with a fetch error.
    """
    service, storage = _service(monkeypatch, tmp_path,
                                {"camp-2/campaign.db": b"CAMPAIGN-DB-ONLY"})

    dest = service._query_dir("camp-2")

    assert (dest / "campaign.db").read_bytes() == b"CAMPAIGN-DB-ONLY"
    assert not (dest / "_execution" / "data.db").exists()


def test_status_reports_cold_then_cached(svc):
    service, _ = svc

    cold = service.campaign_data_status("camp-1")
    assert cold.fetch_required is True
    assert cold.cached is False
    assert cold.db_bytes == len(b"DATA-DB-BYTES") + len(b"CAMPAIGN-DB")
    assert cold.last_fetch_seconds is None
    assert "not in the service cache" in cold.note

    service._query_dir("camp-1")
    warm = service.campaign_data_status("camp-1")
    assert warm.cached is True
    # The measured cost of the transfer that just happened, not a guess.
    assert warm.last_fetch_bytes == cold.db_bytes
    assert warm.last_fetch_seconds is not None


def test_status_names_the_transfer_mode(svc, monkeypatch):
    """The same object store reached two ways differs by orders of magnitude."""
    service, _ = svc

    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    assert service.campaign_data_status("camp-1").transfer == "port-forward"

    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    assert service.campaign_data_status("camp-1").transfer == "cluster-network"


def test_status_does_not_enumerate_the_campaign(svc):
    """The probe precedes the expensive thing, so it must not itself cost a listing."""
    service, storage = svc

    service.campaign_data_status("camp-1")

    assert [c[0] for c in storage.calls] == ["stat_object", "stat_object"]


def test_status_reports_live_progress_from_memory(svc):
    """While a transfer runs the status is polled about once a second, over the very link the
    transfer is saturating. It must answer from memory and touch the store not at all."""
    service, storage = svc
    seen = []
    storage.download_object = lambda bucket, key, dst: (
        seen.append(service.campaign_data_status("camp-1")) or True)
    storage.calls.clear()

    service._query_dir("camp-1")

    assert [s.progress.phase for s in seen] == ["downloading", "downloading"]
    assert [s.progress.done for s in seen] == [0, 1]
    assert [s.progress.total for s in seen] == [2, 2]
    assert all(s.fetch_in_progress and not s.cached for s in seen)
    # Every ``stat_object`` here belongs to the fetch itself; the status calls added none.
    assert [c[0] for c in storage.calls] == ["stat_object", "stat_object"]


def test_progress_is_dropped_when_the_work_ends(svc):
    """A record left behind would advertise a transfer that is not running — and on the
    failure path, park a bar at 37%% forever."""
    service, storage = svc
    service._query_dir("camp-1")
    assert service.campaign_data_status("camp-1").progress is None

    def boom(*_a, **_kw):
        raise RuntimeError("the port-forward died mid-transfer")

    storage.objects["camp-1/_execution/data.db"] = b"REWRITTEN-SO-IT-REFETCHES"
    storage.download_object = boom
    with pytest.raises(RuntimeError):
        service._query_dir("camp-1")

    assert service.campaign_data_status("camp-1").progress is None


def test_a_cached_fetch_publishes_no_progress(svc):
    """Nothing is transferred on a re-view, so nothing should flash a progress bar.

    Reads the record directly rather than through ``campaign_data_status``: that call is
    itself made of ``stat_object``, which is the hook this test observes through.
    """
    service, storage = svc
    service._query_dir("camp-1")
    real_stat, seen = storage.stat_object, []
    storage.stat_object = lambda bucket, key: (
        seen.append(dict(service._work_progress)) or real_stat(bucket, key))

    service._query_dir("camp-1")

    assert seen and all(published == {} for published in seen)
