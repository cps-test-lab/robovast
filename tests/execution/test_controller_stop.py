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


def _state(stop_requested):
    return types.SimpleNamespace(stop_requested=stop_requested)


def test_finish_campaign_skips_work_when_stopped(monkeypatch):
    calls = []
    monkeypatch.setattr(controller, "_chain_postprocessing",
                        lambda *a, **k: calls.append("postprocess"))
    monkeypatch.setattr(controller, "_finalize",
                        lambda *a, **k: calls.append("finalize"))

    controller._finish_campaign(object(), "/root", "camp-1", _state(True), None)

    assert calls == []  # neither postprocessing nor finalize attempted


def test_finish_campaign_runs_normally_when_not_stopped(monkeypatch):
    calls = []
    monkeypatch.setattr(controller, "_chain_postprocessing",
                        lambda *a, **k: calls.append("postprocess"))
    monkeypatch.setattr(controller, "_finalize",
                        lambda *a, **k: calls.append("finalize"))

    controller._finish_campaign(object(), "/root", "camp-1", _state(False), None)

    assert calls == ["postprocess", "finalize"]  # postprocess before finalize


def test_finish_campaign_runs_when_no_state(monkeypatch):
    """No control channel (e.g. a bare CLI run) still finishes normally."""
    calls = []
    monkeypatch.setattr(controller, "_chain_postprocessing",
                        lambda *a, **k: calls.append("postprocess"))
    monkeypatch.setattr(controller, "_finalize",
                        lambda *a, **k: calls.append("finalize"))

    controller._finish_campaign(object(), "/root", "camp-1", None, None)

    assert calls == ["postprocess", "finalize"]
