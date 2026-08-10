# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""PanelConfig schema + custom-panel validation.

Panel ``type`` is a core built-in, an installed ``robovast.panel_types`` entry point
(``robovast_nav``'s ``costmap``), or ``custom`` (a user-authored bundle referenced by path).
"""

import pytest
from pydantic import ValidationError

from robovast.common.config import (ConfigV1, PanelConfig, PanelPosition,
                                    VisualizationConfig)
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


@pytest.mark.parametrize("anchor", [
    "top", "bottom", "left", "right",
    "top-left", "top-right", "bottom-left", "bottom-right",
    "top-center", "bottom-center", "left-center", "right-center",
    "center",
])
def test_every_anchor_accepted(anchor):
    assert PanelPosition(anchor=anchor).anchor == anchor


def test_fill_replaces_the_anchor():
    p = PanelConfig.model_validate({"scene3d": {"position": {"fill": True}}})
    assert (p.position.fill, p.position.anchor) == (True, None)


@pytest.mark.parametrize("position", [
    {"fill": True, "anchor": "left"},    # fill is used instead of an anchor, not with one
    {"fill": True, "width": 400},        # a filling panel is sized by the docks around it
    {"fill": True, "height": "40%"},
    {"anchor": "top", "width": 400},     # a full-width bar's width is ignored
    {"anchor": "bottom", "width": 400},
    {"anchor": "fill"},                  # fill stopped being an anchor
])
def test_placement_rejects_combinations_it_could_not_honour(position):
    # Each of these used to be silently dropped by the layout engine, which is how a panel ends
    # up somewhere its author did not ask for and cannot explain from the .vast.
    with pytest.raises(ValidationError):
        PanelPosition.model_validate(position)


def test_only_one_panel_may_fill():
    with pytest.raises(ValidationError):
        VisualizationConfig.model_validate({"panels": [
            {"scene3d": {"position": {"fill": True}}},
            {"camera": {"position": {"fill": True}}},
        ]})


def _column(*heights):
    """A ``left`` column of members with the given heights (``None`` = takes the rest)."""
    return {"panels": [
        {"scenario_tree": {"position": {
            "anchor": "left", "width": 320,
            **({} if h is None else {"height": h})}}}
        for h in heights
    ]}


@pytest.mark.parametrize("heights", [
    ("50%", "50%"),        # split by ratio
    ("70%", "30%"),
    (550, None),           # exact pixels, then the rest
    (None,),               # one member takes the whole column
    ("calc(50% - 44px)", None),
])
def test_column_members_may_be_sized_by_ratio_pixels_or_remainder(heights):
    assert len(VisualizationConfig.model_validate(_column(*heights)).panels) == len(heights)


@pytest.mark.parametrize("heights", [
    (None, "50%"),         # the first takes the rest; the second would land on top of it
    ("50%", None, "20%"),
])
def test_only_the_last_column_member_may_omit_its_height(heights):
    with pytest.raises(ValidationError):
        VisualizationConfig.model_validate(_column(*heights))


def test_a_hidden_fill_panel_does_not_count():
    # Hidden panels are filtered out before layout, so they occupy no rectangle to collide over.
    vis = VisualizationConfig.model_validate({"panels": [
        {"scene3d": {"position": {"fill": True}}},
        {"camera": {"position": {"fill": True}, "hidden": True}},
    ]})
    assert len(vis.panels) == 2


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
