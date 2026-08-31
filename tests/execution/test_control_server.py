# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the live campaign state (``Status`` + ``ControllerState``).

The controller runs *in the driving process* (the CLI locally, the
robovast-service for cluster campaigns), so the service reads ``snapshot()``
directly. There is no HTTP ``/status`` + ``/command`` channel, no command RPC and
no upload-to-share retrigger over one: those exist only to reach a controller that
lives in its own pod. What is here is the state contract every surface depends on.
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


# -- the postprocessing step marker -----------------------------------------
#
# ``postprocessing`` has no run counter: every tally in ``runs`` is frozen and ``progress`` is
# pinned at 1.0, so on the evidence a reader had, a campaign spending half an hour converting a
# large run's rosbags and one stuck on a wedged step were the same campaign. The steps already
# narrate themselves through ``run_postprocessing(output_callback=...)``, so the marker is that
# narration published where a reader is already looking.


def test_each_step_line_becomes_the_live_stage():
    from robovast.execution.control_server import stage_output_callback
    state = ControllerState()
    state.set_phase(Phase.POSTPROCESSING)
    logged = []
    emit = stage_output_callback(state, logged.append)

    emit("[1/2] Executing: run_log")
    assert state.snapshot().stage == "[1/2] Executing: run_log"
    emit("  1000/1870 runs (53%)")
    assert state.snapshot().stage == "  1000/1870 runs (53%)"
    # Still logged: the marker is one line for a reader watching, the log is the full account.
    assert logged == ["[1/2] Executing: run_log", "  1000/1870 runs (53%)"]


def test_publishing_a_step_does_not_restart_the_phase_clock():
    """The marker must not be written with ``set_phase``: re-setting the phase would move
    ``phase_since``, turning "how long has this phase run" into "how long since its last
    step" — on a phase whose whole problem was that nobody could tell how far along it was."""
    from robovast.execution.control_server import stage_output_callback
    state = ControllerState()
    state.set_phase(Phase.POSTPROCESSING)
    started = state.snapshot().phase_since

    stage_output_callback(state, lambda _msg: None)("[2/2] Executing: resource_usage")

    assert state.snapshot().phase_since == started


def test_a_step_line_does_not_count_as_run_progress():
    """``progress_since`` tracks completed *runs*; postprocessing completes none. Moving it
    here would let a busy step stand in for a run finishing."""
    from robovast.execution.control_server import stage_output_callback
    state = ControllerState()
    state.set_phase(Phase.POSTPROCESSING)
    started = state.snapshot().progress_since

    stage_output_callback(state, lambda _msg: None)("Building data.db from 1870 run(s)")

    assert state.snapshot().progress_since == started


def test_without_a_state_it_is_just_the_logger():
    """The re-run entry points postprocess a campaign nothing is driving, so a caller never
    has to ask which case it is in."""
    from robovast.execution.control_server import stage_output_callback
    log = [].append          # bound in a name: each attribute access makes a *new* method object
    assert stage_output_callback(None, log) is log


def test_a_time_budget_does_not_count_as_progress():
    """Wall clock advancing is not evidence the campaign advanced.

    The regression this exists for: the obvious way to make a ``time`` budget tick live is to
    have the progress poller rewrite its ``current`` every few seconds. ``_progress_signal``
    includes each budget row's ``current``, so that would advance the signal on every poll
    forever -- ``progress_since`` would never age and no time-budgeted search could be
    reported stalled again, which is exactly the failure the signal exists to prevent (and
    the reason it is not ``updated_at``).

    The shipped design publishes the search's ORIGIN once and lets readers derive elapsed, so
    nothing rewrites the row. This asserts the *signal* is immune regardless, because the
    cheap fix will look tempting again.
    """
    from robovast.client.status import BudgetItem
    state = ControllerState()
    state.update(budget=[BudgetItem(label="time", current=0.0, limit=3600.0, kind="time")])
    started = state.snapshot().progress_since

    # A whole hour of "progress" on the clock alone, and nothing else moved.
    state.update(budget=[BudgetItem(label="time", current=3599.0, limit=3600.0, kind="time")])

    assert state.snapshot().progress_since == started


def test_a_counted_budget_does_count_as_progress():
    """The other half of the rule: a row whose change IS evidence of progress must stamp it,
    or a search advancing only its run count would be called stalled."""
    from robovast.client.status import BudgetItem
    state = ControllerState()
    state.update(budget=[BudgetItem(label="runs", current=8.0, limit=180.0, kind="runs")])
    started = state.snapshot().progress_since

    state.update(budget=[BudgetItem(label="runs", current=16.0, limit=180.0, kind="runs")])

    assert state.snapshot().progress_since > started
