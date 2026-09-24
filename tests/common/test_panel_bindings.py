"""Declared markers resolve in Python as they do in the browser."""

import pytest
from pydantic import ValidationError

from robovast.common.panel_bindings import declared_markers

CONFIG = {
    "name": "cfg",
    "config": {
        "goal_pose": {"position": {"x": 2.5, "y": 0.0}, "orientation": {"yaw": 1.0}},
        "goals": [{"x": 1.0, "y": 1.0}, {"x": 2.0, "y": 2.0}],
    },
    "_path": [{"x": 0.0, "y": 0.0}, {"x": 2.5, "y": 0.0}],
    "_doorway": {"position": {"x": 0.0, "y": 1.5}, "orientation": {"yaw": 0.5}},
}


def test_a_literal_pose_is_drawn_where_it_says():
    [m] = declared_markers({"markers": [{"kind": "pose", "pos": [-2.5, 0.0], "yaw": 0.0,
                                         "label": "start", "color": "#60a5fa"}]}, CONFIG)
    assert m.model_dump(exclude_none=True) == {
        "kind": "pose", "pos": [-2.5, 0.0], "yaw": 0.0, "label": "start", "color": "#60a5fa",
        "group": "declared"}


def test_a_pose_states_the_placement_as_the_rest_of_the_file_does():
    [m] = declared_markers({"markers": [{"kind": "pose", "label": "goal",
                                         "pose": {"position": {"x": 2.5, "y": 0.0},
                                                  "orientation": {"yaw": 1.0}}}]}, CONFIG)
    assert (m.pos, m.yaw, m.label) == ([2.5, 0.0], 1.0, "goal")
    [m] = declared_markers({"markers": [{"kind": "pose", "pose": {"x": 1.0, "y": 2.0},
                                         "offset": [1.0, 0.0]}]}, CONFIG)
    assert m.pos == [2.0, 2.0]  # a bare position is a pose too, and the offset still applies


def test_a_placement_is_stated_once():
    with pytest.raises(ValidationError, match="drop 'pos'/'yaw'"):
        declared_markers({"markers": [{"kind": "pose", "pose": {"x": 1.0, "y": 2.0},
                                       "pos": [3.0, 4.0]}]}, CONFIG)
    with pytest.raises(ValidationError, match="needs a position"):
        declared_markers({"markers": [{"kind": "pose", "label": "nowhere"}]}, CONFIG)


def test_a_param_marker_follows_the_configuration():
    [m] = declared_markers({"markers": [{"kind": "pose", "param": "goal_pose", "label": "goal"}]},
                           CONFIG)
    assert (m.pos, m.yaw, m.label) == ([2.5, 0.0], 1.0, "goal")
    [m] = declared_markers({"markers": [{"kind": "pose", "param": "goal_pose", "yaw": 0.5}]},
                           CONFIG)
    assert m.yaw == 0.5  # a stated yaw wins over the parameter's


def test_a_param_the_configuration_lacks_draws_nothing():
    assert declared_markers({"markers": [{"kind": "pose", "param": "nowhere"}]}, CONFIG) == []
    assert declared_markers({"markers": [{"kind": "path", "internal": "_none"}]}, CONFIG) == []


def test_a_list_of_poses_is_one_marker_each_numbered():
    markers = declared_markers({"markers": [{"kind": "pose", "param": "goals"}]}, CONFIG)
    assert [(m.pos, m.label) for m in markers] == [([1.0, 1.0], "goals 1"),
                                                         ([2.0, 2.0], "goals 2")]


def test_an_internal_path_is_read_as_the_polyline_and_offset_applies():
    [m] = declared_markers({"markers": [{"kind": "path", "internal": "_path", "offset": [1.0, 0.0]}]},
                           CONFIG)
    assert m.points == [[1.0, 0.0], [3.5, 0.0]] and m.label == "_path"
    [m] = declared_markers({"markers": [{"kind": "pose", "param": "goal_pose",
                                         "offset": [-8.0, 0.0, 0.0]}]}, CONFIG)
    assert m.pos == [-5.5, 0.0]


def test_an_internal_pose_is_read_like_a_param_and_named_after_the_key():
    """`param:` and `internal:` are one question -- where the position comes from -- so a
    variation's leftover reads as a pose exactly as a scenario parameter does."""
    [m] = declared_markers({"markers": [{"kind": "pose", "internal": "_doorway"}]}, CONFIG)
    assert (m.pos, m.yaw, m.label) == ([0.0, 1.5], 0.5, "_doorway")


def test_an_internal_the_configuration_lacks_draws_nothing():
    """A pose silently drawn at the origin is a wrong answer; an absent one is a question."""
    assert declared_markers({"markers": [{"kind": "pose", "internal": "_absent"}]}, CONFIG) == []


def test_no_declaration_is_no_markers():
    assert declared_markers(None, CONFIG) == []
    assert declared_markers({"map": "files/x.yaml"}, CONFIG) == []
