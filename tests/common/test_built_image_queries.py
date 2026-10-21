# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""A simulator that adds packages is asked its questions in the image the campaign builds.

A world shipped by the campaign's own package exists in that image and nowhere else, so which
streams are on demand, which files a world is made of and what it provides are asked there --
never in the backend's base image, whose answer would be about another campaign. A campaign's
span registers the image it built; a validation before the first build says the questions wait
for it, as a warning, and asks no other image.
"""

import json
import textwrap

import pytest

from robovast.common import config_generation
from robovast.common.config_generation import (
    generate_scenario_variations,
    set_built_images,
)
from robovast.common.errors import ImageNotBuilt

pytest.importorskip("robovast_sim_roqsim",
                    reason="the roqsim backend extra is not installed")

#: Every test here drives a simulator backend end to end.
pytestmark = pytest.mark.requires_simulator

_SCENARIO = """\
import osc.robotics

scenario nav:
    goal_pose: pose_3d = pose_3d()
    do serial:
        wait elapsed(1s)
"""

#: What `roqsim check --json` reports of a world with a camera rendered only on demand.
CHECK_REPORT = {"ok": True, "problems": [], "world": {"endpoints": [
    {"name": "image", "direction": "out", "namespace": "robot", "topic": "camera/image_raw",
     "type": "sensor_msgs.msg.Image", "lazy": True, "cost": "render", "rebuildable": True}]}}

BUILT = {"ref": "registry.example/robovast-builds:simulation-1",
         "identity": "build:simulation@1"}
REBUILT = {"ref": "registry.example/robovast-builds:simulation-2",
           "identity": "build:simulation@2"}


class _Runner:
    """The image a query was sent to, answering ``roqsim scenes inputs`` and ``roqsim check``."""

    def __init__(self, image, asked):
        self._image = image
        self._asked = asked

    def run(self, command, emit):
        self._asked.append((self._image, list(command)))
        if command[:2] == ["roqsim", "check"]:
            emit(json.dumps(CHECK_REPORT))
            return
        emit(json.dumps({"packaged": False, "inputs": [command[-1]]}))

    def image_digest(self):
        return "sha256:" + "0" * 64

    def close(self):
        pass


@pytest.fixture
def asked(monkeypatch):
    """Every ``(image, command)`` composition sent to a container."""
    calls: list = []
    monkeypatch.setattr(config_generation, "_make_container_runner",
                        lambda spec, **_kwargs: _Runner(spec.image, calls))
    return calls


@pytest.fixture
def registered():
    """Register the built images for the test, as a service's span does, and unregister them."""
    tokens = []

    def _register(images):
        tokens.append(set_built_images(images))

    yield _register
    for token in reversed(tokens):
        token.var.reset(token)


def _project(tmp_path):
    """A campaign whose simulator container adds a package, and records every topic."""
    (tmp_path / "scenario.osc").write_text(_SCENARIO)
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
              config: worlds/depot.yaml
              python_packages: [example-world-package==1.0]
          runs: 1
          scenario_file: scenario.osc
        """))
    return vast


def _compose(vast, tmp_path, use_cache=False):
    return generate_scenario_variations(
        str(vast), progress_update_callback=lambda m: None,
        output_dir=str(tmp_path / "gen"), use_cache=use_cache)


def test_a_campaign_asks_its_simulator_in_the_image_it_built(tmp_path, asked, registered):
    registered({"simulation": BUILT})
    data = _compose(_project(tmp_path), tmp_path)

    assert {image for image, _command in asked} == {BUILT["ref"]}
    assert any(command[:2] == ["roqsim", "check"] for _image, command in asked)
    # The answer the built image gave is the one frozen into the configuration.
    assert data["configs"][0]["recording"]["ros2"]["left_out"] == [
        {"topic": "/robot/camera/image_raw", "type": "sensor_msgs/msg/Image"}]
    # And the image recorded for a replay is the one that answered.
    assert set(data["aux_container_images"].values()) == {BUILT["ref"]}


def test_a_campaign_span_without_its_built_image_does_not_start(tmp_path, asked, registered):
    registered({})
    with pytest.raises(ImageNotBuilt, match="'simulation' is not built"):
        _compose(_project(tmp_path), tmp_path)
    assert not asked, "no other image is asked in its place"


def test_validation_before_the_build_warns_and_asks_no_other_image(tmp_path, asked,
                                                                    registered):
    from robovast.common.config_validation import validate_project_file

    registered({"simulation": None})
    report = validate_project_file(str(_project(tmp_path)))

    warnings = [p for p in report["problems"] if p["severity"] == "warning"]
    assert [p["stage"] for p in warnings] == ["generation"]
    assert "not built" in warnings[0]["message"]
    assert "start_campaign" in warnings[0]["message"]
    assert report["valid"] is True, report["problems"]
    assert report["configs"] == 1
    assert not asked, "the base image is never asked in place of the one the campaign builds"


def test_the_world_check_before_the_build_warns_and_asks_no_other_image(tmp_path, registered):
    import yaml

    from robovast.service.world_query import world_problems

    def _exec(_request):
        raise AssertionError("no container is asked while the campaign's image is not built")

    def _resolve(_request):
        raise AssertionError("no image is resolved while the campaign's image is not built")

    vast = _project(tmp_path)
    registered({"simulation": None})
    problems = world_problems(_exec, resolve_call=_resolve, workspace_id="ws-1",
                              config_path="campaign.vast", vast_dir=str(tmp_path),
                              parameters=yaml.safe_load(vast.read_text()))

    assert [p["severity"] for p in problems] == ["warning", "warning"]
    assert all("start_campaign" in p["message"] for p in problems)


def test_a_rebuilt_image_is_asked_again_not_answered_from_the_cache(tmp_path, asked,
                                                                    registered):
    vast = _project(tmp_path)
    registered({"simulation": BUILT})
    _compose(vast, tmp_path, use_cache=True)
    registered({"simulation": REBUILT})
    _compose(vast, tmp_path, use_cache=True)

    # The on-demand question is composition's own, so it is asked again only on a cache miss.
    assert any(image == REBUILT["ref"] and command[:2] == ["roqsim", "check"]
               for image, command in asked)
