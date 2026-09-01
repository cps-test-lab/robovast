# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""On the cluster, a query transfers nothing at all.

This file used to pin a query to the two databases it opened (``_execution/data.db`` +
``campaign.db``), because a query needed those files present in the pod. With the central
index there is no such need: the rows are in the index and the campaign's directory is
never involved. What has to be pinned now is the *absence* of the transfer — the fetch-
then-query path re-downloaded the databases on every cold query, and reinstating it would
be invisible except as a wait.

The companion of ``test_cluster_file_reads``, which pins a file read to a single object.
"""

import threading

import pytest

from robovast.execution.cluster_execution.cluster_service import ClusterService


class _FakeStorage:
    """Refuses everything: a query must not reach the object store at all."""

    def stat_object(self, *a, **kw):                           # pragma: no cover
        raise AssertionError("a query must not probe the object store")

    def download_object(self, *a, **kw):                       # pragma: no cover
        raise AssertionError("a query must not download a database")

    def download_prefix(self, *a, **kw):                       # pragma: no cover
        raise AssertionError("a query must not fetch the whole campaign")


@pytest.fixture(name="svc")
def _svc(monkeypatch, tmp_path):
    storage = _FakeStorage()
    service = ClusterService.__new__(ClusterService)
    service._fetch_locks = {}
    service._fetch_locks_guard = threading.Lock()
    service._last_fetch = {}
    service._work_progress = {}
    service._work_progress_guard = threading.Lock()
    monkeypatch.setattr(ClusterService, "_campaign_object_location",
                        lambda self, cid, *, interactive=False: (storage, "bucket", f"{cid}/"))
    monkeypatch.setattr(ClusterService, "_cache_dir", lambda self, cid: tmp_path / cid)
    monkeypatch.setattr(ClusterService, "fetch_campaign",
                        lambda *a, **kw: pytest.fail(
                            "a query must never fall back to fetch_campaign"))
    monkeypatch.setattr(ClusterService, "_materialize",
                        lambda *a, **kw: pytest.fail(
                            "a query must not materialize any object"))
    return service


def test_query_dir_names_the_campaign_without_fetching_anything(svc):
    """All the shared query surface takes from the path is the campaign it names."""
    from robovast.results_processing.data_query import campaign_id_of

    dest = svc._query_dir("camp-1")

    assert campaign_id_of(dest) == "camp-1"
    # Nothing was created either: the campaign has no directory on this pod.
    assert not dest.exists()


def test_status_reports_no_transfer(svc):
    """The probe exists to warn before an expensive wait; there is no longer one to warn
    about, and saying otherwise would keep a fetch notice on every query."""
    status = svc.campaign_data_status("camp-1")

    assert status.fetch_required is False
    assert status.cached is True
    assert status.transfer == "none"
    assert status.fetch_in_progress is False
    assert "index" in status.note


def test_status_costs_nothing(svc):
    """It is polled on the query path, so it must not reach the store — ``_FakeStorage``
    fails the test if it does."""
    for _ in range(3):
        svc.campaign_data_status("camp-1")
