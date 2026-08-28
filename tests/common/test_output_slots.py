# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""Output slots: how a variation with several outputs says where each one lands.

The single-output form (``scenario: goal_pose``) is covered by test_sim_channel.py. This
file is about the case that form cannot express -- a plugin whose outputs are several, whose
*names* the campaign chooses, and which may straddle the two channels.
"""

import json
import logging

import pytest
from pydantic import ValidationError

from robovast.common.variation.base_variation import (SCENARIO_CHANNEL, SIM_CHANNEL,
                                                      DestinationConfig, Variation)


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
    # `__new__` takes the class as its first argument, which is what is passed here. pylint
    # only reads it as unfilled because pydantic is not installed in super-linter's container.
    # pylint: disable-next=no-value-for-parameter
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

def test_obstacle_extents_are_stated_once_and_rendered_per_spawner():
    """One statement of the geometry; the spawner's argument names stay in the campaign.

    ``xacro_arguments`` is a template over ``size``, substituted positionally, so this
    generic variation never learns that some model file calls its parameters
    width/length/height.
    """
    from robovast_nav.variation.obstacle_variation import ObstacleConfig

    oc = ObstacleConfig(amount=1, max_distance=0.1, model="box.sdf.xacro", size=[0.5, 0.8, 1.0],
                        xacro_arguments="width:={size[0]}, length:={size[1]}, height:={size[2]}")
    assert oc.rendered_xacro_arguments() == "width:=0.5, length:=0.8, height:=1.0"

    # A model whose parameters are spelled differently is served by the same mechanism.
    radial = ObstacleConfig(amount=1, max_distance=0.1, model="cylinder.sdf.xacro",
                            size=[0.3, 0.3, 1.0], xacro_arguments="radius:={size[0]}")
    assert radial.rendered_xacro_arguments() == "radius:=0.3"

    # A literal string with no placeholders is passed through untouched.
    literal = ObstacleConfig(amount=1, max_distance=0.1, model="m", xacro_arguments="width:=0.5")
    assert literal.rendered_xacro_arguments() == "width:=0.5"


def test_obstacle_size_must_be_declared_rather_than_guessed():
    """A simulator that COMPILES the placement is refused an obstacle with no extents.

    Inferring it by parsing ``xacro_arguments`` for width/length/height yields no size at
    all for anything unparseable -- so the placement plugin falls back to its own default
    and compiles a differently-sized obstacle than the other simulator spawns, silently.
    Refusing at composition is what makes that a fixable error instead of a wrong number in
    a result set.
    """
    from robovast_nav.variation.obstacle_variation import ObstacleVariationConfig

    def _cfg(**oc):
        return ObstacleVariationConfig(
            scenario={"objects": "static_objects"},
            sim={"instances": "plugins.obstacles.instances"},
            obstacle_configs=[{"amount": 1, "max_distance": 0.1, "model": "m", **oc}],
            seed=1, robot_diameter=0.35)

    with pytest.raises(ValueError, match="'size' is required when the 'instances' slot is bound"):
        _cfg(xacro_arguments="radius:=0.3")

    # Bound only to a run-time spawner, no size is needed -- that campaign compiles nothing.
    ObstacleVariationConfig(scenario={"objects": "static_objects"},
                            obstacle_configs=[{"amount": 1, "max_distance": 0.1, "model": "m",
                                               "xacro_arguments": "radius:=0.3"}],
                            seed=1, robot_diameter=0.35)

    # A template that cannot resolve is refused rather than reaching a spawner as a literal.
    with pytest.raises(ValueError, match=r"references \{size\[\.\.\.\]\} but no 'size'"):
        ObstacleVariationConfig(scenario={"objects": "static_objects"},
                                obstacle_configs=[{"amount": 1, "max_distance": 0.1, "model": "m",
                                                   "xacro_arguments": "width:={size[0]}"}],
                                seed=1, robot_diameter=0.35)


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

@pytest.mark.requires_simulator
def test_a_world_with_no_campaign_parent_needs_no_container(tmp_path):
    """The common case must not make composition depend on pulling a simulator image."""
    from robovast.common.simulators import ContainerQuery
    from robovast_sim_roqsim.backend import RoqsimBackend, RoqsimConfig

    world = tmp_path / "w.yaml"
    world.write_text("extends: roqsim_scenes:depot\nplugins: []\n")
    declared = RoqsimBackend().input_files(
        RoqsimConfig(config=str(world)), {}, str(tmp_path))
    assert not isinstance(declared, ContainerQuery)
    assert declared == [str(world)]


@pytest.mark.requires_simulator
def test_a_world_extending_a_campaign_file_asks_the_image(tmp_path):
    """That chain is what the backend cannot resolve without importing the simulator."""
    from robovast.common.simulators import ContainerQuery
    from robovast_sim_roqsim.backend import RoqsimBackend, RoqsimConfig

    world = tmp_path / "w.yaml"
    world.write_text("extends: ./base.yaml\nplugins: []\n")
    declared = RoqsimBackend().input_files(
        RoqsimConfig(config=str(world)), {}, str(tmp_path))
    assert isinstance(declared, ContainerQuery)
    assert declared.command[:3] == ["roqsim", "scenes", "inputs"]


@pytest.mark.requires_simulator
def test_a_packaged_world_answers_without_a_container():
    from robovast_sim_roqsim.backend import RoqsimBackend, RoqsimConfig

    assert RoqsimBackend().input_files(
        RoqsimConfig(config="roqsim_scenes:depot"), {}, "") == []


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
    original, cg._make_container_runner = cg._make_container_runner, lambda spec, **_: _Runner()
    try:
        assert _run_input_files_query(ContainerQuery(None, []), str(tmp_path)) == ["w.yaml"]
    finally:
        cg._make_container_runner = original


@pytest.mark.requires_simulator
def test_the_extends_question_is_answered_from_the_vast_dir_not_the_cwd(tmp_path, monkeypatch):
    """The authored path is relative to the ``.vast``; the caller's cwd is not a party to it.

    Opening it as given made the same campaign answer differently depending on where the
    caller stood -- asked from its own directory, skipped from anywhere else -- and only the
    asking branch staged the parent.
    """
    from robovast.common.simulators import ContainerQuery
    from robovast_sim_roqsim.backend import RoqsimBackend, RoqsimConfig

    (tmp_path / "world").mkdir()
    (tmp_path / "world" / "w.yaml").write_text("extends: base.yaml\nplugins: []\n")
    cfg = RoqsimConfig(config="world/w.yaml")

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    declared = RoqsimBackend().input_files(cfg, {}, str(tmp_path))
    assert isinstance(declared, ContainerQuery)
    # And the container is told where the file will be, not where it is on this host.
    assert declared.command == ["roqsim", "scenes", "inputs", "/config/world/w.yaml"]

    monkeypatch.chdir(tmp_path)
    assert isinstance(RoqsimBackend().input_files(cfg, {}, str(tmp_path)), ContainerQuery)


def test_the_query_mounts_the_campaign_where_its_command_looks(tmp_path):
    """The command names ``/config/...``; without the mount the container has no such path."""
    from robovast.common.config_generation import _run_input_files_query
    from robovast.common.simulators import ContainerQuery

    exposed = {}

    class _Runner:
        def expose(self, host_path, container_path):
            exposed[container_path] = host_path

        def run(self, _command, emit):
            emit('{"packaged": false, "inputs": ["%s/w.yaml"]}' % tmp_path)

        def close(self):
            pass

    import robovast.common.config_generation as cg
    original, cg._make_container_runner = cg._make_container_runner, lambda spec, **_: _Runner()
    try:
        assert _run_input_files_query(ContainerQuery(None, []), str(tmp_path)) == ["w.yaml"]
    finally:
        cg._make_container_runner = original
    assert exposed == {"/config": str(tmp_path)}


def test_the_local_runner_gets_a_ref_docker_can_pull(monkeypatch):
    """``docker`` cannot pull ``family:<member>`` -- that name is RoboVAST's, not a registry's."""
    from robovast.common.config_generation import _make_container_runner
    from robovast.common.execution import MEMBER_ROQSIM, family_image_ref
    from robovast.common.variation.container_runner import ContainerSpec

    monkeypatch.setenv("ROBOVAST_PROJECT", "registry.example/proj")
    runner = _make_container_runner(ContainerSpec(image=family_image_ref(MEMBER_ROQSIM)))
    try:
        assert runner._spec.image == f"registry.example/proj/{MEMBER_ROQSIM}:latest"
    finally:
        runner.close()

    runner = _make_container_runner(ContainerSpec(image=family_image_ref(MEMBER_ROQSIM)),
                                    image_project="dev.example/mine",
                                    image_project_tag="pinned")
    try:
        assert runner._spec.image == f"dev.example/mine/{MEMBER_ROQSIM}:pinned"
    finally:
        runner.close()


def test_the_factory_keeps_the_family_ref_because_it_names_a_container(monkeypatch):
    """The cluster factory execs into an aux Pod container named after the spec's image.

    Resolving it here would name a container that Pod does not have, so the resolution is
    the local fallback's alone.
    """
    from robovast.common.config_generation import (_container_runner_factory,
                                                   _make_container_runner,
                                                   set_container_runner_factory)
    from robovast.common.execution import MEMBER_ROQSIM, family_image_ref
    from robovast.common.variation.container_runner import ContainerSpec

    seen = []
    token = set_container_runner_factory(lambda spec: seen.append(spec.image) or "runner")
    try:
        assert _make_container_runner(
            ContainerSpec(image=family_image_ref(MEMBER_ROQSIM))) == "runner"
    finally:
        _container_runner_factory.reset(token)
    assert seen == [family_image_ref(MEMBER_ROQSIM)]


def test_a_query_that_prints_no_json_fails_loudly(tmp_path):
    from robovast.common.config_generation import _run_input_files_query
    from robovast.common.simulators import ContainerQuery

    class _Runner:
        def run(self, _command, emit):
            emit("bash: roqsim: command not found")

        def close(self):
            pass

    import robovast.common.config_generation as cg
    original, cg._make_container_runner = cg._make_container_runner, lambda spec, **_: _Runner()
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
            emit(json.dumps(payload))

        def close(self):
            pass
    return lambda spec, **_: _Runner()


def _check(block, payload, tmp_path, params=None, scenario_parameters=None):
    import robovast.common.config_generation as cg

    execution = {"mode": "ros2",
                 "containers": {"simulation": {"backend": "roqsim",
                                               "config": "w.yaml"}}}
    configs = [{"name": "c", "sim": block, "config": params or {}}]
    original, cg._make_container_runner = cg._make_container_runner, _describing(payload)
    try:
        cg._check_sim_against_world(execution, configs, str(tmp_path), scenario_parameters)
    finally:
        cg._make_container_runner = original


@pytest.mark.requires_simulator
def test_a_simulator_too_old_for_the_overrides_still_gets_the_plugin_half_checked(tmp_path):
    """An old image must be the second-best case, not the least-checked one.

    Its describe rejects ``--override`` outright, so the answer that needs the overrides is lost.
    The plugin-key half never needed them, and dropping it too would mean a misspelled plugin
    reached the container precisely where the image was oldest.
    """
    import robovast.common.config_generation as cg

    asked = []

    class _Runner:
        def run(self, command, emit):
            asked.append(command)
            if "--override" in command:
                emit("roqsim scenes describe: error: unrecognized arguments: --override")
                raise RuntimeError("exit 2")
            emit(json.dumps({"plugins": [{"address": "boxes", "ref": "boxes"}], "addresses": ["boxes"],
                            "entities": None}))

        def close(self):
            pass

    execution = {"mode": "ros2",
                 "containers": {"simulation": {"backend": "roqsim", "config": "w.yaml"}}}
    block = {"config": "w.yaml", "overrides": {"plugins": {"boxesTYPO": {"instances": []}}}}
    configs = [{"name": "c", "sim": block, "config": {}}]
    original, cg._make_container_runner = cg._make_container_runner, lambda spec, **_: _Runner()
    try:
        with pytest.raises(ValueError, match="targets no component in this world"):
            cg._check_sim_against_world(execution, configs, str(tmp_path), None)
    finally:
        cg._make_container_runner = original
    # Asked with the overrides first, then again without them -- the second container is the
    # price of the degraded case only.
    assert ["--override" in c for c in asked] == [True, False]


@pytest.mark.requires_simulator
def test_the_world_is_described_with_the_campaign_overrides_a_run_would_get():
    """Otherwise the answer is about the base world, and the entity check reads it as truth.

    A campaign whose obstacles come from its own ``sim`` overrides compiles those entities only
    once the overrides are applied. Described without them, the world has none -- and the check
    refused campaigns that run correctly.
    """
    from robovast.common.simulators import SIM_QUERY_OVERRIDES_MOUNT
    from robovast_sim_roqsim.backend import RoqsimBackend, RoqsimConfig

    cfg = RoqsimConfig(config="w.yaml",
                       overrides={"plugins": {"boxes": {"instances": [{"pos": [1, 1]}]}}})
    query = RoqsimBackend().describe_query(cfg, {"mode": "ros2"}, entities=True)
    assert query.command[-2:] == ["--override", SIM_QUERY_OVERRIDES_MOUNT]
    # And the document travels ON the query, so the path its command names is the path the
    # caller mounts -- one string, read twice, rather than two places agreeing by luck.
    assert query.documents == {SIM_QUERY_OVERRIDES_MOUNT: cfg.overrides}

    plain = RoqsimBackend().describe_query(RoqsimConfig(config="w.yaml"), {"mode": "ros2"})
    assert "--override" not in plain.command and plain.documents == {}


def test_a_query_document_is_written_where_its_command_looks(tmp_path):
    import yaml as _yaml

    from robovast.common.config_generation import _stage_query_documents
    from robovast.common.simulators import ContainerQuery

    class _Runner:
        workspace = str(tmp_path)

    mounted = {}
    query = ContainerQuery(None, [], {"/aux/sim.overrides.yaml": {"plugins": {"boxes": {}}}})
    _stage_query_documents(_Runner(), query, lambda host, at: mounted.update({at: host}))

    assert list(mounted) == ["/aux/sim.overrides.yaml"]
    with open(mounted["/aux/sim.overrides.yaml"], encoding="utf-8") as handle:
        assert _yaml.safe_load(handle) == {"plugins": {"boxes": {}}}


@pytest.mark.requires_simulator
def test_an_override_targeting_no_plugin_is_refused_before_the_image_pull(tmp_path):
    block = {"config": "w.yaml", "overrides": {"plugins": {"floorplna": {"size": 4.0}}}}
    with pytest.raises(ValueError, match="targets no component"):
        _check(block, {"plugins": [{"address": "floorplan", "paths": []}],
                "addresses": ["floorplan"]}, tmp_path)


@pytest.mark.requires_simulator
def test_the_error_names_what_the_world_does_have(tmp_path):
    block = {"config": "w.yaml", "overrides": {"plugins": {"nope": {}}}}
    with pytest.raises(ValueError, match="floorplan, lidar"):
        _check(block, {"plugins": [{"address": "lidar"}, {"address": "floorplan"}],
                "addresses": ["floorplan", "lidar"]}, tmp_path)


@pytest.mark.requires_simulator
def test_a_real_plugin_passes(tmp_path):
    block = {"config": "w.yaml", "overrides": {"plugins": {"floorplan": {"size": 4.0}}}}
    _check(block, {"plugins": [{"address": "floorplan", "paths": []}],
                "addresses": ["floorplan"]}, tmp_path)


@pytest.mark.requires_simulator
def test_a_path_the_world_leaves_at_its_default_is_not_refused(tmp_path):
    """`paths` lists what exists; a plugin may accept a key its world never sets."""
    block = {"config": "w.yaml",
             "overrides": {"plugins": {"floorplan": {"never_set_in_this_world": 1}}}}
    _check(block, {"plugins": [{"address": "floorplan", "paths": ["components.floorplan.mesh"]}],
                  "addresses": ["floorplan"]},
           tmp_path)


@pytest.mark.requires_simulator
def test_a_campaign_with_nothing_to_check_starts_no_container(tmp_path):
    """No overrides and no entity names means nothing a description could settle."""
    import robovast.common.config_generation as cg

    def _refuse(_spec):
        raise AssertionError("should not have started a container")

    execution = {"mode": "ros2",
                 "containers": {"simulation": {"backend": "roqsim", "config": "w.yaml"}}}
    configs = [{"name": "c", "sim": {"config": "w.yaml"}, "config": {}}]
    original, cg._make_container_runner = cg._make_container_runner, _refuse
    try:
        cg._check_sim_against_world(execution, configs, str(tmp_path))
    finally:
        cg._make_container_runner = original


# -- entities the trial drives must be entities the world compiled ---------------------------

_SPAWN_PARAMS = [{"name": "static_objects", "type": "listofspawn_entity"}]


@pytest.mark.requires_simulator
def test_an_entity_the_world_never_compiled_is_refused(tmp_path):
    """Nothing can create it at run time, so this is a run that fails on a service call."""
    with pytest.raises(ValueError, match="does not compile: obstacle_9"):
        _check({"config": "w.yaml"},
               {"plugins": [], "addresses": [], "entities": ["obstacle_0", "obstacle_1"]},
               tmp_path,
               params={"static_objects": [{"entity_name": "obstacle_9"}]},
               scenario_parameters=_SPAWN_PARAMS)


@pytest.mark.requires_simulator
def test_entities_the_world_compiled_pass(tmp_path):
    _check({"config": "w.yaml"},
           {"plugins": [], "addresses": [], "entities": ["obstacle_0", "obstacle_1"]},
           tmp_path,
           params={"static_objects": [{"entity_name": "obstacle_0"},
                                      {"entity_name": "obstacle_1"}]},
           scenario_parameters=_SPAWN_PARAMS)


@pytest.mark.requires_simulator
def test_a_partial_answer_still_checks_the_half_it_has(tmp_path, caplog):
    """A build that failed costs the entity check, not the plugin-key check -- and says which."""
    with caplog.at_level(logging.WARNING):
        _check({"config": "w.yaml", "overrides": {"plugins": {"floorplan": {"size": 4.0}}}},
               {"plugins": [{"address": "floorplan", "paths": []}], "addresses": ["floorplan"],
                "entities": None,
                "errors": {"build": "unresolved plugins: ros2_bridge"}},
               tmp_path,
               params={"static_objects": [{"entity_name": "obstacle_0"}]},
               scenario_parameters=_SPAWN_PARAMS)
    assert "entities this scenario names were not pre-checked" in caplog.text
    assert "obstacle_0" in caplog.text
    assert "ros2_bridge" in caplog.text, "the reason has to be in the line, not just the fact"


@pytest.mark.requires_simulator
def test_a_partial_answer_does_not_soften_the_check_it_can_still_make(tmp_path):
    """The plugin keys came back, so a misspelt one is refused exactly as it always was."""
    with pytest.raises(ValueError, match="targets no component"):
        _check({"config": "w.yaml", "overrides": {"plugins": {"floorplna": {"size": 4.0}}}},
               {"plugins": [{"address": "floorplan", "paths": []}], "addresses": ["floorplan"],
                "entities": None,
                "errors": {"build": "unresolved plugins: ros2_bridge"}},
               tmp_path)


@pytest.mark.requires_simulator
def test_only_entity_typed_parameters_are_read(tmp_path):
    """A pose parameter is not an entity reference, whatever its fields are called."""
    _check({"config": "w.yaml"},
           {"plugins": [], "addresses": [], "entities": []},
           tmp_path,
           params={"spawn_trigger_point": {"entity_name": "not_an_entity"}},
           scenario_parameters=[{"name": "spawn_trigger_point", "type": "position_3d"}])


def test_both_channels_call_an_obstacle_the_same_thing():
    """Count and geometry agreeing is not enough if the names do not.

    A placement plugin left to name its own instances calls them `boxes_0`, while the trial
    knows `obstacle_0` -- so a scenario driving one by name would address nothing, in a
    campaign where every other cross-check passes.
    """
    from dataclasses import dataclass, field

    from robovast_nav.data_model import Orientation, Pose, Position
    from robovast_nav.variation.obstacle_variation import _instances_for_sim

    @dataclass
    class _Obj:
        entity_name: str = "obstacle_7"
        model: str = "box.sdf.xacro"
        xacro_arguments: str = "width:=0.5, length:=0.5, height:=1.0"
        spawn_pose: Pose = field(default_factory=lambda: Pose(
            position=Position(x=1.0, y=2.0), orientation=Orientation(yaw=0.0)))

    # The geometry travels beside the spawn objects, in placement order, because what a
    # spawner is handed and what a compiler needs are different questions about one placement.
    instances = _instances_for_sim([_Obj()], [("box", [0.5, 0.5, 1.0])])
    assert instances[0]["name"] == "obstacle_7"
    assert instances[0]["size"] == [0.5, 0.5, 1.0]
    assert instances[0]["pos"] == [1.0, 2.0]
    # 'box' is the placement plugin's own default, so it is not restated per instance.
    assert "shape" not in instances[0]
