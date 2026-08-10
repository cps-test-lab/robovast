# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""Output slots: how a variation with several outputs says where each one lands.

The single-output form (``scenario: goal_pose``) is covered by test_sim_channel.py. This
file is about the case that form cannot express -- a plugin whose outputs are several, whose
*names* the campaign chooses, and which may straddle the two channels.
"""

import pytest
from pydantic import ValidationError

from robovast.common.variation.base_variation import (SCENARIO_CHANNEL,
                                                      SIM_CHANNEL,
                                                      DestinationConfig,
                                                      Variation)


class OneOutput(DestinationConfig):
    """A generic value-setting plugin: one output, no slots."""


class TwoOutputs(DestinationConfig):
    """A generator producing a map and a mesh -- the FloorplanGeneration shape."""

    SLOTS = ("map", "mesh")


# -- single output ------------------------------------------------------------------------

def test_a_single_output_takes_a_bare_name():
    cfg = OneOutput(scenario="goal_pose")
    assert cfg.channel == SCENARIO_CHANNEL
    assert cfg.destination == "goal_pose"
    assert cfg.outputs() == {SCENARIO_CHANNEL: ["goal_pose"]}


def test_a_single_output_refuses_a_slot_mapping():
    """A mapping here means the author thinks the plugin has slots. It does not."""
    with pytest.raises(ValidationError, match="single output"):
        OneOutput(scenario={"map": "map_file"})


# -- several outputs ----------------------------------------------------------------------

def test_slots_may_straddle_both_channels():
    """The point of slots: one plugin, two artifacts, opposite sides of the compile boundary."""
    cfg = TwoOutputs(scenario={"map": "map_file"},
                     sim={"mesh": "plugins.floorplan.mesh"})
    assert cfg.binding("map") == (SCENARIO_CHANNEL, "map_file")
    assert cfg.binding("mesh") == (SIM_CHANNEL, "plugins.floorplan.mesh")
    assert cfg.outputs() == {SCENARIO_CHANNEL: ["map_file"],
                             SIM_CHANNEL: ["plugins.floorplan.mesh"]}


def test_both_slots_may_share_one_channel():
    cfg = TwoOutputs(scenario={"map": "map_file", "mesh": "mesh_file"})
    assert cfg.outputs() == {SCENARIO_CHANNEL: ["map_file", "mesh_file"]}


def test_an_unbound_slot_is_refused_naming_it():
    """Silence here would mean an artifact the plugin produced reaching nothing."""
    with pytest.raises(ValidationError, match="unbound: mesh"):
        TwoOutputs(scenario={"map": "map_file"})


def test_an_unknown_slot_is_refused_naming_the_real_ones():
    with pytest.raises(ValidationError, match="its outputs are: map, mesh"):
        TwoOutputs(scenario={"map": "map_file", "meshh": "x"})


def test_a_slot_cannot_go_to_both_channels():
    with pytest.raises(ValidationError, match="goes to one channel"):
        TwoOutputs(scenario={"map": "map_file", "mesh": "a"}, sim={"mesh": "b"})


def test_slots_refuse_a_bare_name():
    """The retired positional form: which of two outputs would a bare name be?"""
    with pytest.raises(ValidationError, match="mapping of slot to destination"):
        TwoOutputs(scenario="map_file")


def test_the_retired_name_key_is_refused_in_both_forms():
    for kwargs in ({"name": "goal_pose"}, {"name": ["map_file", "mesh_file"]}):
        with pytest.raises(ValidationError, match="no longer names a destination"):
            TwoOutputs(**kwargs)


# -- what a plugin declares ----------------------------------------------------------------

class _Slotted(Variation):
    CONFIG_CLASS = TwoOutputs


class _Undeclared(Variation):
    """A third-party plugin with a config of its own and no opinion about outputs."""

    CONFIG_CLASS = None


def test_declared_outputs_comes_from_the_binding():
    cfg = TwoOutputs(scenario={"map": "map_file"}, sim={"mesh": "plugins.floorplan.mesh"})
    assert _Slotted.declared_outputs(cfg) == {
        SCENARIO_CHANNEL: ["map_file"],
        SIM_CHANNEL: ["plugins.floorplan.mesh"],
    }


def test_a_plugin_that_declares_nothing_is_undeclared_not_empty():
    """``{}`` means "not pre-checked", which is every plugin's state before slots existed."""
    assert _Undeclared.declared_outputs(object()) == {}


