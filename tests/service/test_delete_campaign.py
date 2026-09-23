# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Deleting a campaign removes everything it owns on the service host, or says what it left.

A campaign's bytes are not only its directory: the local lane writes each archive it shares
beside the campaign dirs, and a failed import keeps the copy it fetched from the share. A
delete that left those behind, or that swallowed a path it could not remove, would answer
"deleted" while the space stayed taken.
"""

import os

import pytest

CID = "gone-2026-09-01-101500"


class _NullIndexConn:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


@pytest.fixture(name="env")
def _env(tmp_path, monkeypatch):
    from tests.service.null_lane import NullLane
    from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore

    monkeypatch.delenv("ROBOVAST_ARCHIVE_DIR", raising=False)
    monkeypatch.setattr("robovast.results_processing.index_schema.forget_campaign",
                        lambda conn, cid: {})
    monkeypatch.setattr("robovast.common.index_db.connect",
                        lambda *a, **k: _NullIndexConn())
    results = tmp_path / "results"
    (results / CID / "cfg" / "0").mkdir(parents=True)
    (results / CID / "cfg" / "0" / "out.bag").write_bytes(b"x" * 16)
    store = WorkspaceStore(registry=WorkspaceRegistry(root=str(tmp_path / "ws")))
    transport = NullLane(store=store)
    transport._campaigns_root = lambda: results        # noqa: SLF001
    return transport, results


def test_the_local_archives_go_with_the_campaign(env):
    transport, results = env
    archives = results / "_archives"
    archives.mkdir()
    mine = [archives / f"{CID}.raw.tar.gz", archives / f"{CID}.postprocessed.tar.gz",
            archives / f"{CID}.raw.tar.gz.part"]
    other = archives / "other-2026-09-01-101500.raw.tar.gz"
    for path in [*mine, other]:
        path.write_bytes(b"a" * 8)

    result = transport.delete_campaign(CID)

    assert result.ok, result.message
    assert "3 archive(s)" in result.message
    assert not (results / CID).exists()
    assert not any(p.exists() for p in mine)
    assert other.exists(), "another campaign's archive is not this delete's to remove"


def test_the_archive_dir_override_is_honoured(env, tmp_path, monkeypatch):
    transport, _results = env
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv("ROBOVAST_ARCHIVE_DIR", str(elsewhere))
    archive = elsewhere / f"{CID}.raw.tar.gz"
    archive.write_bytes(b"a")

    assert transport.delete_campaign(CID).ok
    assert not archive.exists()


def test_a_share_copy_a_failed_import_kept_is_removed(env):
    transport, results = env
    staged = results / "_imports" / f"{CID}.tar.gz"
    staged.parent.mkdir()
    staged.write_bytes(b"a")
    upload = results / "_imports" / "sometoken.tar.gz"
    upload.write_bytes(b"a")

    assert transport.delete_campaign(CID).ok
    assert not staged.exists()
    assert upload.exists(), "an upload is keyed by its token and left to the age sweep"


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root can unlink anything, so nothing is left to report")
def test_a_path_that_cannot_be_removed_fails_the_delete_and_is_named(env):
    """The shape of run output written by a container user other than the service's."""
    transport, results = env
    locked = results / CID / "cfg" / "0"
    locked.chmod(0o555)
    try:
        result = transport.delete_campaign(CID)
    finally:
        locked.chmod(0o755)

    assert not result.ok
    assert "out.bag" in result.message
    assert "run_as_user" in result.message
    # Once the owner has made it removable, deleting again finishes the job.
    assert transport.delete_campaign(CID).ok
    assert not (results / CID).exists()


