# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A cooperative stop (Ctrl+C / Stop button) is a clean terminal, not a failure.

On Ctrl+C the cluster storage tunnel dies with the process group, so the builders'
finish tail (result download → postprocessing → finalize upload) would only fail
noisily against a dead endpoint. ``_finish_campaign`` must skip it entirely when a
stop was requested, leaving the already-uploaded per-run results as-is.
"""

import types

from robovast.execution import controller
from robovast.execution.control_server import Phase


def _state(stop_requested, phase=Phase.RUNNING):
    """A control-channel double whose phase actually moves.

    ``_finish_campaign`` now *ends* the campaign as well as finishing its work, so a
    double that cannot record a phase change would pass while the thing under test
    silently failed to publish one.
    """
    st = types.SimpleNamespace(stop_requested=stop_requested, phase=phase)
    st.snapshot = lambda: types.SimpleNamespace(phase=st.phase)
    st.set_phase = lambda p, **kw: setattr(st, "phase", p)
    return st


def test_finish_campaign_skips_work_when_stopped(monkeypatch):
    calls = []
    monkeypatch.setattr(controller, "_chain_postprocessing",
                        lambda *a, **k: calls.append("postprocess"))
    monkeypatch.setattr(controller, "_finalize",
                        lambda *a, **k: calls.append("finalize"))

    state = _state(True)
    controller._finish_campaign(object(), "/root", "camp-1", state, None)

    assert calls == []  # neither postprocessing nor finalize attempted
    # ...but the campaign is still ended. Skipping the work is not the same as leaving
    # the campaign non-terminal: a stop that never published one would hang every
    # waiter until its timeout and look identical to a campaign still running.
    assert state.phase == Phase.FINISHED


def test_finish_campaign_runs_normally_when_not_stopped(monkeypatch):
    calls = []
    monkeypatch.setattr(controller, "_chain_postprocessing",
                        lambda *a, **k: calls.append("postprocess"))
    monkeypatch.setattr(controller, "_finalize",
                        lambda *a, **k: calls.append("finalize"))

    state = _state(False)
    controller._finish_campaign(object(), "/root", "camp-1", state, None)

    assert calls == ["postprocess", "finalize"]  # postprocess before finalize
    assert state.phase == Phase.FINISHED


def test_finish_campaign_skips_postprocess_but_finalizes_on_failure(monkeypatch):
    """A FAILED campaign skips postprocessing (its campaign_root is incomplete, so it
    would only raise a misleading 'no .vast under _config') but still finalizes so the
    failure outcome is published."""
    calls = []
    monkeypatch.setattr(controller, "_chain_postprocessing",
                        lambda *a, **k: calls.append("postprocess"))
    monkeypatch.setattr(controller, "_finalize",
                        lambda *a, **k: calls.append("finalize"))

    state = _state(False, phase=Phase.FAILED)
    controller._finish_campaign(object(), "/root", "camp-1", state, None)

    assert calls == ["finalize"]  # postprocessing skipped, finalize still runs
    # FAILED is already terminal, so ending the campaign must not overwrite it with
    # FINISHED — that would paint a failed campaign green.
    assert state.phase == Phase.FAILED


def test_finish_campaign_runs_when_no_state(monkeypatch):
    """No control channel (e.g. a bare CLI run) still finishes normally."""
    calls = []
    monkeypatch.setattr(controller, "_chain_postprocessing",
                        lambda *a, **k: calls.append("postprocess"))
    monkeypatch.setattr(controller, "_finalize",
                        lambda *a, **k: calls.append("finalize"))

    controller._finish_campaign(object(), "/root", "camp-1", None, None)

    assert calls == ["postprocess", "finalize"]
