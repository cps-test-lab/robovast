# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""upload-to-share runs before postprocessing and never loses the campaign.

``_finish_campaign`` must call the backend's ``share_campaign`` hook (the raw,
pre-postprocess snapshot) *before* ``_chain_postprocessing``, only when the toggle
is set, skipped on a cooperative stop, and isolated so a share failure still lets
postprocessing + finalize run.
"""

import types

from robovast.execution import controller
from robovast.execution.backends import RunOptions
from robovast.execution.control_server import Phase, Status


def _state(stop_requested, phase=Phase.RUNNING):
    """A control-channel double whose snapshot is a real :class:`Status`.

    Ending a campaign reads the run tallies and the postprocess/share error fields to
    say what it actually produced, so a hand-rolled namespace would have to grow a
    field every time that message does. The real model cannot drift from itself.
    """
    ns = types.SimpleNamespace(stop_requested=stop_requested, fields={})
    ns.status = Status(phase=phase)
    ns.update = lambda **kw: (ns.fields.update(kw),
                              [setattr(ns.status, k, v) for k, v in kw.items()])
    ns.set_phase = lambda p, **kw: setattr(ns.status, "phase", p)
    ns.snapshot = lambda: ns.status
    return ns


class _RecordingBackend:
    def __init__(self, fail=False):
        self.calls = []
        self._fail = fail
        self.progress_callback = None

    def share_campaign(self, campaign_root, options, progress_callback=None):
        # The callback is part of the hook: the controller builds one per attempt so the
        # campaign view can show an upload bar. It went unpassed for a long time, which is
        # why this fake records that it arrives at all.
        self.calls.append("share")
        self.progress_callback = progress_callback
        if self._fail:
            raise RuntimeError("share boom")


def _patch_tail(monkeypatch, calls):
    monkeypatch.setattr(controller, "_chain_postprocessing",
                        lambda *a, **k: calls.append("postprocess"))
    monkeypatch.setattr(controller, "_finalize",
                        lambda *a, **k: calls.append("finalize"))
    # The finish tail now writes one durable outcome after share+postprocess; these
    # tests cover ordering/isolation, so stub the recorder out.
    monkeypatch.setattr(controller, "_record_controller_outcome",
                        lambda *a, **k: None)


def test_share_runs_before_postprocessing(monkeypatch):
    backend = _RecordingBackend()
    _patch_tail(monkeypatch, backend.calls)
    state = _state(False)
    controller._finish_campaign(
        backend, "/root", "camp-1", state, RunOptions(upload_to_share=True))
    assert backend.calls == ["share", "postprocess", "finalize"]
    # A successful share clears the failure marker.
    assert state.fields.get("share_error") is None


def test_share_skipped_when_toggle_off(monkeypatch):
    backend = _RecordingBackend()
    _patch_tail(monkeypatch, backend.calls)
    controller._finish_campaign(
        backend, "/root", "camp-1", _state(False), RunOptions(upload_to_share=False))
    assert backend.calls == ["postprocess", "finalize"]


def test_share_skipped_on_stop(monkeypatch):
    backend = _RecordingBackend()
    _patch_tail(monkeypatch, backend.calls)
    controller._finish_campaign(
        backend, "/root", "camp-1", _state(True), RunOptions(upload_to_share=True))
    assert backend.calls == []  # stop skips the whole tail


def test_share_failure_still_runs_postprocess_and_finalize(monkeypatch):
    backend = _RecordingBackend(fail=True)
    _patch_tail(monkeypatch, backend.calls)
    state = _state(False)
    controller._finish_campaign(
        backend, "/root", "camp-1", state, RunOptions(upload_to_share=True))
    # share raised but was isolated; postprocess + finalize still ran.
    assert backend.calls == ["share", "postprocess", "finalize"]
    # The failure is recorded on its own field (not swallowed), not raised.
    assert state.fields.get("share_error")


class _RecordingNotifier:
    """The notifier surface ``_finish_campaign`` now drives.

    Beyond ``uploaded`` it also ends the campaign, which stops the heartbeat and sends
    exactly one terminal message — see ``controller.end_campaign``.
    """

    def __init__(self):
        self.uploaded_with = []
        self.finished_with = []
        self.heartbeat_stopped = False

    def uploaded(self, share_type):
        self.uploaded_with.append(share_type)

    def stop_heartbeat(self):
        self.heartbeat_stopped = True

    def finished(self, summary, *, degraded=False):
        self.finished_with.append((summary, degraded))


def test_notifier_uploaded_fires_on_successful_share(monkeypatch):
    monkeypatch.setenv("ROBOVAST_SHARE_TYPE", "gcs")
    backend = _RecordingBackend()
    _patch_tail(monkeypatch, backend.calls)
    notifier = _RecordingNotifier()
    controller._finish_campaign(
        backend, "/root", "camp-1", _state(False), RunOptions(upload_to_share=True),
        notifier)
    assert notifier.uploaded_with == ["gcs"]  # the configured share type


def test_notifier_uploaded_not_fired_when_share_fails(monkeypatch):
    monkeypatch.setenv("ROBOVAST_SHARE_TYPE", "gcs")
    backend = _RecordingBackend(fail=True)
    _patch_tail(monkeypatch, backend.calls)
    notifier = _RecordingNotifier()
    controller._finish_campaign(
        backend, "/root", "camp-1", _state(False), RunOptions(upload_to_share=True),
        notifier)
    assert notifier.uploaded_with == []  # failed share → no "uploaded" notification