def test_a_delete_drops_what_is_cached_about_the_campaign(env):
    transport, _results = env
    transport._started_at_cache[CID] = "2026-09-01T10:15:00"       # noqa: SLF001
    transport._description_cache[CID] = "old"                      # noqa: SLF001
    transport._summary_cache[CID] = ("key", object())              # noqa: SLF001

    assert transport.delete_campaign(CID).ok
    assert CID not in transport._started_at_cache                  # noqa: SLF001
    assert CID not in transport._description_cache                 # noqa: SLF001
    assert CID not in transport._summary_cache                     # noqa: SLF001


def test_a_campaign_with_nothing_left_deletes_idempotently(env):
    transport, _results = env
    assert transport.delete_campaign(CID).ok
    again = transport.delete_campaign(CID)
    assert again.ok
    assert "nothing to delete" in again.message


# -- several at once -------------------------------------------------------------------------
#
# One call, one outcome per id. Not all-or-nothing: a running campaign among the ids is that
# id's refusal, and the rest are deleted -- so a caller clearing out a page of finished
# campaigns is not sent back to retry every one because one of them was busy.

OTHER = "kept-2026-09-01-101500"


def _running(transport, monkeypatch, campaign_id):
    transport._campaigns[campaign_id] = object()                    # noqa: SLF001
    monkeypatch.setattr(transport, "_is_done", lambda entry: False)


def test_each_id_gets_its_own_outcome_in_request_order(env, monkeypatch):
    from robovast.service.interface import DeleteCampaignsRequest
    transport, results = env
    (results / OTHER).mkdir()
    _running(transport, monkeypatch, OTHER)
    ids = [CID, "..", OTHER, "absent-2026-09-01-101500"]

    res = transport.delete_campaigns(DeleteCampaignsRequest(campaign_ids=ids))

    assert [(r.campaign_id, r.outcome, r.ok) for r in res.results] == [
        (CID, "deleted", True),
        ("..", "invalid", False),
        (OTHER, "running", False),
        ("absent-2026-09-01-101500", "not_found", True),
    ]
    assert not (results / CID).exists()
    assert (results / OTHER).is_dir(), "a refused campaign is not touched"
    assert "stop it" in res.results[2].message


def test_a_repeated_id_is_deleted_and_reported_once(env):
    from robovast.service.interface import DeleteCampaignsRequest
    transport, _results = env
    res = transport.delete_campaigns(DeleteCampaignsRequest(campaign_ids=[CID, CID]))
    assert [(r.campaign_id, r.outcome) for r in res.results] == [(CID, "deleted")]


def test_a_request_naming_no_campaign_is_refused():
    from pydantic import ValidationError

    from robovast.service.interface import DeleteCampaignsRequest
    with pytest.raises(ValidationError):
        DeleteCampaignsRequest(campaign_ids=[])


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root can unlink anything, so nothing is left to report")
def test_a_partial_delete_is_that_ids_outcome_and_the_rest_still_go(env):
    from robovast.service.interface import DeleteCampaignsRequest
    transport, results = env
    (results / OTHER).mkdir()
    locked = results / CID / "cfg" / "0"
    locked.chmod(0o555)
    try:
        res = transport.delete_campaigns(DeleteCampaignsRequest(campaign_ids=[CID, OTHER]))
    finally:
        locked.chmod(0o755)

    first, second = res.results
    assert (first.outcome, first.ok) == ("partial", False)
    assert "out.bag" in first.message
    assert (second.outcome, second.ok) == ("deleted", True)
    assert not (results / OTHER).exists()


def test_a_removal_that_raises_is_that_ids_outcome_and_the_rest_still_go(env, monkeypatch):
    """A batch that let the exception out would discard the outcomes of the ids already
    deleted, and the caller would have no record of what is gone."""
    from robovast.service.interface import DeleteCampaignsRequest
    transport, results = env
    (results / OTHER).mkdir()
    real = transport._delete_deletable                              # noqa: SLF001

    def _explode(campaign_id):
        if campaign_id == CID:
            raise OSError(5, "Input/output error")
        return real(campaign_id)

    monkeypatch.setattr(transport, "_delete_deletable", _explode)

    res = transport.delete_campaigns(DeleteCampaignsRequest(campaign_ids=[CID, OTHER]))

    first, second = res.results
    assert (first.campaign_id, first.outcome, first.ok) == (CID, "partial", False)
    assert "Input/output error" in first.message
    assert (second.campaign_id, second.outcome, second.ok) == (OTHER, "deleted", True)
    assert not (results / OTHER).exists()


