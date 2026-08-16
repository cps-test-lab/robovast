# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The ``robovast.simulators`` backend API.

Driven by a **stub** backend rather than the real roqsim one, which is itself the
property under test: RoboVAST's own suite must never import a simulator. The stub's
config key is deliberately ``stage``, not ``world`` or ``config``, so anything that
hard-codes roqsim's vocabulary fails here.
"""

import pytest
from pydantic import BaseModel, ConfigDict

from robovast.common.containers import plan_containers
from robovast.common.execution import scenario_env
from robovast.common.simulators import (SHAPE_ROS, SHAPE_STEPPED, SimulatorBackend, apply_backend,
                                        shape_for)


class StageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stage: str
    fidelity: str = "low"


class StubBackend(SimulatorBackend):
    CONFIG_CLASS = StageConfig
    SUPPORTED_SHAPES = (SHAPE_STEPPED, SHAPE_ROS)

    def containers(self, cfg, execution):
        if shape_for(execution.get("mode")) == SHAPE_ROS:
            return {"simulation": {"image": "vendor/sim:1",
                                   "command": ["sim", "--stage", cfg.stage]}}
        return {"scenario": {"image": "combined/sim:1"}}

    def simulation_ref(self, cfg, execution):
        return "stub.adapter:StubSim"

    def env(self, cfg, execution):
        return {"STUB_STAGE": cfg.stage, "STUB_FIDELITY": cfg.fidelity}

    def produces_run_capture(self, cfg, execution):
        return True

    def input_files(self, cfg, execution):
        # A path travels with the campaign; a `pkg:name` ref lives in the image.
        return [] if ":" in cfg.stage else [cfg.stage]


class RosOnlyBackend(SimulatorBackend):
    """Like a simulator with no SimulationInterface at all -- Gazebo, Isaac."""
    SUPPORTED_SHAPES = (SHAPE_ROS,)

    def containers(self, cfg, execution):
        return {"simulation": {"image": "gz:harmonic", "command": ["gz", "sim", "-s"]}}


@pytest.fixture(autouse=True)
def _register(monkeypatch):
    """Resolve 'stub'/'rosonly' without installing an entry point."""
    import robovast.common.simulators as mod
    backends = {"stub": StubBackend, "rosonly": RosOnlyBackend}
    monkeypatch.setattr(mod, "resolve_backend",
                        lambda name, base_dir="": backends[name]())


def _execution(mode="base", **sim):
    return {"mode": mode, "runs": 1,
            "containers": {"simulation": dict(backend="stub", **sim)}}


# -- what a backend contributes ----------------------------------------------------

def test_the_stepped_shape_folds_the_simulator_into_the_scenario_container():
    ex = apply_backend(_execution("base", stage="cell.usd"))
    plan = plan_containers(ex)
    assert plan.names() == ["scenario"]
    assert plan.main.image == "combined/sim:1"
    # The name still resolves -- a caller never has to know which shape it is looking at.
    assert plan.by_name("simulation").name == "scenario"


def test_the_ros_shape_gives_the_simulator_its_own_container():
    ex = apply_backend(_execution("ros2", stage="cell.usd"))
    plan = plan_containers(ex)
    assert plan.names() == ["scenario", "simulation"]
    sim = plan.by_name("simulation")
    assert sim.image == "vendor/sim:1"
    assert sim.command == ["sim", "--stage", "cell.usd"]


def test_a_simulation_ref_is_only_for_the_stepped_shape():
    """In the ROS shape the simulator is a process, not a SimulationInterface -- which is
    why a simulator that has none fits the API unchanged."""
    assert apply_backend(_execution("base", stage="s")).get("simulation") == \
        "stub.adapter:StubSim"
    assert apply_backend(_execution("ros2", stage="s")).get("simulation") is None


def test_a_backend_serves_only_the_shapes_it_declares():
    ex = {"mode": "base", "containers": {"simulation": {"backend": "rosonly"}}}
    with pytest.raises(ValueError, match="does not support the stepped shape"):
        apply_backend(ex)


def test_a_ros_only_backend_needs_no_simulation_interface():
    ex = apply_backend({"mode": "ros2",
                        "containers": {"simulation": {"backend": "rosonly"}}})
    assert ex.get("simulation") is None
    assert plan_containers(ex).by_name("simulation").image == "gz:harmonic"


# -- the campaign always wins -------------------------------------------------------

def test_an_authored_image_beats_the_backend_default():
    ex = apply_backend({"mode": "ros2", "containers": {
        "simulation": {"backend": "stub", "stage": "s", "image": "mine:1"}}})
    assert plan_containers(ex).by_name("simulation").image == "mine:1"


def test_an_authored_env_value_beats_the_backend():
    """A backend supplies defaults it knows, not decisions it takes away.

    ``scenario_env`` carries only *derived* variables; a campaign's own ``execution.env``
    is emitted separately by each lane. So winning here means the backend's value is
    **withheld** -- emitting it too would leave two entries for one name, resolved by
    emission order, which is exactly the ambiguity this precedence exists to remove.
    """
    ex = apply_backend(_execution("base", stage="cell.usd"))
    ex["env"] = [{"STUB_FIDELITY": "high"}]
    env = scenario_env({"execution": ex})
    assert "STUB_FIDELITY" not in env        # authored: left to execution.env alone
    assert env["STUB_STAGE"] == "cell.usd"   # backend-supplied, untouched


def test_backend_env_reaches_scenario_env():
    ex = apply_backend(_execution("base", stage="cell.usd"))
    env = scenario_env({"execution": ex})
    assert env["STUB_STAGE"] == "cell.usd"
    assert env["SIMULATION"] == "stub.adapter:StubSim"


def test_extending_a_folded_simulation_container_still_builds():
    """The campaign's own plugins must reach the container the simulator runs in."""
    ex = apply_backend(_execution("base", stage="s", python_packages=["./mine"]))
    plan = plan_containers(ex)
    assert plan.main.builds
    assert plan.main.python_packages == ("./mine",)


