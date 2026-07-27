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

import pytest

from robovast.service.cluster_service import ClusterService
from robovast.service.interface import (ActionResult, RunPostprocessingRequest,
                                        RunShareRequest)


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
