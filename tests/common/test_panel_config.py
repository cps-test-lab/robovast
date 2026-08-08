# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""PanelConfig schema + custom-panel validation.

Panel ``type`` is a core built-in, an installed ``robovast.panel_types`` entry point
(``robovast_nav``'s ``costmap``), or ``custom`` (a user-authored bundle referenced by path).
"""

import pytest
from pydantic import ValidationError

from robovast.common.config import ConfigV1, PanelConfig
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


VEGA_SPEC = {"mark": "line", "encoding": {"x": {"field": "timestamp"}, "y": {"field": "v"}}}


def _vega(**props):
    return {"vega": {"source": {"table": "poses"}, "vega_lite": VEGA_SPEC, **props}}


def test_vega_accepted_in_both_forms():
    assert PanelConfig.model_validate(_vega()).type == "vega"
    explicit = PanelConfig.model_validate(
        {"type": "vega", "source": {"table": "poses"}, "vega_lite": VEGA_SPEC})
    assert explicit.type == "vega"


def test_vega_bindings_kept_as_passthrough():
    # The bindings are interpreted by the panel plugin, like every other panel's, so they must
    # survive validation in ``__pydantic_extra__`` rather than being dropped as unknown keys.
    p = PanelConfig.model_validate(_vega(title="base_link"))
    assert p.title == "base_link"
    assert p.__pydantic_extra__["vega_lite"] == VEGA_SPEC
    assert p.__pydantic_extra__["source"] == {"table": "poses"}


@pytest.mark.parametrize("props", [
    {"source": {"table": "poses"}},                          # no spec
    {"source": {"table": "poses"}, "vega_lite": {}},          # empty spec
    {"vega_lite": VEGA_SPEC},                                 # no source
    {"source": {"filter": {"frame": "base_link"}}, "vega_lite": VEGA_SPEC},  # source without table
])
def test_vega_rejects_incomplete_bindings(props):
    with pytest.raises(ValidationError):
        PanelConfig.model_validate({"vega": props})


def test_validation_reports_every_broken_vega_panel():
    # Why this check exists at all: the schema raises on the first bad panel, so a .vast with two
    # of them would only ever show one. ``validate_project`` is the collect-all report.
    raw = {"visualization": {"panels": [
        {"vega": {"source": {"table": "poses"}}},   # missing vega_lite
        {"vega": {"vega_lite": VEGA_SPEC}},         # missing source
    ]}}
    fields = [p["field"] for p in _panel_problems(raw, "/nonexistent")]
    assert fields == ["visualization.panels[0].vega_lite", "visualization.panels[1].source"]


def test_validation_passes_for_complete_vega_panel():
    raw = {"visualization": {"panels": [_vega()]}}
    assert _panel_problems(raw, "/nonexistent") == []


def test_json_schema_accepts_shorthand():
    # The web config editor validates the .vast against ConfigV1's JSON Schema. The default
    # schema requires a literal ``type`` property and flagged the shorthand every example
    # config uses as ``Missing property "type"``; the branches below are what fixes that.
    branches = ConfigV1.model_json_schema()["$defs"]["PanelConfig"]["anyOf"]
    assert {"type": "string"} in branches  # bare ``- playback``
    shorthand = next(b for b in branches if b.get("maxProperties") == 1)  # ``- costmap: {...}``
    props = shorthand["additionalProperties"]["anyOf"][1]["properties"]
    assert "title" in props and "position" in props and "type" not in props
    assert any("type" in b.get("required", []) for b in branches)  # explicit form still offered