# -- the backend owns its own vocabulary --------------------------------------------

def test_an_undeclared_key_is_rejected_naming_the_backend():
    with pytest.raises(ValueError, match="backend 'stub'"):
        apply_backend(_execution("base", stage="s", wrold="typo"))


def test_a_missing_required_key_is_rejected_naming_the_backend():
    with pytest.raises(ValueError, match="backend 'stub'"):
        apply_backend(_execution("base"))


def test_robovast_keys_are_not_offered_to_the_backend():
    """image/command/resources are RoboVAST's; a backend's CONFIG_CLASS forbids extras,
    so handing them over would reject every campaign that sets one."""
    ex = apply_backend(_execution("base", stage="s", image="mine:1",
                                  resources={"cpu": 2}))
    assert plan_containers(ex).main.image == "mine:1"


def test_no_backend_is_a_no_op():
    ex = {"mode": "base", "containers": {"scenario": {"image": "a"}}}
    assert apply_backend(ex) is ex


def test_shape_is_derived_from_mode_not_declared_twice():
    assert shape_for("ros2") == SHAPE_ROS
    assert shape_for("base") == SHAPE_STEPPED


# -- the build plan and the run plan must agree on which container builds ------------

def _campaign_config(execution: dict):
    """A stand-in for the validated project config extract_build_specs reads."""
    class _Block:
        def __init__(self, data):
            self._data = data

        def model_dump(self):
            return dict(self._data)

    class _Execution:
        def __init__(self, ex):
            self.mode = ex.get("mode")
            self.containers = {n: _Block(b) for n, b in ex["containers"].items()}

    class _Config:
        def __init__(self, ex):
            self.execution = _Execution(ex)

    return _Config(execution)


