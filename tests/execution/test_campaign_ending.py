# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A campaign ends exactly once, and not before its results exist.

Publishing ``finished`` the moment the run loop returns — while share and postprocessing
are still to come — is believed by every reader: a waiter returns "done" for a campaign
with no metrics, and the ntfy message says so on a phone nobody re-reads. So ``run()``
stops at ``finishing`` there, and ``end_campaign`` publishes the terminal phase from
whichever scope is outermost for the lane.

The tests that matter here are the two failure directions, because they are opposites and
a fix for one produces the other: ending *too early* is the original bug; never ending at
all strands every waiter until its timeout, which is worse.
"""

import types

import pytest

from robovast.execution import controller
from robovast.execution.backends import RunOptions
from robovast.execution.control_server import Phase, Status


def _state(phase=Phase.RUNNING, **fields):
    """A control channel whose snapshot is a real :class:`Status`.

    The ending reads run tallies and the postprocess/share error fields to say what the
    campaign produced, so a hand-rolled namespace would need a new attribute every time
    that message grows. The real model cannot drift from itself.
    """
    ns = types.SimpleNamespace(stop_requested=False)
    ns.status = Status(phase=phase, **fields)
    ns.set_phase = lambda p, **kw: setattr(ns.status, "phase", p)
    ns.update = lambda **kw: [setattr(ns.status, k, v) for k, v in kw.items()]
    ns.snapshot = lambda: ns.status
    return ns


class _Notifier:
    def __init__(self):
        self.sent = []
        self.heartbeat_stopped = False

    def stop_heartbeat(self):
        self.heartbeat_stopped = True

    def finished(self, summary, *, degraded=False):
        self.sent.append(("finished", summary, degraded))

    def stopped(self, summary):
        self.sent.append(("stopped", summary, False))

    def failed(self, reason):
        self.sent.append(("failed", reason, True))


@pytest.fixture(autouse=True)
def _no_side_effects(monkeypatch):
    """Neutralise the finish tail's real work; these tests are about the ending."""
    monkeypatch.setattr(controller, "_chain_postprocessing", lambda *a, **k: None)
    monkeypatch.setattr(controller, "_finalize", lambda *a, **k: None)
    monkeypatch.setattr(controller, "_record_controller_outcome", lambda *a, **k: None)


# -- who publishes the terminal phase ---------------------------------------

def test_finish_tail_ends_the_campaign_when_it_is_outermost():
    """Cluster lanes and the bare CLI: nothing happens after this tail, so it ends."""
    state = _state()
    controller._finish_campaign(object(), "/root", "c1", state,
                                RunOptions(finalize_phase=True))
    assert state.status.phase == Phase.FINISHED


def test_finish_tail_leaves_the_campaign_open_when_it_is_not_outermost():
    """The local service runs postprocessing *after* this returns.

    Ending here would republish the original bug on that lane: terminal before the
    metrics exist. The worker ends it instead (see LocalTransport._drive_campaign).
    """
    state = _state()
    controller._finish_campaign(object(), "/root", "c1", state,
                                RunOptions(finalize_phase=False))
    assert state.status.phase == Phase.RUNNING


