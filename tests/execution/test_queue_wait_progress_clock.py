# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Leaving a capacity queue restarts the progress clock.

``waiting_for_capacity`` suppresses the stall verdict only WHILE the wait lasts. Without
restarting the clock when it ends, the accusation lands the instant the campaign starts
doing the thing it was queued for -- which is the worst possible moment.
"""

import time

from robovast.client.status import Phase, stall_report
from robovast.execution.control_server import ControllerState


def _queued_then_running(queue_seconds):
    state = ControllerState()
    state.set_phase(Phase.RUNNING)
    state.update(waiting_for_capacity=True, progress_deadline_s=300)
    # Pretend the queue lasted longer than one run's budget.
    state._status.progress_since = time.time() - queue_seconds
    state.update(waiting_for_capacity=False)
    return state._status


def test_a_campaign_that_queued_past_the_deadline_is_not_called_stalled():
    """The measured case: 337s behind its own calibration probe, both runs healthy and
    seconds old, reported ``stalled: true``."""
    report = stall_report(_queued_then_running(337))
    assert report["stalled"] is False, report
    assert report["progress_age_s"] < 5, "the clock restarts when the wait ends"


def test_the_verdict_is_still_suppressed_during_the_wait():
    state = ControllerState()
    state.set_phase(Phase.RUNNING)
    state.update(waiting_for_capacity=True, progress_deadline_s=300)
    state._status.progress_since = time.time() - 900
    report = stall_report(state._status)
    assert report["stalled"] is None
    assert report["progress_age_s"] > 800, "the wait's length is still reported"


def test_a_genuine_stall_is_still_reported():
    """The clock restarts once, on leaving the queue -- it does not keep excusing a run that
    then wedges. Suppressing a real stall is the failure this must not trade for."""
    status = _queued_then_running(337)
    status.progress_since = time.time() - 400
    report = stall_report(status)
    assert report["stalled"] is True
    assert "no progress for" in report["stall_reason"]


def test_a_campaign_that_never_queued_is_untouched():
    state = ControllerState()
    state.set_phase(Phase.RUNNING)
    state.update(progress_deadline_s=300)
    state._status.progress_since = time.time() - 400
    state.update(waiting_for_capacity=False)
    assert stall_report(state._status)["stalled"] is True, (
        "a false->false write must not reset the clock and mask a real stall")
