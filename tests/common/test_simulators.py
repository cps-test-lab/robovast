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
                                        shape_for, simulator_image)


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

    def env(self, cfg, execution, recording):
        env = {"STUB_STAGE": cfg.stage, "STUB_FIDELITY": cfg.fidelity}
        if recording is not None and recording.roqsim is not None:
            # Any backend may read the block; what it reads is its own section of it.
            env["STUB_RATE"] = str(recording.roqsim.rate_hz)
        return env

    def records_scene_state(self, cfg, execution):
        return True

    def input_files(self, cfg, execution, vast_dir):
        # A path travels with the campaign; a `pkg:name` ref lives in the image.
        return [] if ":" in cfg.stage else [cfg.stage]


class RosOnlyBackend(SimulatorBackend):
    """Like a simulator with no SimulationInterface at all -- Gazebo, Isaac."""
    SUPPORTED_SHAPES = (SHAPE_ROS,)

    def containers(self, cfg, execution):
        return {"simulation": {"image": "gz:harmonic", "command": ["gz", "sim", "-s"]}}


class _ContainerSpec:
    """Only what a query carries: an image, and a name derived from it."""

    def __init__(self, image):
        self.image = image

    def container_name(self):
        return "aux-" + self.image.split("/")[-1].split(":")[0]


class PanelBackend(StubBackend):
    """A backend that contributes a panel, as roqsim contributes its ``scene3d``."""

    def default_panels(self, cfg, execution):
        return [{"scene3d": {}}]


@pytest.fixture(autouse=True)
def _register(monkeypatch):
    """Resolve 'stub'/'rosonly'/'panels' without installing an entry point. An unknown name
    raises, which is also how a campaign whose backend package is not installed here behaves."""
    import robovast.common.simulators as mod
    backends = {"stub": StubBackend, "rosonly": RosOnlyBackend, "panels": PanelBackend}
    monkeypatch.setattr(mod, "resolve_backend",
                        lambda name, base_dir="": backends[name]())


def _execution(mode="base", **sim):
    return {"mode": mode, "runs": 1,
            "containers": {"simulation": {"backend": "stub", **sim}}}


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


def test_a_query_is_asked_in_the_image_the_run_will_use():
    """``simulator_image`` and ``apply_backend`` answer one question, so they answer it alike.

    A campaign names an image for ``scenario`` because that is where scenario-execution runs.
    In the ROS shape the simulator has a container of its own, so that image is a different
    program's: answering with it sends ``input_files`` and ``describe_query`` into an image
    with no simulator in it, and the exec never starts.
    """
    authored = {"mode": "ros2", "containers": {
        "simulation": {"backend": "stub", "stage": "cell.usd"},
        "scenario": {"image": "robovast:1"}}}
    declared = StubBackend().containers(StageConfig(stage="cell.usd"), authored)
    ran = plan_containers(apply_backend(authored)).by_name("simulation").image
    assert simulator_image(authored, declared) == ran == "vendor/sim:1"


def test_a_folded_simulator_is_asked_in_the_container_it_folded_into():
    """The stepped shape moves the author's image onto ``scenario``; the query follows it."""
    authored = {"mode": "base", "containers": {
        "simulation": {"backend": "stub", "stage": "cell.usd", "image": "mine:1"}}}
    declared = StubBackend().containers(StageConfig(stage="cell.usd"), authored)
    ran = plan_containers(apply_backend(authored)).by_name("simulation").image
    assert simulator_image(authored, declared) == ran == "mine:1"


