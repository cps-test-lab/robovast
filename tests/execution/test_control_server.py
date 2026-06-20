# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the in-controller control channel (state + RPC)."""

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")

from robovast.execution.control_server import (ControllerState, build_app,
                                               dispatch, Command, register)
from fastapi.testclient import TestClient


def _client(state):
    return TestClient(build_app(state))


def test_status_reflects_state_updates():
    state = ControllerState()
    state.set_phase("running")
    state.update(mode="search", campaign_id="nav-x", batch=2, batches_done=2,
                 budget=[{"label": "batches", "current": 2.0, "limit": 10.0, "done": False}],
                 runs={"completed": 3, "total": 8}, best_objective=0.25,
                 batch_history=[{"idx": 0, "n_units": 4}, {"idx": 1, "n_units": 4}])
    body = _client(state).get("/status").json()
    assert body["phase"] == "running"
    assert body["mode"] == "search"
    assert body["batch"] == 2 and body["batches_done"] == 2
    assert body["runs"] == {"completed": 3, "total": 8}
    assert body["budget"][0]["label"] == "batches"
    assert body["best_objective"] == 0.25
    assert len(body["batch_history"]) == 2


def test_nan_budget_current_serialises_as_null():
    # The controller maps NaN (e.g. target_objective before any result) to None,
    # so /status stays valid JSON.
    state = ControllerState()
    state.update(budget=[{"label": "failure_rate", "current": None, "limit": 0.5}])
    body = _client(state).get("/status").json()
    assert body["budget"][0]["current"] is None


def test_stop_command_sets_event():
    state = ControllerState()
    assert state.stop_requested is False
    resp = _client(state).post("/command", json={"name": "stop"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert state.stop_requested is True


def test_unknown_command_returns_400():
    state = ControllerState()
    resp = _client(state).post("/command", json={"name": "does-not-exist"})
    assert resp.status_code == 400


def test_custom_handler_dispatch_and_error():
    state = ControllerState()

    @register("echo")
    def _echo(_state, **args):
        return args

    assert dispatch(state, Command(name="echo", args={"a": 1})).result == {"a": 1}

    @register("boom")
    def _boom(_state, **_a):
        raise RuntimeError("kaboom")

    result = dispatch(state, Command(name="boom"))
    assert result.ok is False and "kaboom" in result.error


def test_healthz():
    assert _client(ControllerState()).get("/healthz").json() == {"ok": True}


# -- upload-to-share retrigger plumbing -------------------------------------

def test_upload_to_share_command_requests_retrigger():
    state = ControllerState()
    resp = _client(state).post(
        "/command",
        json={"name": "upload-to-share", "args": {"ROBOVAST_WEBDAV_PASSWORD": "new"}})
    assert resp.status_code == 200 and resp.json()["ok"] is True
    action, overrides = state.wait_for_retrigger()
    assert action == "retrigger"
    assert overrides == {"ROBOVAST_WEBDAV_PASSWORD": "new"}


def test_upload_to_share_no_args_reuses_injected_creds():
    state = ControllerState()
    state.request_upload()           # manual retrigger with no overrides
    action, overrides = state.wait_for_retrigger()
    assert action == "retrigger" and overrides == {}


def test_stop_abandons_pending_upload_wait():
    # `stop` doubles as "give up on the upload"; wait_for_retrigger returns abandon.
    state = ControllerState()
    state.request_stop()
    action, overrides = state.wait_for_retrigger()
    assert action == "abandon" and overrides == {}