def test_campaign_still_ends_when_the_finish_tail_raises(monkeypatch):
    """The regression this design can cause, and the reason end_campaign runs in a
    ``finally``: a campaign left non-terminal blocks every waiter until its timeout."""
    monkeypatch.setattr(controller, "_chain_postprocessing",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    state = _state()
    with pytest.raises(RuntimeError):
        controller._finish_campaign(object(), "/root", "c1", state,
                                    RunOptions(finalize_phase=True))
    assert state.status.phase == Phase.FINISHED


@pytest.mark.parametrize("terminal", [Phase.FAILED, Phase.STOPPED])
def test_an_already_terminal_phase_is_not_overwritten(terminal):
    """FINISHED over FAILED would paint a failed campaign green."""
    state = _state(phase=terminal)
    controller.end_campaign("c1", state)
    assert state.status.phase == terminal


def test_ending_twice_is_harmless():
    state = _state()
    controller.end_campaign("c1", state)
    controller.end_campaign("c1", state)
    assert state.status.phase == Phase.FINISHED


# -- what the one notification says ------------------------------------------

def test_the_heartbeat_stops_only_at_the_end():
    """It deliberately outlives run(): share and postprocessing are the longest stretch
    in which nothing else reports, so stopping it there leaves that window silent."""
    notifier = _Notifier()
    controller.end_campaign("c1", _state(), notifier)
    assert notifier.heartbeat_stopped


def test_a_clean_finish_is_reported_clean():
    notifier = _Notifier()
    state = _state(runs={"completed": 20, "total": 20}, postprocessed=True)
    controller.end_campaign("c1", state, notifier)
    kind, summary, degraded = notifier.sent[0]
    assert (kind, degraded) == ("finished", False)
    assert "20/20 runs" in summary and "postprocessed" in summary


def test_a_postprocessing_failure_is_not_reported_as_success():
    """The runs are the deliverable, so the phase stays ``finished`` — but there are no
    CSVs and no data.db, and sent as a clean finish that read as success on a phone."""
    notifier = _Notifier()
    state = _state(runs={"completed": 20, "total": 20},
                   postprocessing_error="conversion died")
    controller.end_campaign("c1", state, notifier)
    kind, summary, degraded = notifier.sent[0]
    assert (kind, degraded) == ("finished", True)
    assert "POSTPROCESSING FAILED" in summary


def test_failed_trials_make_the_finish_degraded():
    notifier = _Notifier()
    state = _state(runs={"completed": 20, "total": 20, "failed": 2})
    controller.end_campaign("c1", state, notifier)
    _, summary, degraded = notifier.sent[0]
    assert degraded and "2 failed trial(s)" in summary


def test_a_stopped_campaign_says_so():
    """Silence was indistinguishable from a campaign still running."""
    notifier = _Notifier()
    controller.end_campaign("c1", _state(phase=Phase.STOPPED), notifier)
    assert notifier.sent[0][0] == "stopped"


def test_a_failure_is_announced_from_the_recorded_error():
    """Also covers failures from *before* the controller ran — an image build that could
    not resolve — which nothing announced at all when run() owned this message."""
    notifier = _Notifier()
    state = _state(phase=Phase.FAILED, error="image build failed: no such base")
    controller.end_campaign("c1", state, notifier)
    kind, reason, _ = notifier.sent[0]
    assert kind == "failed" and "image build failed" in reason


def test_exactly_one_terminal_message_per_campaign():
    notifier = _Notifier()
    controller._finish_campaign(object(), "/root", "c1", _state(),
                                RunOptions(finalize_phase=True), notifier)
    assert len(notifier.sent) == 1


def test_two_scopes_ending_the_same_campaign_send_one_message():
    """Both scopes legitimately end a campaign on the cluster lane.

    ``ClusterService`` subclasses ``LocalTransport``, so the service worker's ``finally``
    ends every campaign it drives — it has to, because one that failed before the builder
    ran (an image build that could not resolve) never reaches the finish tail at all. On
    the lanes where the finish tail is *also* outermost, both fire. The phase is
    idempotent; two "Campaign finished" pushes to someone's phone are not.
    """
    from robovast.execution.notify import Notifier

    notifier = Notifier("c1", topic="t")
    sent = []
    notifier._send = lambda msg, **kw: sent.append(msg)
    state = _state()
    controller._finish_campaign(object(), "/root", "c1", state,
                                RunOptions(finalize_phase=True), notifier)
    controller.end_campaign("c1", state, notifier)  # the worker's finally
    assert len(sent) == 1


def test_retriggered_is_not_a_terminal_message():
    """A re-run says where it went without spending the source's one end-of-life message.

    ``retriggered`` reports on the SOURCE campaign's topic, and the source is unmodified by
    a re-run -- so it must not go through ``_announce_terminal``, which would swallow the real
    "Campaign finished" that is still to come.
    """
    from robovast.execution.notify import Notifier

    notifier = Notifier("c1", topic="t")
    sent = []
    notifier._send = lambda msg, **kw: sent.append(msg)

    notifier.retriggered("c2")
    notifier.finished("2 runs")

    assert len(sent) == 2
    assert "c2" in sent[0]
    assert "finished" in sent[1].lower()


def test_retriggered_is_silent_without_a_topic(monkeypatch):
    """Unconfigured stays a no-op, like every other event -- no request is attempted."""
    import requests

    from robovast.execution.notify import Notifier

    def _refuse(*args, **kwargs):
        raise AssertionError("a disabled notifier must not reach the network")

    monkeypatch.setattr(requests, "post", _refuse)

    notifier = Notifier("c1")           # no topic -> disabled
    assert not notifier.enabled
    notifier.retriggered("c2")          # must not raise


def test_upload_failed_is_not_a_terminal_message():
    """A failed share leaves the campaign finished, so it must not spend its last message.

    A re-triggered upload runs long after the campaign ended, and it can fail on its own
    (credentials rotated, bucket gone) without anything being wrong with the campaign. Had
    this gone through ``_announce_terminal`` -- or through ``failed`` -- it would claim the
    campaign died, and swallow the finish of whatever ends next.
    """
    from robovast.execution.notify import Notifier

    notifier = Notifier("c1", topic="t")
    sent = []
    notifier._send = lambda msg, **kw: sent.append(msg)

    notifier.upload_failed("bucket rejected the upload")
    notifier.finished("2 runs")

    assert len(sent) == 2
    assert "bucket rejected the upload" in sent[0]
    assert "Campaign FAILED" not in sent[0]  # the upload failed, not the campaign
    assert "finished" in sent[1].lower()


def test_upload_failed_is_silent_without_a_topic(monkeypatch):
    """Unconfigured stays a no-op, like every other event -- no request is attempted."""
    import requests

    from robovast.execution.notify import Notifier

    def _refuse(*args, **kwargs):
        raise AssertionError("a disabled notifier must not reach the network")

    monkeypatch.setattr(requests, "post", _refuse)

    notifier = Notifier("c1")                     # no topic -> disabled
    assert not notifier.enabled
    notifier.upload_failed("nowhere to send it")  # must not raise


def test_postprocessing_failed_is_not_a_terminal_message():
    """A re-run of postprocessing that fails is not the campaign failing.

    Inside a campaign this case is the campaign's ending, and ``finished(degraded=True)``
    says so. Re-triggered from the service it is not: the trials are long done and on
    disk, and only the derived data is missing -- so it must leave the terminal message
    for whatever actually ends next.
    """
    from robovast.execution.notify import Notifier

    notifier = Notifier("c1", topic="t")
    sent = []
    notifier._send = lambda msg, **kw: sent.append(msg)

    notifier.postprocessing_failed("the metrics step found no bags")
    notifier.finished("2 runs")

    assert len(sent) == 2
    assert "the metrics step found no bags" in sent[0]
    assert "Campaign FAILED" not in sent[0]
    assert "finished" in sent[1].lower()


def test_a_cancelled_postprocessing_is_not_announced_as_a_failure():
    """A re-run of postprocessing can be stopped, and a stop is not a fault.

    The operator asked for it, so labelling it FAILED files a deliberate act under faults
    and sends whoever reads the message looking for a fault that is not there — the same
    distinction the campaign's own stop already makes.
    """
    from robovast.execution.notify import Notifier

    notifier = Notifier("c1", topic="t")
    sent = []
    notifier._send = lambda msg, **kw: sent.append(msg)

    notifier.postprocessing_cancelled("postprocessing cancelled by stop request")

    assert len(sent) == 1
    assert "CANCELLED" in sent[0] and "FAILED" not in sent[0]
    # Not terminal: the campaign's trials are untouched and something else ends it.
    notifier.finished("2 runs")
    assert "finished" in sent[1].lower()