# -- routing --------------------------------------------------------------------------------

def test_update_slots_routes_each_output_to_its_channel():
    """The atomicity that matters: both channels written by one call, from one set of values."""
    variation = _Slotted.__new__(_Slotted)
    variation.parameters = TwoOutputs(scenario={"map": "map_file"},
                                      sim={"mesh": "plugins.floorplan.mesh"})
    variation._config_child_indices = {}

    out = variation.update_slots({"name": "cfg"},
                                 {"map": "maps/a.yaml", "mesh": "3d/a.stl"})

    assert out["config"] == {"map_file": "maps/a.yaml"}
    assert out["sim"] == {"plugins.floorplan.mesh": "3d/a.stl"}


# -- the shape of a one-or-many output ------------------------------------------------------

class _GoalShape(DestinationConfig):
    SLOTS = ("start", "goal")


def _path_variation(scenario_file, binding):
    """A PathVariationRandom wired up far enough to answer the shape question."""
    from robovast_nav.variation.path_variation import PathVariationRandom

    variation = PathVariationRandom.__new__(PathVariationRandom)
    variation.parameters = _GoalShape(scenario=binding)
    variation.scenario_file = str(scenario_file)
    return variation


def _write_scenario(tmp_path, goal_decl):
    osc = tmp_path / "scenario.osc"
    osc.write_text(
        "scenario test_scenario:\n"
        "    start_pose: pose_3d\n"
        f"    {goal_decl}\n"
        "    do serial:\n"
        "        wait elapsed(1s)\n")
    return osc


def test_a_list_declaration_gives_a_list(tmp_path):
    osc = _write_scenario(tmp_path, "goal_poses: list of pose_3d")
    v = _path_variation(osc, {"start": "start_pose", "goal": "goal_poses"})
    assert v._goal_destination() == ("goal_poses", False)


def test_a_scalar_declaration_gives_one_pose(tmp_path):
    """Same plugin, same binding name style -- the scenario decides, not the name."""
    osc = _write_scenario(tmp_path, "goal_pose: pose_3d")
    v = _path_variation(osc, {"start": "start_pose", "goal": "goal_pose"})
    assert v._goal_destination() == ("goal_pose", True)


def test_the_name_no_longer_decides_the_shape(tmp_path):
    """A destination called `goal_pose` declared as a list is a list.

    The retired rule compared the name to the literal string "goal_pose", so this case
    produced a single pose for a parameter the scenario declared as a list.
    """
    osc = _write_scenario(tmp_path, "goal_pose: list of pose_3d")
    v = _path_variation(osc, {"start": "start_pose", "goal": "goal_pose"})
    assert v._goal_destination() == ("goal_pose", False)


def test_an_undeclared_destination_is_refused_not_guessed(tmp_path):
    osc = _write_scenario(tmp_path, "goal_pose: pose_3d")
    v = _path_variation(osc, {"start": "start_pose", "goal": "waypoints"})
    with pytest.raises(ValueError, match="not declared"):
        v._goal_destination()


def test_goal_cannot_be_bound_to_the_sim_channel(tmp_path):
    """There is no declaration to read a shape from on that side."""
    osc = _write_scenario(tmp_path, "goal_pose: pose_3d")
    v = _path_variation(osc, {"start": "start_pose", "goal": "goal_pose"})
    v.parameters = _GoalShape(scenario={"start": "start_pose"},
                              sim={"goal": "plugins.x.y"})
    with pytest.raises(ValueError, match="cannot be bound to the 'sim' channel"):
        v._goal_destination()