def test_an_authored_env_value_beats_the_backend():
    """A backend supplies defaults it knows, not decisions it takes away.

    ``scenario_env`` carries only *derived* variables; a campaign's own ``execution.env``
    is emitted separately by the backend. So winning here means the backend's value is
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


def test_calibration_is_a_container_key_not_a_backend_one():
    """``calibration`` sizes the container, so a simulator never sees it.

    It is the ``resources`` case one level up: a campaign sizing its simulator under
    ``execution.sizing: calibrated`` writes the block on the ``simulation`` container,
    which is also where the backend is named.
    """
    ex = apply_backend(_execution("ros2", stage="s",
                                  calibration={"size_on": 95, "limit": "declared"}))
    assert ex["containers"]["simulation"]["calibration"] == {"size_on": 95,
                                                             "limit": "declared"}


def test_a_folded_simulator_takes_its_calibration_with_it():
    """The allocation and the rule producing it must land on the same container.

    Stepped, the simulator IS the scenario container: a ``calibration`` left on the
    folded block would size nothing while ``resources`` moved.
    """
    ex = apply_backend(_execution("base", stage="s", resources={"cpu": 2},
                                  calibration={"size_on": 95}))
    assert ex["containers"]["scenario"]["calibration"] == {"size_on": 95}
    assert "calibration" not in ex["containers"]["simulation"]


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
    roqsim's ROQSIM_RECORD to the scenario container and nowhere else, so the run
    produced no recording at all -- while records_scene_state() still said True and
    validation accepted a scene3d panel with nothing to replay.
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


def test_a_query_that_never_started_reports_what_the_container_said(tmp_path):
    """An exec that does not start states its reason on the exception and nowhere else.

    The runner raises ``CalledProcessError``, which renders as an exit status and no reason
    at all -- so a missing simulator in the image reached the caller as a bare number, with
    the sentence naming it discarded.
    """
    import subprocess

    import robovast.common.config_generation as cg
    from robovast.common.simulators import ContainerQuery

    class Exploding:
        workspace = str(tmp_path)

        def run(self, command, callback=None):
            raise subprocess.CalledProcessError(
                126, command,
                output='exec did not start: invalid literal for int() with base 10: '
                       '\'exec: "sim": executable file not found in $PATH\'')

        def close(self):
            pass

    query = ContainerQuery(_ContainerSpec("vendor/sim:1"), ["sim", "inputs", "/config/w.yaml"])
    token = cg.set_container_runner_factory(lambda spec: Exploding())
    try:
        with pytest.raises(RuntimeError) as caught:
            cg._run_input_files_query(query, str(tmp_path))
    finally:
        cg._container_runner_factory.reset(token)
    assert "executable file not found" in str(caught.value)
    assert "vendor/sim:1" in str(caught.value)


# -- the panels no .vast has to write -----------------------------------------------

def _panel_types(merged):
    from robovast.common.config import flatten_panel_shorthand
    return [flatten_panel_shorthand(p)["type"] for p in merged]


@pytest.mark.parametrize("execution", [
    {},
    {"mode": "ros2"},
    {"mode": "ros2", "containers": {"scenario": {"image": "img:1"}}},
    {"mode": "ros2", "containers": {"simulation": {"backend": "notinstalled"}}},
])
def test_the_transport_bar_is_contributed_whatever_the_simulator(execution):
    """Including with no backend at all and with one that cannot be resolved.

    A run view without the transport has no clock to scrub and every other panel has nothing
    to follow, so it is not a thing a campaign can be missing -- and an unresolvable backend
    must not take it away, since the list is read to *show* a campaign whose simulator package
    is not installed here.
    """
    from robovast.common.simulators import merge_default_panels

    assert _panel_types(merge_default_panels([], execution)) == ["playback"]


def test_the_always_on_types_are_read_from_the_always_on_set():
    """The one place that says which panels are not content, so the service asking "is this run
    view bare?" and the merge deciding what to contribute cannot come to disagree."""
    from robovast.common.config import ALWAYS_ON_PANELS, always_on_panel_types

    assert always_on_panel_types() == set(_panel_types(ALWAYS_ON_PANELS))


def test_a_backend_contributes_on_top_of_the_always_on_set():
    """Order matters: the transport docks flush against the bottom edge, which the first
    ``bottom`` bar in the list takes, and the declared panels come last."""
    from robovast.common.simulators import merge_default_panels

    merged = merge_default_panels([{"log": {}}], _execution("ros2", stage="s", backend="panels"))
    assert _panel_types(merged) == ["playback", "scene3d", "log"]


@pytest.mark.parametrize("declared", [
    "playback",
    {"playback": None},
    {"playback": {"title": "Transport"}},
])
def test_a_campaign_that_declares_the_transport_keeps_its_own_entry(declared):
    """Every shorthand shape, because the dedup reads the type through the same function the
    service and the validation do -- a spelling it did not recognize would show the bar twice."""
    from robovast.common.simulators import merge_default_panels

    merged = merge_default_panels([{"log": {}}, declared], {})
    assert _panel_types(merged) == ["log", "playback"]
    assert merged[-1] == declared


def test_a_declared_transport_keeps_its_position_among_the_contributed_panels():
    from robovast.common.simulators import merge_default_panels

    declared = [{"playback": {"position": {"anchor": "top", "height": 32}}}]
    merged = merge_default_panels(declared, _execution("ros2", stage="s", backend="panels"))
    assert _panel_types(merged) == ["scene3d", "playback"]
    assert merged[-1] == declared[0]


# -- the recording block reaches the backend ------------------------------------------

def test_the_recording_block_reaches_the_backend_on_every_route():
    """apply_backend, the per-job overlay and the sidecar all hand the backend the same
    block, so what the simulator is asked to record cannot differ by route."""
    from robovast.common.config import RecordingConfig
    from robovast.common.execution import sidecar_backend_env
    from robovast.common.simulators import sim_job_overlay

    recording = RecordingConfig.model_validate({"roqsim": {"rate_hz": 25}})
    ex = apply_backend(_execution("ros2", stage="s"), recording=recording)
    assert ex["_backend_env"]["STUB_RATE"] == "25.0"
    assert sidecar_backend_env(ex, "simulation")["STUB_RATE"] == "25.0"
    overlay = sim_job_overlay(ex, {"backend": "stub", "stage": "s"}, recording=recording)
    assert overlay["env"]["STUB_RATE"] == "25.0"
    # And its absence is a block, not an error: the backend is told there is none.
    assert "STUB_RATE" not in apply_backend(_execution("ros2", stage="s"))["_backend_env"]
