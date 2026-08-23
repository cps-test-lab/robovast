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


from robovast.client.status import Phase
from robovast.execution.control_server import ControllerState, Status


def test_snapshot_reflects_state_updates():
    state = ControllerState()
    state.set_phase("running")
    state.update(mode="search", campaign_id="nav-x", batch=2, batches_done=2,
                 budget=[{"label": "batches", "current": 2.0, "limit": 10.0, "done": False}],
                 runs={"completed": 3, "total": 8}, best_objective=0.25)
    body = state.snapshot().model_dump()
    assert body["phase"] == "running"
    assert body["mode"] == "search"
    assert body["batch"] == 2 and body["batches_done"] == 2
    # no_result (delivered nothing), failed (delivered a failing verdict), killed
    # (an operator stopped its job) and invalid (the runner threw the trial away) are
    # distinct counters — see RunProgress.
    assert body["runs"] == {"completed": 3, "total": 8, "no_result": 0, "failed": 0,
                            "killed": 0, "invalid": 0}
    assert body["budget"][0]["label"] == "batches"
    assert body["best_objective"] == 0.25
    # The per-batch trajectory is deliberately NOT here: it lived on this payload as
    # `batch_history`, was read by nothing, and grew with the batch count on a status every
    # campaign card polls. It is served by GET /campaigns/{id}/search/history instead.
    assert "batch_history" not in body


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


def test_status_reports_the_share_phase():
    # There is deliberately no share_provider field: what is on the share is the
    # share's state, and a copy of it here went stale (and, in fact, was never
    # written at all). The phase is what a campaign knows about its own upload.
    state = ControllerState()
    state.set_phase(Phase.SHARING, stage="upload-to-share")
    snap = state.snapshot()
    assert snap.phase == "sharing" and snap.stage == "upload-to-share"


def test_error_is_part_of_the_status_contract():
    """A failed campaign explains itself here — the one place every surface reads."""
    state = ControllerState()
    state.update(error="No configs matched pattern 'typo*'.\nAvailable configs:\n  - a")
    state.set_phase("failed")
    snap = state.snapshot()
    assert snap.phase == "failed"
    # pylint: disable-next=unsupported-membership-test  -- error was set two lines up
    assert "Available configs" in snap.error


def test_status_defaults():
    s = Status()
    # A Status that exists but has not been advanced claims only that it was accepted:
    # "starting" would assert pre-flight it has not done.
    assert s.phase == "initializing"
    assert s.phase_since > 0
    assert s.error is None
    assert s.runs.completed == 0


def test_phase_since_tracks_changes_only():
    """The phase clock must measure *this* phase, not the last write.

    ``phase_since`` exists so a reader can tell a slow pre-run step from a wedged one;
    if a defensive re-set of the current phase restarted the clock, a campaign stuck in
    one phase could keep reporting a fresh age forever.
    """
    state = ControllerState()
    state.set_phase("building")
    first = state.snapshot().phase_since
    state.set_phase("building", stage="still going")
    assert state.snapshot().phase_since == first
    state.set_phase("running")
    assert state.snapshot().phase_since > first


def test_a_stage_does_not_outlive_its_phase():
    """``stage`` describes the phase that set it, so the next phase starts without one.

    The bug this pins: a campaign that waited for an image kept reporting "waiting for image(s)
    simulation, sut" as its stage while it ran, while it postprocessed, and after it had
    finished. A sentence that was true once, still being served as a statement about now.
    """
    state = ControllerState()
    state.set_phase("building", stage="waiting for image(s) simulation, sut")
    assert state.snapshot().stage == "waiting for image(s) simulation, sut"

    state.set_phase("running")

    assert state.snapshot().stage is None
    state.set_phase("finished")
    assert state.snapshot().stage is None, "a finished campaign is not waiting for anything"


def test_re_setting_the_same_phase_keeps_its_stage():
    """Several paths re-set the current phase defensively. Clearing there would wipe a stage the
    same phase had just set -- which is why this clears on a *change* and not on every call."""
    state = ControllerState()
    state.set_phase("postprocessing", stage="merging results")
    state.set_phase("postprocessing")

    assert state.snapshot().stage == "merging results"


def test_a_new_phase_may_bring_its_own_stage():
    """The clear is a default, not a rule: a phase change that names a stage keeps that one."""
    state = ControllerState()
    state.set_phase("building", stage="waiting for image(s) sut")
    state.set_phase("finished", stage="postprocessing failed: no results")

    assert state.snapshot().stage == "postprocessing failed: no results"
