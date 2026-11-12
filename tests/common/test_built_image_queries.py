# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""A simulator that adds packages is asked its questions in the image the campaign builds.

A world shipped by the campaign's own package exists in that image and nowhere else, so which
streams are on demand, which files a world is made of and what it provides are asked there --
never in the backend's base image, whose answer would be about another campaign. A campaign's
span registers the image it built; a composition no service arranged has no such image and
refuses, naming a preview through a service, rather than asking another image.
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


def test_validation_without_a_service_refuses_and_asks_no_other_image(tmp_path, asked):
    from robovast.common.config_validation import validate_project_file

    report = validate_project_file(str(_project(tmp_path)))

    assert report["valid"] is False
    assert [p["severity"] for p in report["problems"]] == ["unchecked"]
    message = report["problems"][0]["message"]
    assert "only a RoboVAST service builds it" in message
    assert "vast workspace preview" in message
    assert not asked, "the base image is never asked in place of the one the campaign builds"


def test_the_local_cli_refuses_naming_a_preview_through_a_service(tmp_path, asked):
    from click.testing import CliRunner

    from robovast.configuration.configuration_utils.cli import configuration

    result = CliRunner().invoke(configuration, ["info", str(_project(tmp_path))])

    assert result.exit_code != 0
    assert "only a RoboVAST service builds it" in result.output
    assert "vast workspace preview" in result.output
    assert not asked


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
