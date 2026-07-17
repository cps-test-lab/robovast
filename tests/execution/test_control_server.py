# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the live campaign state (``Status`` + ``ControllerState``).

The controller runs *in the driving process* now (the CLI locally, the
robovast-service for cluster campaigns), so the service reads ``snapshot()``
directly. The HTTP ``/status`` + ``/command`` channel this module used to serve —
along with the command RPC and the upload-to-share retrigger — is gone: it existed
only to reach a controller that lived in its own pod. Those tests went with it;
what remains is the state contract every surface still depends on.
"""

import pytest

from robovast.execution.control_server import ControllerState, Status


def test_snapshot_reflects_state_updates():
    state = ControllerState()
    state.set_phase("running")
    state.update(mode="search", campaign_id="nav-x", batch=2, batches_done=2,
                 budget=[{"label": "batches", "current": 2.0, "limit": 10.0, "done": False}],
                 runs={"completed": 3, "total": 8}, best_objective=0.25,
                 batch_history=[{"idx": 0, "n_units": 4}, {"idx": 1, "n_units": 4}])
    body = state.snapshot().model_dump()
    assert body["phase"] == "running"
    assert body["mode"] == "search"
    assert body["batch"] == 2 and body["batches_done"] == 2
    assert body["runs"] == {"completed": 3, "total": 8}
    assert body["budget"][0]["label"] == "batches"
    assert body["best_objective"] == 0.25
    assert len(body["batch_history"]) == 2


def test_snapshot_is_a_copy():
    """Readers must not observe half-applied updates from the worker thread."""
    state = ControllerState()
    state.update(runs={"completed": 1, "total": 4})
    snap = state.snapshot()
    state.update(runs={"completed": 2, "total": 4})
    assert snap.runs.completed == 1


def test_nan_budget_current_serialises_as_null():
    # The controller maps NaN (e.g. target_objective before any result) to None,
    # so the status stays valid JSON over the service's HTTP contract.
    state = ControllerState()
    state.update(budget=[{"label": "failure_rate", "current": None, "limit": 0.5}])
    body = state.snapshot().model_dump_json()
    assert '"current":null' in body


def test_request_stop_sets_event():
    """`stop` is now a direct in-process call from the service, not an HTTP command."""
    state = ControllerState()
    assert state.stop_requested is False
    state.request_stop()
    assert state.stop_requested is True


def test_status_reports_share_provider():
    # share_provider tracks the current upload attempt; upload-to-share is a
    # stateless service call now, but the phase/provider it reports is unchanged.
    state = ControllerState()
    state.update(share_provider="sftp")
    state.set_phase("uploading", stage="upload-to-share")
    snap = state.snapshot()
    assert snap.share_provider == "sftp"
    assert snap.phase == "uploading" and snap.stage == "upload-to-share"


def test_error_is_part_of_the_status_contract():
    """A failed campaign explains itself here — the one place every surface reads."""
    state = ControllerState()
    state.update(error="No configs matched pattern 'typo*'.\nAvailable configs:\n  - a")
    state.set_phase("failed")
    snap = state.snapshot()
    assert snap.phase == "failed"
    assert "Available configs" in snap.error


def test_status_defaults():
    s = Status()
    assert s.phase == "starting"
    assert s.error is None
    assert s.runs.completed == 0
