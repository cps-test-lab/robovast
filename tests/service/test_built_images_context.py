# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""The service registers, for each span that composes, the image each built container runs.

A campaign's span maps a container to the image the campaign pinned for it, and a replay to the
image its source asked the simulator in. A preview or a validation maps it to the image the
store holds, building it first -- through the build ``start_campaign`` runs, so the campaign
then finds it built. An exec maps an unbuilt one to ``None``. Composition reads the map
(``config_generation.set_built_images``) when it asks the simulator.
"""

import json
import textwrap
import threading
import time
import pytest

from robovast.common import config_generation
from robovast.common.config_generation import REPLAYED_BASE, built_images
from robovast.common.errors import (CampaignConfigError, ImageBuildFailed,
                                    ImageStoreUnavailable, InsufficientStorageError)
from robovast.execution.backends import RunOptions
from robovast.service.image_store import ImageBuildStore, ImageRef
from robovast.service.interface import ImageBuildError, ImageBuildRef, ImageBuildStatus
from robovast.service.service_base import WorkspaceTarget
from tests.service.null_service import NullService, serving

BUILT_REF = "registry.example/robovast-builds:simulation-1"


def _vast(tmp_path):
    (tmp_path / "scenario.osc").write_text("scenario nav:\n    do serial:\n        wait elapsed(1s)\n")
    vast = tmp_path / "campaign.vast"
    vast.write_text(textwrap.dedent("""\
        version: 7
        metadata: {name: built-image}
        configuration:
        - name: depot
        execution:
          mode: ros2
          containers:
            simulation:
              backend: roqsim
              config: roqsim_scenes:depot
              python_packages: [example-world-package==1.0]
          runs: 1
          scenario_file: scenario.osc
        """))
    return str(vast)


class _Store(ImageBuildStore):
    """The image store: one content-addressed ref per container, present once built."""

    def __init__(self):
        self.built = set()

    def ref_for(self, spec, project_dir):
        del project_dir
        return ImageRef(ref=f"registry.example/robovast-builds:{spec.tag}-1",
                        identity=f"build:{spec.tag}@1", build_id=f"b-{spec.tag}")

    def present(self, ref):
        return ref.build_id in self.built


class _BuildingService(NullService):
    """A NullService with an image store and a builder: a build is started by the hooks
    ``start_campaign`` uses, and lands -- or fails -- on its first status read."""

    def __init__(self, *args, fail=False, **kwargs):
        super().__init__(*args, **kwargs)
        self._image_store = _Store()
        self.started = []
        self.fail = fail
        #: Set to hold every build in ``building`` until it is set.
        self.release = None

    def _start_build_images(self, project, campaign_config, image_project=None,
                            image_project_tag=None, should_stop=None) -> list:
        specs, project_dir = self._build_specs_for(project, campaign_config)
        refs = []
        for spec in specs.values():
            found = self._images.ref_for(spec, project_dir)
            cached = self._images.present(found)
            # A build already running for these inputs is joined, as the cluster's is.
            if not cached and found.build_id not in self.started:
                self.started.append(found.build_id)
            refs.append(ImageBuildRef(build_id=found.build_id, tag=spec.tag, cached=cached))
        return refs

    def _resolve_built_images(self, project, campaign_config, image_project=None,
                              image_project_tag=None, should_stop=None) -> dict:
        specs, project_dir = self._build_specs_for(project, campaign_config)
        return {name: self._images.ref_for(spec, project_dir).ref
                for name, spec in specs.items()}

    def get_image_build_status(self, build_id):
        if build_id not in self.started:
            raise KeyError(build_id)        # nothing was ever started for these inputs
        if self.release is not None and not self.release.is_set():
            return ImageBuildStatus(build_id=build_id, tag="simulation", phase="building")
        if self.fail:
            return ImageBuildStatus(build_id=build_id, tag="simulation", phase="failed",
                                    done=True, error=ImageBuildError(
                                        phase="pip", message="no matching distribution"))
        self._image_store.built.add(build_id)
        return ImageBuildStatus(build_id=build_id, tag="simulation", phase="succeeded",
                                done=True)


@pytest.fixture
def building(tmp_path):
    """A service that builds, over a workspace holding the campaign."""
    vast = _vast(tmp_path)

    def _make(fail=False):
        service = _BuildingService(store=serving(tmp_path / "results",
                                                 tmp_path / "workspaces").store, fail=fail)
        service._resolve_project = (  # noqa: SLF001
            lambda workspace_id, path: WorkspaceTarget(config_path=vast))
        return service, vast

    return _make


def test_a_campaign_span_maps_each_container_to_the_image_it_pinned(tmp_path):
    service = serving(tmp_path / "results", tmp_path / "workspaces")
    options = RunOptions(images={"simulation": BUILT_REF})

    with service._built_images_context(_vast(tmp_path), options=options) as images:
        assert built_images() == images == {
            "simulation": {"ref": BUILT_REF, "identity": BUILT_REF}}
    assert built_images() is None, "the span ends with the context"


def test_an_exec_registers_an_unbuilt_image_as_not_built_and_builds_nothing(building):
    service, vast = building()
    with service._built_images_context(vast) as images:
        assert images == {"simulation": None}
    assert service.started == []


def test_only_the_simulators_container_is_resolved(tmp_path, monkeypatch):
    """An archived campaign's other containers may not resolve at all -- a role recorded by
    tag only -- and nothing asks them: an exec into one must not fail over it."""
    service = serving(tmp_path / "results", tmp_path / "workspaces")
    (tmp_path / "scenario.osc").write_text("scenario nav:\n    do serial:\n        wait elapsed(1s)\n")
    vast = tmp_path / "campaign.vast"
    vast.write_text(textwrap.dedent("""\
        version: 7
        metadata: {name: built-image}
        configuration:
        - name: depot
        execution:
          mode: ros2
          containers:
            scenario: {python_packages: [example-scenario-package==1.0]}
            simulation: {backend: roqsim, config: roqsim_scenes:depot}
          runs: 1
          scenario_file: scenario.osc
        """))

    def _unresolvable(*_args, **_kwargs):
        raise ValueError("the campaign recorded only a tag for this role")

    monkeypatch.setattr(service, "_resolve_exec_image", _unresolvable)
    with service._built_images_context(str(vast), campaign_id="c-1") as images:
        assert images == {}


def test_an_unresolvable_campaign_record_fails_only_a_query_that_asks(tmp_path, monkeypatch):
    """An exec into an archived campaign whose record holds no digest for the simulator still
    reaches its other containers; a query of the simulator is refused with the reason."""
    from robovast.common.config_generation import _resolve_built_spec
    from robovast.common.variation.container_runner import ContainerSpec

    service = serving(tmp_path / "results", tmp_path / "workspaces")

    def _unresolvable(*_args, **_kwargs):
        raise ValueError("the campaign recorded only a tag for this role")

    monkeypatch.setattr(service, "_resolve_exec_image", _unresolvable)
    with service._built_images_context(_vast(tmp_path), campaign_id="c-1") as images:
        assert images == {"simulation": {
            "unresolved": "the campaign recorded only a tag for this role"}}
        with pytest.raises(ValueError, match="recorded only a tag"):
            _resolve_built_spec(ContainerSpec(image="build:simulation"))


def test_a_preview_builds_an_unbuilt_image_and_says_so(building):
    service, vast = building()
    stages = []
    with service._built_images_context(vast, build="wait", on_stage=stages.append) as images:
        assert images == {"simulation": {"ref": BUILT_REF, "identity": "build:simulation@1"}}
    assert service.started == ["b-simulation"]
    assert len(stages) == 1
    assert "building the campaign's image for container 'simulation'" in stages[0]
    assert "b-simulation" in stages[0]


def test_a_second_preview_reuses_the_image_without_a_build(building):
    service, vast = building()
    with service._built_images_context(vast, build="wait"):
        pass
    stages = []
    with service._built_images_context(vast, build="wait", on_stage=stages.append) as images:
        assert images["simulation"]["ref"] == BUILT_REF
    assert service.started == ["b-simulation"], "built once"
    assert stages == [], "nothing is said to be building"


def test_a_failed_build_fails_loudly_naming_its_log(building):
    service, vast = building(fail=True)
    with pytest.raises(ImageBuildFailed) as failed:
        with service._built_images_context(vast, build="wait"):
            pass
    message = str(failed.value)
    assert "no matching distribution" in message
    assert "get_image_build_log(build_id='b-simulation')" in message
    assert "vast image log b-simulation" in message


def test_start_campaign_after_a_preview_reuses_its_image(building, monkeypatch):
    service, vast = building()
    with service._built_images_context(vast, build="wait"):
        pass
    used = {}
    monkeypatch.setattr("robovast.execution.controller.run_batch_campaign",
                        lambda *a, **k: used.update(k["options"].images or {}))
    monkeypatch.setattr(NullService, "_record_campaign_failure",
                        lambda self, *a, **k: used.setdefault("failed", a))

    from robovast.service.interface import CreateCampaignRequest
    ref = service.create_campaign(CreateCampaignRequest(workspace_id="ws"))
    service._campaigns[ref.campaign_id].thread.join(10)  # noqa: SLF001

    assert "failed" not in used, used.get("failed")
    assert service.started == ["b-simulation"], "the campaign's build is the preview's"
    assert used == {"simulation": BUILT_REF}


@pytest.mark.requires_simulator
def test_a_background_preview_says_it_is_building_then_answers(building, monkeypatch):
    """The launch bar's preview: ``composing`` with a ``stage`` naming the build while the
    image builds, then the configurations, composed with the simulator asked in that image."""
    service, _vast_file = building()
    service.release = threading.Event()
    monkeypatch.setattr(service, "_BUILD_POLL_SECONDS", 0.01)
    asked = []

    class _Runner:
        def __init__(self, image):
            self._image = image

        def run(self, command, emit):
            asked.append(self._image)
            if command[:2] == ["roqsim", "check"]:
                emit(json.dumps({"ok": True, "problems": [], "world": {"endpoints": []}}))
                return
            emit(json.dumps({"packaged": True, "inputs": []}))

        def image_digest(self):
            return "sha256:" + "0" * 64

        def close(self):
            pass

    monkeypatch.setattr(config_generation, "_make_container_runner",
                        lambda spec, **_kwargs: _Runner(spec.image))

    def _poll(until):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            answer = service.preview_configurations("ws", wait=False)
            if until(answer):
                return answer
            time.sleep(0.01)
        raise AssertionError("the preview never got there")

    building_now = _poll(lambda a: a.stage)
    assert building_now.state == "composing"
    assert "building the campaign's image for container 'simulation'" in building_now.stage
    service.release.set()
    ready = _poll(lambda a: a.state != "composing")
    assert ready.state == "ready", ready.error
    assert ready.configs == 1
    assert asked and set(asked) == {BUILT_REF}, "the simulator is asked in the built image"
    assert service.started == ["b-simulation"]


# -- a validation starts the build and does not wait for it ------------------------------

def _validated(service):
    started = time.monotonic()
    report = service.validate_project("ws", check_world=True, check_scenario=False)
    return report, time.monotonic() - started


@pytest.mark.requires_simulator
def test_a_validation_starts_the_build_and_reports_unchecked_without_waiting(
        building, monkeypatch):
    service, _vast_file = building()
    service.release = threading.Event()             # the build never finishes here
    monkeypatch.setattr(service, "_wait_for_build", lambda *a, **k: pytest.fail(
        "a validation must not wait for the build"))

    report, took = _validated(service)

    assert service.started == ["b-simulation"], "the campaign's build is started"
    assert took < 10
    assert report.valid is False
    assert report.world_checked is False
    unchecked = [p for p in report.problems if p.severity == "unchecked"]
    assert unchecked, report.problems
    assert "is being built (build b-simulation)" in unchecked[0].message
    assert "get_image_build_log(build_id='b-simulation')" in unchecked[0].message
    assert "vast image log b-simulation" in unchecked[0].message


@pytest.mark.requires_simulator
def test_a_validation_while_the_build_runs_joins_it(building, monkeypatch):
    service, _vast_file = building()
    service.release = threading.Event()
    service.started.append("b-simulation")          # a preview's build, still running
    monkeypatch.setattr(service, "_wait_for_build", lambda *a, **k: pytest.fail(
        "a validation must not wait for the build"))

    report, _took = _validated(service)

    assert service.started == ["b-simulation"], "joined, not started a second time"
    assert any("is being built (build b-simulation)" in p.message for p in report.problems)


def _refuse_the_registry(service, monkeypatch):
    def _no_registry(*_args, **_kwargs):
        raise CampaignConfigError("no image registry is configured for builds")
    monkeypatch.setattr(service, "_start_build_images", _no_registry)
    return "cannot be looked up or built here: no image registry is configured for builds"


def _refuse_the_storage(service, monkeypatch):
    def _full(_action):
        raise InsufficientStorageError("Cannot build an image. Free space is below the reserve.",
                                       next_step="vast service cache --clear")
    monkeypatch.setattr(service, "_admit_storage", _full)
    return "Free space is below the reserve. Next: vast service cache --clear"


def _refuse_the_lookup(service, monkeypatch):
    def _unreachable(_ref):
        raise ImageStoreUnavailable("the image registry did not answer")
    monkeypatch.setattr(service._image_store, "present", _unreachable)  # noqa: SLF001
    return "cannot be looked up or built here: the image registry did not answer"


@pytest.mark.requires_simulator
@pytest.mark.parametrize("refuse", [_refuse_the_registry, _refuse_the_storage,
                                    _refuse_the_lookup])
def test_a_build_that_cannot_start_is_the_simulators_unchecked_question(
        building, monkeypatch, refuse):
    service, _vast_file = building()
    said = refuse(service, monkeypatch)

    report, _took = _validated(service)

    assert service.started == []
    assert report.valid is False
    assert report.world_checked is False
    unchecked = [p for p in report.problems if p.severity == "unchecked"]
    assert [p.stage for p in unchecked] == ["generation"], report.problems
    assert said in unchecked[0].message
    assert "image for container 'simulation'" in unchecked[0].message


def test_a_defect_in_starting_the_build_is_not_reported_as_a_refusal(building, monkeypatch):
    service, _vast_file = building()

    def _defect(*_args, **_kwargs):
        raise AttributeError("'NoneType' object has no attribute 'tag'")
    monkeypatch.setattr(service, "_start_build_images", _defect)

    report, _took = _validated(service)

    assert report.valid is False
    assert [p.stage for p in report.problems] == ["error"], report.problems
    assert "has no attribute 'tag'" in report.problems[0].message
    assert "cannot be looked up or built" not in report.problems[0].message


def test_a_file_with_a_problem_of_its_own_reports_it_and_starts_no_build(
        building, monkeypatch):
    service, vast_file = building()
    with open(vast_file, encoding="utf-8") as handle:
        text = handle.read()
    with open(vast_file, "w", encoding="utf-8") as handle:
        handle.write(text.replace("scenario_file: scenario.osc", "scenario_file: missing.osc"))
    _refuse_the_registry(service, monkeypatch)
    monkeypatch.setattr(service, "_built_images_context", lambda *a, **k: pytest.fail(
        "a file with a problem of its own composes nothing and starts no build"))

    report, _took = _validated(service)

    assert report.valid is False
    assert report.world_checked is False
    assert [p.stage for p in report.problems] == ["scenario_file"], report.problems
    assert "missing.osc" in report.problems[0].message


# -- a replay asks the simulator in the image its source asked it in ------------------------

def test_a_replay_whose_source_asked_the_built_image_asks_it_again(tmp_path):
    service = serving(tmp_path / "results", tmp_path / "workspaces")
    pinned = "registry.example/robovast-builds@sha256:" + "1" * 64
    options = RunOptions(images={"simulation": pinned}, images_fixed=True,
                         aux_images={"aux-robovast-builds": pinned.split("@")[1]})

    with service._built_images_context(_vast(tmp_path), options=options) as images:
        assert images == {"simulation": {"ref": pinned, "identity": pinned}}


def test_a_replay_whose_source_asked_the_base_image_asks_the_base_again(tmp_path):
    service = serving(tmp_path / "results", tmp_path / "workspaces")
    pinned = "registry.example/robovast-builds@sha256:" + "1" * 64
    options = RunOptions(images={"simulation": pinned}, images_fixed=True,
                         aux_images={"aux-robovast-roqsim": "sha256:" + "2" * 64})

    with service._built_images_context(_vast(tmp_path), options=options) as images:
        assert images == {"simulation": REPLAYED_BASE}
        assert config_generation.replays_base_image("simulation")


def test_a_replay_whose_source_recorded_another_build_asks_the_base_again(tmp_path):
    """Every built image shares a repository, so its auxiliary container name: only the pinned
    digest says whether the source asked the simulator in this one."""
    service = serving(tmp_path / "results", tmp_path / "workspaces")
    pinned = "registry.example/robovast-builds@sha256:" + "1" * 64
    options = RunOptions(images={"simulation": pinned}, images_fixed=True,
                         aux_images={"aux-robovast-builds": "sha256:" + "3" * 64})

    with service._built_images_context(_vast(tmp_path), options=options) as images:
        assert images == {"simulation": REPLAYED_BASE}


@pytest.mark.requires_simulator
def test_a_replay_composes_in_the_image_its_source_recorded(tmp_path, monkeypatch):
    """End to end through composition: the query of a replay whose source asked the base image
    runs in the base image, and the image recorded for it is that one."""
    pytest.importorskip("robovast_sim_roqsim")
    from robovast.common.config_generation import generate_scenario_variations

    asked = []

    class _Runner:
        def __init__(self, image):
            self._image = image

        def run(self, command, emit):
            asked.append(self._image)
            if command[:2] == ["roqsim", "check"]:
                emit(json.dumps({"ok": True, "problems": [], "world": {"endpoints": []}}))
                return
            emit(json.dumps({"packaged": False, "inputs": [command[-1]]}))

        def image_digest(self):
            return "sha256:" + "0" * 64

        def close(self):
            pass

    monkeypatch.setattr(config_generation, "_make_container_runner",
                        lambda spec, **_kwargs: _Runner(spec.image))
    (tmp_path / "scenario.osc").write_text("scenario nav:\n    do serial:\n        wait elapsed(1s)\n")
    (tmp_path / "worlds").mkdir()
    (tmp_path / "worlds" / "depot.yaml").write_text("sim: {pacing: realtime}\n")
    vast = tmp_path / "campaign.vast"
    vast.write_text(textwrap.dedent("""\
        version: 7
        metadata: {name: built-image}
        configuration:
        - name: depot
        execution:
          mode: ros2
          containers:
            simulation:
              backend: roqsim
              image: registry.example/roqsim-base:1
              config: worlds/depot.yaml
              python_packages: [example-world-package==1.0]
          runs: 1
          scenario_file: scenario.osc
        """))
    service = serving(tmp_path / "results", tmp_path / "workspaces")
    options = RunOptions(images={"simulation": "registry.example/robovast-builds@sha256:"
                                 + "1" * 64},
                         images_fixed=True,
                         aux_images={"aux-roqsim-base": "sha256:" + "2" * 64})

    with service._built_images_context(str(vast), options=options):
        data = generate_scenario_variations(str(vast), progress_update_callback=lambda m: None,
                                            output_dir=str(tmp_path / "gen"), use_cache=False)

    assert set(asked) == {"registry.example/roqsim-base:1"}
    assert set(data["aux_container_images"].values()) == {"registry.example/roqsim-base:1"}
