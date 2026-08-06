# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``gui`` must reach *both* config-generation paths, or a windowed run stays headless.

``execution.local.gui.parameter_overrides`` is what actually un-headlesses a scenario, and
it is selected at generation time — there is no run-time route, because
``SCENARIO_EXECUTION_PARAMETERS`` carries only runner flags and ``scenario_execution`` has
no per-parameter override.

The flag therefore has to arrive twice: in the staged ``scenario.config``
(``prepare_campaign_configs``) and in the packed job documents the local run actually
mounts (``generate_compose_run_script``). Dropping it in either place produces the exact
silent failure the feature exists to avoid — a run that mounts a display and draws nothing —
so this asserts the forwarding rather than trusting the default.
"""

from robovast.execution.backends import RunOptions, stage_run_script


def _capture(monkeypatch):
    """Replace both generators with recorders, returning the kwargs they saw."""
    seen = {}

    def _prepare(out_dir, campaign_data, cluster=False, instance_type_command=None,
                 gui=False):
        seen["prepare_gui"] = gui

    def _generate(*args, **kwargs):
        seen["generate_gui"] = kwargs.get("gui")

    monkeypatch.setattr("robovast.execution.backends.prepare_campaign_configs", _prepare)
    monkeypatch.setattr("robovast.execution.backends.generate_compose_run_script",
                        _generate)
    monkeypatch.setattr("robovast.execution.backends.resolve_robovast_image",
                        lambda **kwargs: "img")
    return seen


def test_a_windowed_run_forwards_gui_to_both_generators(monkeypatch, tmp_path):
    seen = _capture(monkeypatch)
    stage_run_script({"execution": {}}, str(tmp_path), 1, RunOptions(gui=True),
                     results_dir=str(tmp_path))
    assert seen == {"prepare_gui": True, "generate_gui": True}


def test_a_headless_run_forwards_the_absence_just_as_explicitly(monkeypatch, tmp_path):
    seen = _capture(monkeypatch)
    stage_run_script({"execution": {}}, str(tmp_path), 1, RunOptions(gui=False),
                     results_dir=str(tmp_path))
    assert seen == {"prepare_gui": False, "generate_gui": False}