# -- one placement, two channels -------------------------------------------------------------

def test_obstacle_geometry_is_derived_from_the_campaigns_own_extents():
    """The two channels must describe the same box, not the simulator's copy of a default."""
    from robovast_nav.variation.obstacle_variation import _size_from_xacro

    assert _size_from_xacro("width:=0.5, length:=0.8, height:=1.0") == [0.5, 0.8, 1.0]
    assert _size_from_xacro("width:=0.5 length:=0.8 height:=1.0") == [0.5, 0.8, 1.0]


def test_obstacle_size_is_absent_rather_than_guessed():
    """No extents stated means the placement plugin's own default, not a number invented here."""
    from robovast_nav.variation.obstacle_variation import _size_from_xacro

    assert _size_from_xacro("") is None
    assert _size_from_xacro("radius:=0.3") is None
    assert _size_from_xacro("width:=wide, length:=0.8, height:=1.0") is None


def test_an_optional_slot_may_be_left_unbound():
    """A campaign whose simulator spawns at run time has no destination for the geometry."""
    from robovast_nav.variation.obstacle_variation import ObstacleVariationConfig

    cfg = ObstacleVariationConfig(scenario={"objects": "static_objects"},
                                  obstacle_configs=[], seed=1, robot_diameter=0.35)
    assert cfg.is_bound("objects") and not cfg.is_bound("instances")
    assert cfg.outputs() == {SCENARIO_CHANNEL: ["static_objects"]}


def test_binding_the_optional_slot_puts_it_on_the_sim_channel():
    from robovast_nav.variation.obstacle_variation import ObstacleVariationConfig

    cfg = ObstacleVariationConfig(scenario={"objects": "static_objects"},
                                  sim={"instances": "plugins.obstacles.instances"},
                                  obstacle_configs=[], seed=1, robot_diameter=0.35)
    assert cfg.is_bound("instances")
    assert cfg.outputs()[SIM_CHANNEL] == ["plugins.obstacles.instances"]


# -- answers that need the simulator ---------------------------------------------------------

def test_a_world_with_no_campaign_parent_needs_no_container(tmp_path):
    """The common case must not make composition depend on pulling a simulator image."""
    from robovast.common.simulators import ContainerQuery
    from robovast_sim_robosito.backend import RobositoBackend, RobositoConfig

    world = tmp_path / "w.yaml"
    world.write_text("extends: rst_scenes:depot\nplugins: []\n")
    declared = RobositoBackend().input_files(RobositoConfig(config=str(world)), {})
    assert not isinstance(declared, ContainerQuery)
    assert declared == [str(world)]


def test_a_world_extending_a_campaign_file_asks_the_image(tmp_path):
    """That chain is what the backend cannot resolve without importing the simulator."""
    from robovast.common.simulators import ContainerQuery
    from robovast_sim_robosito.backend import RobositoBackend, RobositoConfig

    world = tmp_path / "w.yaml"
    world.write_text("extends: ./base.yaml\nplugins: []\n")
    declared = RobositoBackend().input_files(RobositoConfig(config=str(world)), {})
    assert isinstance(declared, ContainerQuery)
    assert declared.command[:3] == ["rst", "scenes", "inputs"]


def test_a_packaged_world_answers_without_a_container():
    from robovast_sim_robosito.backend import RobositoBackend, RobositoConfig

    assert RobositoBackend().input_files(RobositoConfig(config="rst_scenes:depot"), {}) == []