def test_the_single_delete_still_raises_where_the_batch_reports(env, monkeypatch):
    """The HTTP route maps these to 400 and 409; the batch turns them into outcomes."""
    transport, _results = env
    with pytest.raises(ValueError):
        transport.delete_campaign("..")
    _running(transport, monkeypatch, CID)
    with pytest.raises(RuntimeError):
        transport.delete_campaign(CID)


def test_the_cluster_lane_reaps_jobs_for_every_id_of_a_batch(env, monkeypatch):
    """Its extra cleanup hangs off the removal both deletes share, not off the single one."""
    from robovast.execution.cluster_execution import cluster_execution, pod_access
    from robovast.execution.cluster_execution.cluster_service import ClusterService
    from robovast.service.interface import DeleteCampaignsRequest

    transport, results = env
    (results / OTHER).mkdir()
    svc = ClusterService.__new__(ClusterService)
    svc.__dict__.update(transport.__dict__)
    svc.namespace, svc.kube_context = "ns", None
    reaped, secrets = [], []
    monkeypatch.setattr(cluster_execution, "cleanup_cluster_campaign",
                        lambda namespace, campaign, context: reaped.append(campaign))
    monkeypatch.setattr(pod_access, "delete_campaign_secret",
                        lambda core, namespace, cid: secrets.append(cid))
    monkeypatch.setattr(svc, "_k8s", lambda: None, raising=False)

    res = svc.delete_campaigns(DeleteCampaignsRequest(campaign_ids=[CID, OTHER, ".."]))

    assert [r.outcome for r in res.results] == ["deleted", "deleted", "invalid"]
    assert reaped == [CID, OTHER] and secrets == [CID, OTHER]


def test_the_route_answers_one_result_per_id_and_refuses_only_an_empty_call(env, monkeypatch):
    from fastapi.testclient import TestClient

    from robovast.service.app import build_app

    transport, _results = env
    _running(transport, monkeypatch, OTHER)
    try:
        with TestClient(build_app(transport)) as http:
            resp = http.post("/campaigns/delete", json={"campaign_ids": [CID, OTHER]})
            # A refusal of one id is not a refusal of the call.
            assert resp.status_code == 200
            assert [(r["campaign_id"], r["outcome"]) for r in resp.json()["results"]] == [
                (CID, "deleted"), (OTHER, "running")]
            assert http.post("/campaigns/delete", json={"campaign_ids": []}).status_code == 422
    finally:
        # The stand-in entry is not one the app's shutdown can stop.
        transport._campaigns.pop(OTHER, None)                       # noqa: SLF001


def test_the_http_client_posts_the_ids_to_the_shared_route(monkeypatch):
    from robovast.service.http_client import HTTPTransport
    from robovast.service.interface import DeleteCampaignsRequest, Routes

    sent = {}

    def _post(self, route, json=None, *, timeout=None, **params):
        sent.update(route=route, json=json, timeout=timeout)
        return {"results": [{"campaign_id": CID, "outcome": "deleted", "ok": True,
                             "message": "Deleted."}]}

    monkeypatch.setattr(HTTPTransport, "_post", _post)
    res = HTTPTransport("http://service.example").delete_campaigns(
        DeleteCampaignsRequest(campaign_ids=[CID]))
    assert sent["route"] == Routes.CAMPAIGNS_DELETE
    assert sent["json"] == {"campaign_ids": [CID]}
    # Longer than the default: the service answers only once every id is done.
    assert sent["timeout"] > 30
    assert res.results[0].outcome == "deleted"
