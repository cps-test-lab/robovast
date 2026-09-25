# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""ClusterService's re-triggerable post-run operations dispatch at all.

``run_postprocessing`` / ``run_share`` are the two operations a caller reaches for
*after* a campaign has finished — exactly when the runs are already paid for and a
crash is most expensive. The dispatch tests stub the dispatcher, so the work closure
never runs and no cluster is needed; they assert that the call resolves its names and
hands the right phase to the dispatcher.

Postprocessing runs in the service process, beside the campaign on the results volume: its
work calls the pipeline directly and records the verdict
into the campaign.
"""
from types import SimpleNamespace
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


def test_postprocessing_runs_in_the_service_process_and_records_its_verdict(
        tmp_path, monkeypatch):
    """No Job and no image: the work calls the pipeline on the campaign directory itself."""
    from robovast.execution import status_recovery
    from robovast.results_processing import postprocessing

    svc = ClusterService(namespace="ns", cluster_config_name="x", cluster_config_kwargs={},
                         reap_on_start=False, results_dir=str(tmp_path))
    (tmp_path / "camp-1").mkdir()
    seen = _capture(svc, monkeypatch)
    ran, recorded = {}, {}

    def _run(**kwargs):
        ran.update(kwargs)
        return True, "done"

    def _record(campaign_dir, postprocessing):
        recorded.update(campaign_dir=campaign_dir, outcome=postprocessing)
        return SimpleNamespace(postprocessed=True, postprocessing_error=None,
                               phase="finished")

    monkeypatch.setattr(postprocessing, "run_postprocessing", _run)
    monkeypatch.setattr(status_recovery, "record_step_outcome", _record)
    notifier = MagicMock()
    monkeypatch.setattr(svc, "_notifier", lambda campaign_id: notifier)

    svc.run_postprocessing(RunPostprocessingRequest(campaign_id="camp-1", force=True))
    state = MagicMock()
    seen["work"](state)

    assert ran["campaign"] == "camp-1"
    assert ran["results_dir"] == str(tmp_path)
    assert ran["force"] is True
    assert recorded == {"campaign_dir": tmp_path / "camp-1", "outcome": (True, "done")}
    state.set_phase.assert_called_with("finished")
    notifier.postprocessed.assert_called_once()