def test_the_query_reply_keeps_only_what_the_campaign_owns(tmp_path):
    """Files that arrived with the image must not be copied into the campaign as a second copy."""
    from robovast.common.config_generation import _run_input_files_query
    from robovast.common.simulators import ContainerQuery

    class _Runner:
        def run(self, _command, emit):
            emit('{"packaged": false, "inputs": ["%s/w.yaml", "/opt/pkg/mesh.stl"]}' % tmp_path)

        def close(self):
            pass

    import robovast.common.config_generation as cg
    original, cg._make_container_runner = cg._make_container_runner, lambda spec: _Runner()
    try:
        assert _run_input_files_query(ContainerQuery(None, []), str(tmp_path)) == ["w.yaml"]
    finally:
        cg._make_container_runner = original


def test_a_query_that_prints_no_json_fails_loudly(tmp_path):
    from robovast.common.config_generation import _run_input_files_query
    from robovast.common.simulators import ContainerQuery

    class _Runner:
        def run(self, _command, emit):
            emit("bash: rst: command not found")

        def close(self):
            pass

    import robovast.common.config_generation as cg
    original, cg._make_container_runner = cg._make_container_runner, lambda spec: _Runner()
    try:
        with pytest.raises(RuntimeError, match="printed no JSON"):
            _run_input_files_query(ContainerQuery(None, []), str(tmp_path))
    finally:
        cg._make_container_runner = original


# -- checking an override before any compute is spent ------------------------------------------

def _describing(payload):
    """A container runner that answers a describe query with *payload*."""
    class _Runner:
        def run(self, _command, emit):
            import json as _json
            emit(_json.dumps(payload))

        def close(self):
            pass
    return lambda spec: _Runner()


def _check(block, payload, tmp_path):
    import robovast.common.config_generation as cg

    execution = {"mode": "ros2",
                 "containers": {"simulation": {"backend": "robosito",
                                               "config": "w.yaml"}}}
    original, cg._make_container_runner = cg._make_container_runner, _describing(payload)
    try:
        cg._check_sim_override_targets(execution, [block], str(tmp_path))
    finally:
        cg._make_container_runner = original


def test_an_override_targeting_no_plugin_is_refused_before_the_image_pull(tmp_path):
    block = {"config": "w.yaml", "overrides": {"plugins": {"floorplna": {"size": 4.0}}}}
    with pytest.raises(ValueError, match="targets no plugin"):
        _check(block, {"plugins": [{"key": "floorplan", "paths": []}]}, tmp_path)


def test_the_error_names_what_the_world_does_have(tmp_path):
    block = {"config": "w.yaml", "overrides": {"plugins": {"nope": {}}}}
    with pytest.raises(ValueError, match="floorplan, lidar"):
        _check(block, {"plugins": [{"key": "lidar"}, {"key": "floorplan"}]}, tmp_path)


def test_a_real_plugin_passes(tmp_path):
    block = {"config": "w.yaml", "overrides": {"plugins": {"floorplan": {"size": 4.0}}}}
    _check(block, {"plugins": [{"key": "floorplan", "paths": []}]}, tmp_path)


def test_a_path_the_world_leaves_at_its_default_is_not_refused(tmp_path):
    """`paths` lists what exists; a plugin may accept a key its world never sets."""
    block = {"config": "w.yaml",
             "overrides": {"plugins": {"floorplan": {"never_set_in_this_world": 1}}}}
    _check(block, {"plugins": [{"key": "floorplan", "paths": ["plugins.floorplan.mesh"]}]},
           tmp_path)


def test_a_campaign_that_overrides_nothing_is_not_checked(tmp_path):
    """No container run for a campaign that only selects worlds."""
    import robovast.common.config_generation as cg

    def _refuse(_spec):
        raise AssertionError("should not have started a container")

    execution = {"mode": "ros2",
                 "containers": {"simulation": {"backend": "robosito", "config": "w.yaml"}}}
    original, cg._make_container_runner = cg._make_container_runner, _refuse
    try:
        cg._check_sim_override_targets(execution, [{"config": "w.yaml"}], str(tmp_path))
    finally:
        cg._make_container_runner = original