def test_stepped_build_spec_is_keyed_to_the_container_that_runs():
    """Packages under a folded ``simulation`` must build the ``scenario`` image.

    The build path and the run path plan containers independently. When they disagreed,
    a stepped campaign built an image tagged ``simulation`` while the container that
    actually started was ``scenario`` -- so it ran the unbuilt base, without the
    campaign's own code and without any error. Silence is the whole danger here, which
    is why this asserts the key rather than merely that a spec exists.
    """
    from robovast.service.image_build import extract_build_specs

    execution = _execution("base", stage="s", python_packages=["./mine"])
    specs = extract_build_specs(_campaign_config(execution))

    assert list(specs) == ["scenario"], \
        f"stepped build must target the scenario container, got {list(specs)}"
    assert specs["scenario"].python_packages == ["./mine"]
    assert specs["scenario"].base_image == "combined/sim:1"


def test_ros_build_spec_stays_on_the_simulation_container():
    """The ROS shape does NOT fold, so packages there build the simulation image."""
    from robovast.service.image_build import extract_build_specs

    execution = _execution("ros2", stage="s", python_packages=["./mine"])
    specs = extract_build_specs(_campaign_config(execution))

    assert list(specs) == ["simulation"]
    assert specs["simulation"].base_image == "vendor/sim:1"


# -- the backend's env must reach the container the backend describes ----------------

def test_backend_env_reaches_the_simulation_sidecar():
    """In the ROS shape the simulator is a SIDECAR, so scenario_env cannot serve it.

    scenario_env emits the backend's contribution into the main container, which is only
    correct when the simulator IS the main container. In the ROS shape that sent
    roqsim's ROQSIM_RECORD / ROQSIM_CAPTURE_EXPORT_DIR to the scenario container and
    nowhere else, so the run produced no capture at all -- while produces_run_capture()
    still said True and validation accepted a scene3d panel with nothing to replay.
    """
    from robovast.common.execution import sidecar_backend_env

    ex = apply_backend(_execution("ros2", stage="cell.usd"))
    assert sidecar_backend_env(ex, "simulation") == {"STUB_STAGE": "cell.usd",
                                                     "STUB_FIDELITY": "low"}


def test_backend_env_does_not_leak_into_other_sidecars():
    """A vanilla SUT gets none of it: a backend describes its own simulator."""
    from robovast.common.execution import sidecar_backend_env

    ex = apply_backend(_execution("ros2", stage="s"))
    assert sidecar_backend_env(ex, "sut") == {}
    assert sidecar_backend_env(ex, "scenario") == {}


def test_a_campaigns_own_env_still_beats_the_backend_on_a_sidecar():
    """Same precedence rule as the main container: a backend supplies defaults."""
    from robovast.common.execution import sidecar_backend_env

    ex = apply_backend(_execution("ros2", stage="s"))
    ex["env"] = [{"STUB_FIDELITY": "high"}]
    assert sidecar_backend_env(ex, "simulation")["STUB_STAGE"] == "s"
    assert "STUB_FIDELITY" not in sidecar_backend_env(ex, "simulation")


# -- what the backend says has to travel --------------------------------------------

def test_a_backend_declares_the_files_its_simulator_needs(tmp_path):
    """So a campaign names its world once, under `config:`, and not again in run_files.

    They become run_files rather than _input_files, because the file must be MOUNTED at
    /config/<path> for the simulator to open it -- _input_files are only archived. It also
    has to be hashed into the configuration identity: a changed world is a changed
    experiment.
    """
    from robovast.common.config_generation import _backend_run_files

    params = {"execution": _execution("ros2", stage="worlds/cell.usd")}
    assert _backend_run_files(str(tmp_path), params) == ["worlds/cell.usd"]


def test_a_backend_that_declares_nothing_adds_nothing(tmp_path):
    from robovast.common.config_generation import _backend_run_files

    # RosOnlyBackend implements no input_files at all -> the base class default.
    params = {"execution": {"mode": "ros2",
                            "containers": {"simulation": {"backend": "rosonly"}}}}
    assert _backend_run_files(str(tmp_path), params) == []


def test_no_backend_means_no_extra_run_files(tmp_path):
    from robovast.common.config_generation import _backend_run_files

    params = {"execution": {"containers": {"scenario": {"image": "img:1"}}}}
    assert _backend_run_files(str(tmp_path), params) == []
