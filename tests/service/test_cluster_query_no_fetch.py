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


def test_the_probe_does_not_claim_this_campaign_has_results(svc):
    """It answers "is a transfer needed", and must not be read as "is there data".

    The note is what a client shows, and it used to say the campaign's results *are* in
    the index -- a claim two metadata lookups about transfers cannot support. A caller
    that read it as an all-clear and then found nothing to query had been told a wrong
    thing by the one surface it asked first.
    """
    note = svc.campaign_data_status("camp-1").note

    assert "transfers nothing" in note, "the transfer answer is still given"
    assert "the campaign's results are in the central index" not in note


def test_status_costs_nothing(svc):
    """It is polled on the query path, so it must not reach the store — ``_FakeStorage``
    fails the test if it does."""
    for _ in range(3):
        svc.campaign_data_status("camp-1")


def test_a_query_names_the_campaign_by_id_not_by_its_cache_directory(svc, monkeypatch):
    """The id the caller gave, passed on rather than re-derived from a path.

    ``query_data_db`` takes ``campaign_id`` precisely so a caller who has it need not ask
    the filesystem a question ingestion already answered. Its two siblings here --
    ``describe_campaign_data`` and ``stream_campaign_query_csv`` -- passed it; the query
    path, the one every agent uses, did not, and so depended on a directory this lane
    deliberately never fills.
    """
    seen = {}

    def _spy(campaign_dir, sql, max_rows=500, **kwargs):
        seen.update(campaign_dir=campaign_dir, kwargs=kwargs)
        return {"columns": [], "rows": [], "row_count": 0, "truncated": False}

    monkeypatch.setattr("robovast.results_processing.data_query.query_data_db", _spy)

    svc.query_campaign_data_sql("camp-1", "SELECT 1")

    assert seen["kwargs"].get("campaign_id") == "camp-1"


def test_an_empty_cache_directory_does_not_refuse_a_query(svc, monkeypatch):
    """The state this defect lived in: a cache dir that exists and holds nothing.

    ``campaign_id_of`` returns the directory's name when the path is absent, which covers
    a campaign that was never fetched. A partial or abandoned fetch leaves the directory
    *present and empty* -- structurally the same situation, and the one case the guard did
    not cover, so it walked up for a ``campaign.db`` that was never going to be there and
    refused the query. The refusal named a path on the service's own disk and an argument
    the caller does not have, so it was neither the reason nor actionable.

    The campaign was finished, postprocessed, and its rows were in the index throughout.
    """
    from robovast.results_processing.data_query import campaign_id_of

    dest = svc._query_dir("camp-1")
    dest.mkdir(parents=True)
    assert not any(dest.iterdir()), "the state under test is an existing, empty directory"

    with pytest.raises(Exception, match="not inside a campaign directory"):
        campaign_id_of(dest)  # what the query used to do, and why it failed

    seen = {}
    monkeypatch.setattr(
        "robovast.results_processing.data_query.query_data_db",
        lambda campaign_dir, sql, max_rows=500, **kw: seen.update(kw)
        or {"columns": [], "rows": [], "row_count": 0, "truncated": False})

    svc.query_campaign_data_sql("camp-1", "SELECT 1")

    assert seen.get("campaign_id") == "camp-1", \
        "the query must not depend on what the cache directory happens to contain"


def test_an_absolute_address_is_not_an_index_id():
    """Both query surfaces take a campaign id *or* an absolute directory to analyse.

    Only the id scopes the central index. Passing a path through as one scopes to a
    campaign that does not exist, and the query answers with no rows — a wrong answer that
    looks exactly like a right one, which is worse than the refusal it replaced.
    """
    from robovast.service.local_transport import _index_id

    assert _index_id("camp-1") == "camp-1"
    assert _index_id("/results/camp-1") is None, "a path has to be resolved, not scoped"
    assert _index_id("") is None
