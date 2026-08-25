# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""A campaign queued behind other campaigns is not stalled.

The no-progress deadline is a **per-run** budget measured against a **per-run** signal:
``progress_since`` moves when a run completes, and it is reset at the start of each batch.
Between those two moments the clock counts submission, staging AND the time the batch's
pods spend waiting for capacity -- which the campaign does not control and cannot shorten.

``stall_report`` already refuses to judge a phase that executes no runs, on exactly this
reasoning: "nothing can advance that signal, so ... passing the budget says only that the
phase outlasted one run. That is arithmetic, not a stall." A batch whose every job is queued
is the same situation inside ``running``, and was the one case the rule did not cover.

The cluster backend already separates the two futures a non-starting pod can have --
``contended`` (waiting its turn for capacity or an image pull, recovers by itself) from
``blocked`` (cannot start at all) -- and gives them different tolerances. Only the first
suppresses a verdict: a pod that can never start is a real failure and must stay visible.
"""

import time

import pytest

from robovast.client.status import Status, stall_report


def _status(**fields):
    base = {"phase": "running", "progress_since": time.time() - 5000,
            "progress_deadline_s": 600}
    base.update(fields)
    return Status(**base)


def test_a_queued_campaign_gets_no_verdict_rather_than_a_stall():
    """`None`, not `False`: no verdict is possible, which is not the same as healthy."""
    report = stall_report(_status(waiting_for_capacity=True))
    assert report["stalled"] is None
    assert "stall_reason" not in report, "a queued campaign must not be accused"
    assert "capacity" in report["stall_verdict"].lower()


def test_the_same_campaign_is_stalled_once_it_is_running():
    """The clock is meaningful again the moment the batch has capacity: a run that has
    started and then produces nothing for 5000s IS wedged, and must still be caught."""
    report = stall_report(_status(waiting_for_capacity=False))
    assert report["stalled"] is True
    assert "stall_reason" in report


def test_the_default_changes_nothing():
    """A campaign whose backend never reports the flag -- the local lane, which has no
    queue -- behaves exactly as before."""
    assert stall_report(_status()) == stall_report(_status(waiting_for_capacity=False))


def test_a_queued_campaign_still_reports_its_age():
    """Suppressing the verdict must not hide how long it has been waiting; that is the
    number an operator acts on."""
    report = stall_report(_status(waiting_for_capacity=True))
    assert report["progress_age_s"] > 4000


def test_waiting_does_not_resurrect_a_terminal_campaign():
    report = stall_report(_status(phase="finished", waiting_for_capacity=True))
    assert report == {}


# -- which jobs count as "nothing can run" -----------------------------------

def _pred(remaining, contended):
    from robovast.execution.cluster_execution.kubernetes_backend import \
        all_jobs_waiting_for_capacity
    return all_jobs_waiting_for_capacity(remaining, contended)


def test_every_remaining_job_queued_means_nothing_can_run():
    assert _pred(["a", "b"], {"a": "waiting", "b": "waiting"}) is True


def test_one_job_running_means_progress_is_still_possible():
    """The verdict must stand while ANY job of the batch can run -- that job completing is
    exactly the progress the deadline is watching for."""
    assert _pred(["a", "b"], {"a": "waiting"}) is False


def test_a_job_that_cannot_start_is_not_merely_queued():
    """`blocked` but not `contended` -- an image that does not exist, a request no node can
    hold. It looks the same in ten minutes as in one, and suppressing the verdict for it
    would hide a real failure behind a queue that does not exist."""
    assert _pred(["a"], {}) is False


def test_no_remaining_jobs_is_not_waiting():
    assert _pred([], {}) is False


@pytest.mark.parametrize("contended", [None, {}])
def test_an_unknown_probe_result_does_not_suppress(contended):
    """The probe returns ``None`` when it could not check this cycle. Treating unknown as
    "queued" would silence the deadline exactly when the cluster is unreadable."""
    assert _pred(["a"], contended) is False


# -- the flag is published, and does not outlive the wait --------------------

class _Recorder:
    """Stands in for the control state, capturing what the loop publishes."""

    def __init__(self):
        self.writes = []
        self.stop_requested = False

    def update(self, **fields):
        self.writes.append(fields.get("waiting_for_capacity"))


def _runner(state):
    from robovast.execution.cluster_execution.kubernetes_backend import BatchJobRunner

    runner = BatchJobRunner.__new__(BatchJobRunner)
    runner._state = state
    runner._batch_tag = "batch-0"
    return runner


def test_the_wait_is_actually_published():
    """The half a passing reader-test cannot see: with nothing writing the flag the whole
    fix is inert, and every campaign reports False forever."""
    state = _Recorder()
    _runner(state)._publish_capacity_wait(True)
    assert state.writes == [True]


def test_publishing_is_safe_without_a_control_state():
    """The local lane has no queue and no state to tell; reporting one must never be able
    to fail a batch."""
    _runner(None)._publish_capacity_wait(True)      # must not raise


def test_a_failing_status_write_does_not_break_the_batch():
    class _Broken(_Recorder):
        def update(self, **fields):
            raise RuntimeError("status unreachable")

    _runner(_Broken())._publish_capacity_wait(True)  # must not raise


def test_the_loop_clears_the_flag_when_the_batch_is_done():
    """Cleared on the way out rather than left to the next batch's first probe: in between
    the campaign is still `running`, and a stale True would suppress a verdict for a batch
    that is not queued."""
    import inspect

    from robovast.execution.cluster_execution.kubernetes_backend import BatchJobRunner

    source = inspect.getsource(BatchJobRunner.run_batch_in_pod)
    exit_block = source[source.index("if not remaining:"):]
    assert "_publish_capacity_wait(False)" in exit_block.split("break", maxsplit=1)[0], (
        "the batch loop exits without clearing the capacity-wait flag")
