# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""What a variation contributes to the config view.

The replacement for the desktop editor's ``GUI_RENDERER_CLASS``: a variation returns
neutral geometry (:mod:`robovast.common.scene_markers`) and the panels draw it, so the same
contribution serves the 3D scene and the 2D map.
"""

import pytest
from pydantic import ValidationError

from robovast.common.scene_markers import (ConfigViewContribution, SceneMarker,
                                           collect_contributions)
from robovast.common.variation import Variation
from robovast_nav import config_view
from robovast_nav.data_model import Orientation, Pose, Position, StaticObject


def _pose(x, y, yaw=0.0):
    return Pose(position=Position(x=x, y=y), orientation=Orientation(yaw=yaw))


def _nav_config(**overrides):
    """A resolved config shaped the way PathVariationRandom + ObstacleVariation leave one."""
    config = {
        "name": "c0",
        "config": {
            "start_pose": _pose(1.0, 2.0, 0.5),
            "goal_poses": [_pose(5.0, 6.0, 1.0)],
            "static_objects": [StaticObject(
                entity_name="obstacle_0", model="file:///m/box.sdf.xacro",
                xacro_arguments="width:=0.9, length:=0.9, height:=2.0",
                spawn_pose=_pose(3.0, 4.0, 0.2))],
            "map_file": "environments/office/map.yaml",
        },
        "sim": {"plugins.boxes.instances": [
            {"name": "obstacle_0", "pos": [3.0, 4.0], "size": [0.5, 0.5, 1.0], "yaw": 0.2}]},
        "_path": [Position(x=1.0, y=2.0), Position(x=5.0, y=6.0)],
        "_goal_parameter_name": "goal_poses",
        "_objects_parameter_name": "static_objects",
    }
    config.update(overrides)
    return config


# -- the marker vocabulary --------------------------------------------------

def test_a_marker_without_its_geometry_is_refused():
    # A marker that draws nothing draws nothing *silently*, which is the exact failure
    # "the variation contributed a preview and the view stayed empty" comes from.
    with pytest.raises(ValidationError):
        SceneMarker(kind="box", pos=[0, 0])          # no size
    with pytest.raises(ValidationError):
        SceneMarker(kind="path")                      # no points
    with pytest.raises(ValidationError):
        SceneMarker(kind="cylinder", pos=[0, 0])     # no radius


def test_contributions_merge_and_are_grouped_by_their_variation():
    class Left(Variation):
        @classmethod
        def config_view_data(cls, config, base_path):
            return ConfigViewContribution(markers=[SceneMarker(kind="point", pos=[0, 0])],
                                          files={"map": "a.yaml"})

    class Right(Variation):
        @classmethod
        def config_view_data(cls, config, base_path):
            return ConfigViewContribution(markers=[SceneMarker(kind="point", pos=[1, 1])])

    out = collect_contributions({}, [Left, Right], "")
    assert [m["group"] for m in out["markers"]] == ["Left", "Right"]
    assert out["files"] == {"map": "a.yaml"}
    assert out["errors"] == []


def test_a_broken_hook_is_reported_and_the_others_still_draw():
    class Broken(Variation):
        @classmethod
        def config_view_data(cls, config, base_path):
            raise RuntimeError("no map")

    class Fine(Variation):
        @classmethod
        def config_view_data(cls, config, base_path):
            return ConfigViewContribution(markers=[SceneMarker(kind="point", pos=[2, 2])])

    out = collect_contributions({}, [Broken, Fine], "")
    assert len(out["markers"]) == 1
    assert out["errors"] == ["Broken: no map"]


def test_a_variation_that_places_nothing_contributes_nothing():
    class Plain(Variation):
        pass

    assert collect_contributions({}, [Plain], "") == {"markers": [], "files": {}, "errors": []}


# -- the nav port -----------------------------------------------------------

def test_path_contribution_draws_the_path_and_its_endpoints():
    markers = config_view.path_contribution(_nav_config()).markers
    kinds = [(m.kind, m.label) for m in markers]
    assert kinds == [("path", "planned path"), ("pose", "start"), ("pose", "goal")]
    assert markers[0].points == [[1.0, 2.0], [5.0, 6.0]]


def test_goals_are_read_from_the_parameter_the_campaign_named():
    # The campaign chooses what its goal parameter is called and the variation records the
    # resolved name; guessing a fixed one drew nothing the moment an author picked a third.
    config = _nav_config()
    config["config"]["waypoints"] = config["config"].pop("goal_poses")
    config["_goal_parameter_name"] = "waypoints"
    labels = [m.label for m in config_view.path_contribution(config).markers]
    assert "goal" in labels


def test_several_goals_are_numbered():
    config = _nav_config()
    config["config"]["goal_poses"] = [_pose(5, 6), _pose(7, 8), _pose(9, 10)]
    labels = [m.label for m in config_view.path_contribution(config).markers if m.kind == "pose"]
    assert labels == ["start", "goal 1", "goal 2", "goal 3"]


def test_obstacle_extents_come_from_what_the_campaign_declared():
    # The sim channel's `size` (0.5) wins over the spawner's xacro arguments (0.9). The
    # desktop renderer parsed the argument string instead, and fell back to a default shape
    # whenever it could not -- so the drawn obstacle and the compiled one disagreed.
    boxes = [m for m in config_view.obstacle_contribution(_nav_config()).markers
             if m.kind == "box"]
    assert len(boxes) == 1
    assert boxes[0].size == [0.5, 0.5, 1.0]
    assert boxes[0].pos == [3.0, 4.0]
    assert boxes[0].label == "obstacle_0"


def test_a_spawn_only_campaign_falls_back_to_the_xacro_arguments():
    # No `instances` binding: a run-time spawner is the only description of the obstacle,
    # so its argument string is the only place the extents exist.
    config = _nav_config(sim={})
    boxes = [m for m in config_view.obstacle_contribution(config).markers if m.kind == "box"]
    assert boxes[0].size == [0.9, 0.9, 2.0]


def test_the_map_file_is_offered_to_panels_that_need_one():
    assert config_view.path_contribution(_nav_config()).files == {
        "map": "environments/office/map.yaml"}


def test_an_absolute_recorded_map_is_not_offered():
    # `_map_file` is an absolute path on whichever host composed; a browser fetching
    # through the workspace can do nothing with it.
    config = _nav_config()
    config["config"].pop("map_file")
    config["_map_file"] = "/tmp/generated/map.yaml"
    assert config_view.path_contribution(config).files == {}


def test_the_trigger_variation_adds_its_spawn_point():
    config = _nav_config(_spawn_trigger_point=Position(x=2.5, y=3.5))
    markers = config_view.trigger_contribution(config).markers
    assert [m.label for m in markers if m.kind == "sphere"] == ["spawn trigger"]
