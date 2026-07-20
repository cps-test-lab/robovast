# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""PanelConfig schema + custom-panel validation.

Panel ``type`` is a core built-in, an installed ``robovast.panel_types`` entry point
(``robovast_nav``'s ``costmap``), or ``custom`` (a user-authored bundle referenced by path).
"""

import pytest
from pydantic import ValidationError

from robovast.common.config import PanelConfig
from robovast.common.config_validation import _panel_problems


def test_builtin_shorthand():
    assert PanelConfig.model_validate({"playback": None}).type == "playback"


def test_package_panel_accepted_when_installed():
    # robovast_nav registers the costmap panel_types entry point.
    assert PanelConfig.model_validate({"costmap": {"title": "Nav2"}}).type == "costmap"


def test_unknown_type_rejected():
    with pytest.raises(ValidationError):
        PanelConfig.model_validate({"totally_made_up": {}})


def test_custom_requires_remote():
    with pytest.raises(ValidationError):
        PanelConfig.model_validate({"custom": {"module": "./x"}})


def test_custom_accepted_with_remote():
    p = PanelConfig.model_validate({"custom": {"remote": "panels/x", "module": "./x"}})
    assert (p.type, p.remote, p.module) == ("custom", "panels/x", "./x")


def test_remote_only_on_custom():
    with pytest.raises(ValidationError):
        PanelConfig.model_validate({"scene": {"remote": "panels/x"}})


def test_validation_flags_missing_custom_bundle(tmp_path):
    raw = {"visualization": {"panels": [
        {"custom": {"remote": "panels/missing", "module": "./x"}}]}}
    problems = _panel_problems(raw, str(tmp_path))
    assert len(problems) == 1
    assert problems[0]["field"] == "visualization.panels[0].remote"


def test_validation_passes_when_bundle_present(tmp_path):
    (tmp_path / "panels" / "ok").mkdir(parents=True)
    (tmp_path / "panels" / "ok" / "remoteEntry.js").write_text("//\n")
    raw = {"visualization": {"panels": [{"custom": {"remote": "panels/ok"}}]}}
    assert _panel_problems(raw, str(tmp_path)) == []
