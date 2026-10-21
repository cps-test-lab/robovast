# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""The service registers, for each span that composes, the image each built container runs.

A campaign's span maps a container to the image the campaign pinned for it; a validation, a
preview or an exec maps it to the image the store holds, or to ``None`` while it is not built.
Composition reads the map (``config_generation.set_built_images``) when it asks the simulator.
"""

import textwrap

from robovast.common.config_generation import built_images
from robovast.common.errors import ImageNotBuilt
from robovast.execution.backends import RunOptions
from robovast.service.image_store import ImageRef
from tests.service.null_service import serving


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


def test_a_campaign_span_maps_each_container_to_the_image_it_pinned(tmp_path):
    service = serving(tmp_path / "results", tmp_path / "workspaces")
    options = RunOptions(images={"simulation": "registry.example/robovast-builds:simulation-1"})

    with service._built_images_context(_vast(tmp_path), options=options) as images:
        assert built_images() == images == {
            "simulation": {"ref": "registry.example/robovast-builds:simulation-1",
                           "identity": "registry.example/robovast-builds:simulation-1"}}
    assert built_images() is None, "the span ends with the context"


def test_outside_a_campaign_an_unbuilt_image_is_registered_as_not_built(tmp_path, monkeypatch):
    service = serving(tmp_path / "results", tmp_path / "workspaces")
    asked = []

    def _resolve(vast_file, container=None, campaign_id="", image_family=""):
        asked.append(container)
        raise ImageNotBuilt(f"the image for container {container!r} is not built.")

    monkeypatch.setattr(service, "_resolve_exec_image", _resolve)
    with service._built_images_context(_vast(tmp_path)) as images:
        assert images == {"simulation": None}
    assert asked == ["simulation"], "only the container the campaign builds is looked up"


def test_outside_a_campaign_a_built_image_is_named_by_its_identity(tmp_path, monkeypatch):
    service = serving(tmp_path / "results", tmp_path / "workspaces")
    monkeypatch.setattr(
        service, "_resolve_exec_image",
        lambda vast_file, container=None, campaign_id="", image_family="": ImageRef(
            ref="registry.example/robovast-builds:simulation-1",
            identity="build:simulation@1", build_id="b-1"))

    with service._built_images_context(_vast(tmp_path)) as images:
        assert images == {"simulation": {"ref": "registry.example/robovast-builds:simulation-1",
                                         "identity": "build:simulation@1"}}
