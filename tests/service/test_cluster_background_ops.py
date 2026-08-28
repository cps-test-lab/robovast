# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The cluster lane's re-triggerable post-run operations dispatch at all.

``run_postprocessing`` / ``run_share`` are the two operations a caller reaches for
*after* a campaign has finished — exactly when the runs are already paid for and a
crash is most expensive. Both referenced a ``Phase`` that the module never imported,
so every call raised ``NameError`` before dispatching anything and surfaced as a bare
HTTP 500. Nothing covered them, so the name error survived; ruff had been reporting it
as F821 the whole time.

These tests stub the dispatcher, so the work closure never runs: no cluster, no object
store. They assert only that the call resolves its names and hands the right phase to
the dispatcher — which is all that was broken, and all that a unit test can honestly
claim here.
"""

from unittest.mock import MagicMock

import pytest

from robovast.execution.cluster_execution.cluster_service import ClusterService
from robovast.service.interface import ActionResult, RunPostprocessingRequest, RunShareRequest


@pytest.fixture
def svc():
    return ClusterService(namespace="ns", cluster_config_name="x",
                          cluster_config_kwargs={}, reap_on_start=False)


def _capture(svc, monkeypatch):
    seen = {}

    def _dispatch(campaign_id, *, phase, work):
        seen["campaign_id"] = campaign_id
        seen["phase"] = phase
        seen["work"] = work
        return ActionResult(ok=True, message="dispatched")

    monkeypatch.setattr(svc, "_dispatch_background", _dispatch)
    return seen


def test_run_postprocessing_dispatches_in_the_postprocessing_phase(svc, monkeypatch):
    seen = _capture(svc, monkeypatch)
    result = svc.run_postprocessing(RunPostprocessingRequest(campaign_id="camp-1"))
    assert result.ok
    assert seen["campaign_id"] == "camp-1"
    assert seen["phase"] == "postprocessing"
    assert callable(seen["work"])


def test_run_share_dispatches_in_the_sharing_phase(svc, monkeypatch):
    seen = _capture(svc, monkeypatch)
    result = svc.run_share(RunShareRequest(campaign_id="camp-1"))
    assert result.ok
    assert seen["campaign_id"] == "camp-1"
    assert seen["phase"] == "sharing"
    assert callable(seen["work"])


def test_postprocess_job_loads_the_given_context(monkeypatch):
    """Postprocessing must dial the context the campaign's Jobs were submitted with.

    The Kubernetes clients this path builds read whatever context is loaded at the moment
    they are constructed, so loading it is the whole of the requirement. It used to happen
    only as a side effect of the Kueue admission check that ran first, and when that check
    was called with no context at all it dialled the ambient kubeconfig while the campaign's
    Jobs had gone to the service's ``--context`` cluster. Postprocessing then failed against
    a cluster the campaign never used, and said so self-contradictorily: naming the
    configured API server as unreachable while quoting a timeout to a different address.

    Retiring Kueue deleted that check, and with it the incidental load — so this pins the
    load itself rather than the probe that used to imply it. The stub raises after recording,
    which stops the test where the context has been consumed and needs no cluster.
    """
    from robovast.execution.cluster_execution import (in_pod_storage, kube_client,
                                                      postprocess_job)

    seen = {}

    class _Stop(Exception):
        pass

    def _load(context=None, **kwargs):
        seen["context"] = context
        raise _Stop

    monkeypatch.setattr(kube_client, "load_kube_config", _load)
    monkeypatch.setattr(in_pod_storage, "campaign_storage_location",
                        lambda cfg, cid: ("bucket", "prefix"))
    cfg = MagicMock()
    cfg.get_s3_credentials.return_value = ("key", "secret")
    cfg.get_s3_endpoint.return_value = "http://localhost:9000"

    with pytest.raises(_Stop):
        postprocess_job.run_conversion_job(
            cfg, "camp-1", "ns", "img", ["echo"], kube_context="local")

    assert seen.get("context") == "local", (
        f"postprocessing did not load the caller's context: {seen}")


def test_postprocess_campaign_forwards_the_context(monkeypatch):
    """``postprocess_campaign`` hands its context to the Job it schedules."""
    from robovast.execution.cluster_execution import postprocess_job

    seen = {}

    class _Stop(Exception):
        pass

    def _conversion(*args, **kwargs):
        seen.update(kwargs)
        raise _Stop  # stop before the object-store sync, which needs a cluster

    monkeypatch.setattr(postprocess_job, "run_conversion_job", _conversion)
    monkeypatch.setattr(postprocess_job, "rosbag_commands_for", lambda *a, **k: ["echo"])
    monkeypatch.setattr(postprocess_job, "campaign_vast", lambda root: "vast")
    monkeypatch.setattr(postprocess_job, "campaign_execution_image", lambda root: "img")

    with pytest.raises(_Stop):
        postprocess_job.postprocess_campaign(
            MagicMock(), "camp-1", "/nonexistent", "ns", kube_context="local")
    assert seen.get("kube_context") == "local"


def test_run_share_streams_from_the_store_without_staging_the_campaign(svc, monkeypatch):
    """The export must not materialise the campaign in the pod first.

    It used to open with ``fetch_campaign(force=True)``: the whole campaign came down
    into this pod's scratch before a byte reached the share — a second full copy the pod
    has no room for at campaign scale, and a wait reported nowhere the campaign view
    looks. Only the few small objects carrying the campaign's *status* may be pulled,
    because the outcome is edited and published back.
    """
    seen = _capture(svc, monkeypatch)
    svc.run_share(RunShareRequest(campaign_id="camp-1"))

    def _no(*_a, **_k):
        raise AssertionError("the export fetched the campaign instead of streaming it")

    monkeypatch.setattr(svc, "fetch_campaign", _no)
    materialized = {}

    def _materialize(campaign_id, rel_paths, subject, **_k):
        materialized["paths"] = tuple(rel_paths)
        return svc._cache_dir(campaign_id)  # pylint: disable=protected-access

    monkeypatch.setattr(svc, "_materialize", _materialize)
    streamed = {}
    monkeypatch.setattr(svc, "_stream_campaign_to_share",
                        lambda cid, root, state: streamed.update(campaign_id=cid))
    monkeypatch.setattr(svc, "_publish_execution", lambda cid, root: None)
    monkeypatch.setattr(
        "robovast.execution.status_recovery.record_step_outcome",
        lambda *_a, **_k: MagicMock(share_error=None))

    seen["work"](MagicMock())

    assert streamed["campaign_id"] == "camp-1"
    # The status trio, and nothing that would drag the run tree down with it.
    assert materialized["paths"] == ClusterService._SHARE_STATUS_OBJECTS


def test_export_without_a_configured_share_says_so(svc, monkeypatch):
    """A share that is not configured must fail loudly, naming the variable to set."""
    from robovast.common.errors import CampaignConfigError
    from robovast.execution.cluster_execution import in_pod_upload

    monkeypatch.setattr(svc, "_campaign_is_here", lambda cid: True)
    monkeypatch.setattr(in_pod_upload, "load_provider_from_env", lambda: None)
    with pytest.raises(CampaignConfigError, match="ROBOVAST_SHARE_TYPE"):
        svc._stream_campaign_to_share("camp-1", "/nonexistent", MagicMock())  # pylint: disable=protected-access


def test_export_of_an_unknown_campaign_is_refused_before_anything_is_created(svc, monkeypatch):
    """An id that is not here must not produce a valid, empty archive on the share.

    ``_materialize`` skips an object that is absent and ``add_campaign_members`` tars an
    empty prefix without complaint, so the export would upload a well-formed archive
    holding nothing, under the campaign's name, and report success.
    """
    from robovast.execution.cluster_execution import in_pod_upload

    monkeypatch.setattr(svc, "_campaign_is_here", lambda cid: False)

    def _no_provider():
        raise AssertionError("the share was contacted for a campaign that is not here")

    monkeypatch.setattr(in_pod_upload, "load_provider_from_env", _no_provider)
    with pytest.raises(KeyError, match="camp-nope"):
        svc._stream_campaign_to_share("camp-nope", "/nonexistent", MagicMock())  # pylint: disable=protected-access


def test_export_of_a_campaign_with_no_frozen_config_is_refused(svc, monkeypatch):
    """Indexed is not the same as importable, and only the second decides the archive.

    A campaign that died before its ``_config/`` was published is a real shape: it is
    indexed, it lists, its ``_execution/`` is full of logs, and every byte of it tars and
    uploads happily. What comes out the far end is an archive whose only possible future is
    an ingest refusal on somebody else's service, after a full transfer, with the source
    out of reach. Found live -- a share was holding one.

    Only ``_config/`` is listed: nothing else decides the answer, and an export of a large
    campaign must not pay for its whole key listing twice.
    """
    from robovast.common.errors import CampaignConfigError
    from robovast.execution.cluster_execution import in_pod_upload

    listed = []

    class _Storage:
        def list_entries(self, bucket, prefix, delimited=False):
            listed.append(prefix)
            return [], []

    monkeypatch.setattr(svc, "_campaign_is_here", lambda cid: True)
    monkeypatch.setattr(in_pod_upload, "load_provider_from_env",
                        lambda: MagicMock(SHARE_TYPE="fake"))
    monkeypatch.setattr(svc, "_campaign_object_location",
                        lambda cid, **k: (_Storage(), "bucket", f"campaigns/{cid}/"))

    def _no_access(_provider):
        raise AssertionError("the share was contacted for an unexportable campaign")

    monkeypatch.setattr(in_pod_upload, "verify_share_access", _no_access)
    with pytest.raises(CampaignConfigError, match="_config/"):
        svc._stream_campaign_to_share("camp-1", "/nonexistent", MagicMock())  # pylint: disable=protected-access
    assert listed == ["campaigns/camp-1/_config"], "one listing, of the one prefix that decides"
