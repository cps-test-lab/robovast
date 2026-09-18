"""Declared markers resolve in Python as they do in the browser."""

from robovast.common.panel_bindings import declared_markers

CONFIG = {
    "name": "cfg",
    "config": {
        "goal_pose": {"position": {"x": 2.5, "y": 0.0}, "orientation": {"yaw": 1.0}},
        "goals": [{"x": 1.0, "y": 1.0}, {"x": 2.0, "y": 2.0}],
    },
    "_path": [{"x": 0.0, "y": 0.0}, {"x": 2.5, "y": 0.0}],
}


def test_a_literal_pose_is_drawn_where_it_says():
    [m] = declared_markers({"markers": [{"kind": "pose", "pos": [-2.5, 0.0], "yaw": 0.0,
                                         "label": "start", "color": "#60a5fa"}]}, CONFIG)
    assert m == {"kind": "pose", "pos": [-2.5, 0.0], "yaw": 0.0, "label": "start",
                 "color": "#60a5fa", "group": "declared"}


def test_a_param_marker_follows_the_configuration():
    [m] = declared_markers({"markers": [{"kind": "pose", "param": "goal_pose", "label": "goal"}]},
                           CONFIG)
    assert (m["pos"], m["yaw"], m["label"]) == ([2.5, 0.0], 1.0, "goal")
    [m] = declared_markers({"markers": [{"kind": "pose", "param": "goal_pose", "yaw": 0.5}]},
                           CONFIG)
    assert m["yaw"] == 0.5  # a stated yaw wins over the parameter's


def test_a_param_the_configuration_lacks_draws_nothing():
    assert declared_markers({"markers": [{"kind": "pose", "param": "nowhere"}]}, CONFIG) == []
    assert declared_markers({"markers": [{"kind": "path", "internal": "_none"}]}, CONFIG) == []


def test_a_list_of_poses_is_one_marker_each_numbered():
    markers = declared_markers({"markers": [{"kind": "pose", "param": "goals"}]}, CONFIG)
    assert [(m["pos"], m["label"]) for m in markers] == [([1.0, 1.0], "goals 1"),
                                                         ([2.0, 2.0], "goals 2")]


def test_an_internal_path_is_read_as_the_polyline_and_offset_applies():
    [m] = declared_markers({"markers": [{"kind": "path", "internal": "_path", "offset": [1.0, 0.0]}]},
                           CONFIG)
    assert m["points"] == [[1.0, 0.0], [3.5, 0.0]] and m["label"] == "_path"
    [m] = declared_markers({"markers": [{"kind": "pose", "param": "goal_pose",
                                         "offset": [-8.0, 0.0, 0.0]}]}, CONFIG)
    assert m["pos"] == [-5.5, 0.0]


def test_no_declaration_is_no_markers():
    assert declared_markers(None, CONFIG) == []
    assert declared_markers({"map": "files/x.yaml"}, CONFIG) == []
