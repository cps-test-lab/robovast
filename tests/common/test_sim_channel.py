# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""The simulation channel: a world belongs to a CONFIGURATION.

The campaign's ``simulation`` block is only the default; each configuration resolves its own
block, and that block is what reaches the run. These tests pin the three properties that
make that safe -- the destination of a factor is unambiguous, every world a configuration
names travels, and a job never mixes two of them.
"""

import pytest
from pydantic import BaseModel, ConfigDict

from robovast.common import simulators as S
from robovast.common.variation.base_variation import DestinationConfig, Variation
from robovast.common.variation.parameter_variation import ParameterVariationList
from robovast.execution.packer import FixedK, OnePerJob, WorkItem


class _StubConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: str
    overrides: dict | None = None


class _StubBackend(S.SimulatorBackend):
    CONFIG_CLASS = _StubConfig
    DOTTED_ROOT = "overrides"

    def containers(self, cfg, execution):
        command = ["sim", cfg.config]
        if cfg.overrides:
            command += ["--override", S.SIM_OVERRIDES_MOUNT]
        return {S.SIMULATION_CONTAINER: {"command": command}}

    def env(self, cfg, execution):
        return {"STUB_WORLD": cfg.config}

    def input_files(self, cfg, execution):
        return [] if ":" in cfg.config else [cfg.config]

    def sim_document(self, cfg, execution):
        return cfg.overrides or None


@pytest.fixture
def backend(monkeypatch):
    stub = _StubBackend()
    monkeypatch.setattr(S, "resolve_backend", lambda name, base_dir="": stub)
    return stub


@pytest.fixture
def execution():
    return {"mode": "ros2",
            "containers": {"simulation": {"backend": "stub", "image": "img",
                                          "config": "worlds/depot.yaml"}}}


# -- the destination of a factor is readable off the line it is written on ------------


def test_a_bare_backend_key_is_that_key_and_anything_else_is_an_override(backend):
    assert S.resolve_sim_path(backend, "config") == ("config",)
    assert S.resolve_sim_path(backend, "plugins.floorplan.size") == (
        "overrides", "plugins", "floorplan", "size")
    # The explicit spelling stays valid, and is how a world key colliding with a backend
    # key would be reached.
    assert S.resolve_sim_path(backend, "overrides.plugins.a") == ("overrides", "plugins", "a")


def test_a_backend_with_no_dotted_root_refuses_an_unknown_key_naming_the_real_ones(backend):
    class NoRoot(_StubBackend):
        DOTTED_ROOT = None

    with pytest.raises(ValueError) as err:
        S.resolve_sim_path(NoRoot(), "plugins.floorplan.size", "noroot")
    assert "plugins.floorplan.size" in str(err.value)
    assert "config" in str(err.value) and "overrides" in str(err.value)


def test_the_retired_name_key_is_refused_naming_both_channels():
    with pytest.raises(Exception) as err:
        ParameterVariationList("", {"name": "goal_pose", "values": [1.0]}, {},
                               lambda m: None, "s.osc", "/tmp")
    assert "scenario:" in str(err.value) and "sim:" in str(err.value)


@pytest.mark.parametrize("params", [
    {"values": [1.0]},                              # neither
    {"scenario": "a", "sim": "b", "values": [1.0]},  # both
])
def test_exactly_one_destination_is_required(params):
    with pytest.raises(Exception):
        ParameterVariationList("", params, {}, lambda m: None, "s.osc", "/tmp")


def test_a_variation_writes_the_channel_its_key_names():
    def run(params):
        v = ParameterVariationList("", params, {}, lambda m: None, "s.osc", "/tmp")
        return v.variation([{"name": "c", "config": {}}])

    scenario_side = run({"scenario": "goal_pose", "values": [1.0, 2.0]})
    assert [c["config"] for c in scenario_side] == [{"goal_pose": 1.0}, {"goal_pose": 2.0}]
    assert not any(c.get("sim") for c in scenario_side)

    sim_side = run({"sim": "plugins.floorplan.size", "values": [3.0]})
    assert sim_side[0]["sim"] == {"plugins.floorplan.size": 3.0}
    assert sim_side[0]["config"] == {}


def test_both_channels_are_written_by_one_update_config_call():
    """The coupling that matters: a variation that adds obstacles must also compile them.

    MuJoCo does not recompile mid-run, so poses on the scenario side and existence on the
    sim side have to be produced together or they can drift apart.
    """
    v = Variation.__new__(Variation)
    v._config_child_indices = {}
    out = Variation.update_config(
        v, {"name": "c", "config": {}},
        {"obstacles": [{"x": 1}]},
        sim_values={"plugins.boxes.instances": [{"pos": [1, 2]}]})
    assert out["config"] == {"obstacles": [{"x": 1}]}
    assert out["sim"] == {"plugins.boxes.instances": [{"pos": [1, 2]}]}


# -- the campaign block is a default a configuration overlays --------------------------


def test_a_configuration_overlays_the_campaign_default(backend, execution):
    merged = S.merge_sim_block(execution, {"plugins.floorplan.size": 4.2})
    assert merged == {"config": "worlds/depot.yaml",
                      "overrides": {"plugins": {"floorplan": {"size": 4.2}}}}


def test_a_configuration_may_replace_the_world_outright(backend, execution):
    merged = S.merge_sim_block(execution, {"config": "worlds/warehouse.yaml"})
    assert merged["config"] == "worlds/warehouse.yaml"


def test_a_campaign_that_varies_nothing_resolves_to_what_it_declared(backend, execution):
    assert S.merge_sim_block(execution) == {"config": "worlds/depot.yaml"}


def test_a_generated_file_is_addressed_where_the_container_will_find_it(backend, execution):
    """A per-config artifact is mounted under its own prefix, and absolutely.

    Absolute because the simulator is a separate process with an unrelated working
    directory -- unlike a scenario, which resolves file parameters against its own.
    """
    merged = S.merge_sim_block(
        execution, {"plugins.floorplan.mesh": "3d-mesh/room.stl"},
        deploy_paths={"3d-mesh/room.stl"}, config_name="rooms-1")
    assert (merged["overrides"]["plugins"]["floorplan"]["mesh"]
            == "/config/rooms-1/3d-mesh/room.stl")


def test_a_value_that_is_not_a_staged_file_is_left_alone(backend, execution):
    merged = S.merge_sim_block(
        execution, {"plugins.floorplan.mesh": "roqsim_scenes:depot"},
        deploy_paths={"3d-mesh/room.stl"}, config_name="rooms-1")
    assert merged["overrides"]["plugins"]["floorplan"]["mesh"] == "roqsim_scenes:depot"


def test_an_unrecognised_key_becomes_an_override_path_when_a_root_exists(backend, execution):
    """Deliberate, and the limit of what this layer can check.

    With a dotted root, anything that is not a backend key is a path INTO the world -- which
    is what makes ``sim: plugins.floorplan.size`` work. Whether that path exists in this
    particular world is the world's schema to answer, and RoboVAST cannot read a world
    without the simulator; today a typo there is refused by ``apply_overrides`` in the
    container. What this layer does catch is a misspelled *backend* key, below.
    """
    merged = S.merge_sim_block(execution, {"plugins.floorplan.frixion": 1})
    assert merged["overrides"] == {"plugins": {"floorplan": {"frixion": 1}}}


def test_a_backend_without_a_root_refuses_the_same_key(monkeypatch, execution):
    class Strict(_StubBackend):
        DOTTED_ROOT = None

    monkeypatch.setattr(S, "resolve_backend", lambda name, base_dir="": Strict())
    with pytest.raises(ValueError, match="not a key of simulator backend"):
        S.merge_sim_block(execution, {"nonsense": 1})


# -- what reaches the run --------------------------------------------------------------


def test_the_world_is_argv_and_the_overrides_are_a_document(backend, execution):
    block = S.merge_sim_block(execution, {"plugins.floorplan.size": 4.2})
    overlay = S.sim_job_overlay(execution, block)
    assert overlay["command"] == ["sim", "worlds/depot.yaml",
                                  "--override", S.SIM_OVERRIDES_MOUNT]
    assert overlay["document"] == {"plugins": {"floorplan": {"size": 4.2}}}
    assert overlay["env"] == {"STUB_WORLD": "worlds/depot.yaml"}


def test_no_overrides_means_no_document_and_no_flag(backend, execution):
    overlay = S.sim_job_overlay(execution, S.merge_sim_block(execution))
    assert overlay["document"] is None
    assert "--override" not in overlay["command"]


def test_every_distinct_world_travels(backend, execution):
    """The union, not the campaign default: a world that is not staged cannot be opened."""
    blocks = [S.merge_sim_block(execution, {"config": w})
              for w in ("worlds/a.yaml", "worlds/b.yaml", "roqsim_scenes:depot")]
    staged = {f for b in blocks for f in S.sim_input_files(execution, b)}
    assert staged == {"worlds/a.yaml", "worlds/b.yaml"}  # the package ref needs nothing


# -- a job runs one compiled model -----------------------------------------------------


def _item(config_name, sim, run_number=0):
    return WorkItem(config={"name": config_name, "sim": sim}, run_number=run_number)


def test_work_items_sharing_a_world_share_a_key():
    a = _item("c1", {"config": "w.yaml"})
    b = _item("c2", {"config": "w.yaml"})
    assert a.sim_key == b.sim_key


def test_a_different_world_is_a_different_key():
    assert _item("c1", {"config": "a.yaml"}).sim_key != _item("c2", {"config": "b.yaml"}).sim_key


def test_a_different_override_is_a_different_key():
    """Overrides change the compiled model as much as the world file does."""
    a = _item("c1", {"config": "w.yaml", "overrides": {"plugins": {"f": {"size": 3}}}})
    b = _item("c2", {"config": "w.yaml", "overrides": {"plugins": {"f": {"size": 4}}}})
    assert a.sim_key != b.sim_key


def test_key_does_not_depend_on_mapping_order():
    a = _item("c1", {"config": "w.yaml", "overrides": {"a": 1, "b": 2}})
    b = _item("c2", {"overrides": {"b": 2, "a": 1}, "config": "w.yaml"})
    assert a.sim_key == b.sim_key


def test_packing_never_mixes_two_worlds():
    items = [_item("c1", {"config": "a.yaml"}, 0), _item("c1", {"config": "a.yaml"}, 1),
             _item("c2", {"config": "b.yaml"}, 0), _item("c2", {"config": "b.yaml"}, 1)]
    jobs = FixedK(4).pack(items)
    assert len(jobs) == 2
    for job in jobs:
        assert len({it.sim_key for it in job.items}) == 1


def test_one_world_packs_exactly_as_it_always_did():
    """Every campaign before this had one world; none of them may see a different shape."""
    items = [_item("c1", {"config": "a.yaml"}, r) for r in range(5)]
    assert [len(j) for j in FixedK(2).pack(items)] == [2, 2, 1]


def test_grouping_preserves_first_seen_order():
    """`build_jobs` is called from several places and its answers must match across them."""
    items = [_item("c1", {"config": "a.yaml"}), _item("c2", {"config": "b.yaml"}),
             _item("c3", {"config": "a.yaml"})]
    jobs = FixedK(2).pack(items)
    assert [it.config_name for j in jobs for it in j.items] == ["c1", "c3", "c2"]
    assert [j.index for j in jobs] == list(range(len(jobs)))


def test_one_per_job_is_unaffected_by_worlds():
    items = [_item("c1", {"config": "a.yaml"}), _item("c2", {"config": "b.yaml"})]
    assert [len(j) for j in OnePerJob().pack(items)] == [1, 1]


def test_a_campaign_with_no_simulator_still_packs():
    items = [WorkItem(config={"name": "c1"}, run_number=r) for r in range(3)]
    assert len(FixedK(3).pack(items)) == 1
