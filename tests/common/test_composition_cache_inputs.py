# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A cached composition is reused only while every input it read is unchanged.

Two inputs are known only once a composition has run, so its cache key, built from the
``.vast`` beforehand, cannot name them: the image of a helper container a variation declares,
whose ref a push moves, and a file a variation reads because another file names it -- the image
a map YAML points at. The entry records both and a hit is checked against them.
"""

import textwrap

import pytest

from robovast.common.config_generation import (generate_scenario_variations,
                                               set_aux_image_fixer,
                                               set_container_runner_factory)
from robovast.common.input_generation import run_input_generators

from .test_path_variation_cache import _project
from .test_path_variation_cache_map_image import _redraw_image

_HIT = "Loaded configurations from cache"
_HELPER = "registry.example.com/team/helper:1"


class _Runner:
    """A helper container that reports the digest its image currently has."""

    def __init__(self, digests, workspace):
        self._digests = digests
        self.workspace = workspace

    def run(self, command, progress_update_callback=None):
        del command, progress_update_callback

    def image_digest(self):
        return f"registry.example.com/team/helper@{self._digests['now']}"

    def close(self):
        pass


@pytest.fixture
def helper_image(tmp_path):
    """Install a runner factory whose image digest the test moves, as a push would."""
    digests = {"now": "sha256:" + "1" * 64}
    token = set_container_runner_factory(lambda spec: _Runner(digests, str(tmp_path)))
    yield digests
    token.var.reset(token)


def _helper_project(tmp_path):
    (tmp_path / "helper.py").write_text(textwrap.dedent(f"""\
        from robovast.common.variation import Variation
        from robovast.common.variation.container_runner import ContainerSpec

        class Helper(Variation):
            @classmethod
            def get_required_container(cls, parameters):
                return ContainerSpec(image="{_HELPER}")

            def variation(self, in_configs):
                self.container_runner.run(["build"])
                return in_configs
        """))
    (tmp_path / "scenario.osc").write_text(
        "import osc.robotics\n\nscenario cell_test:\n    do serial:\n        wait elapsed(1s)\n")
    vast = tmp_path / "campaign.vast"
    vast.write_text(textwrap.dedent("""\
        version: 7
        configuration:
        - name: cell
          variations:
          - ./helper.py:Helper: {}
        execution:
          containers:
            scenario: {image: scen:latest}
          runs: 1
          scenario_file: scenario.osc
        """))
    return vast


def _compose(vast, out):
    messages = []
    generate_scenario_variations(str(vast), progress_update_callback=messages.append,
                                 output_dir=str(out))
    return any(_HIT in m for m in messages)


def test_an_unchanged_helper_image_is_served_from_the_cache(tmp_path, helper_image):
    vast = _helper_project(tmp_path)
    assert not _compose(vast, tmp_path / "out")
    assert _compose(vast, tmp_path / "out")


def test_a_pushed_helper_image_is_not_served_from_the_cache(tmp_path, helper_image):
    vast = _helper_project(tmp_path)
    _compose(vast, tmp_path / "out")
    helper_image["now"] = "sha256:" + "2" * 64
    assert not _compose(vast, tmp_path / "out")
    assert _compose(vast, tmp_path / "out")


def test_a_campaign_checks_a_hit_against_its_pinned_digest(tmp_path, helper_image):
    """The campaign's fixer answers without a container, and the hit fixes the image."""
    vast = _helper_project(tmp_path)
    _compose(vast, tmp_path / "out")
    fixed = []

    def fixer(spec):
        fixed.append(spec.image)
        return "registry.example.com/team/helper@" + "sha256:" + "1" * 64

    token = set_aux_image_fixer(fixer)
    try:
        assert _compose(vast, tmp_path / "out")
    finally:
        token.var.reset(token)
    assert fixed == [_HELPER]


def test_an_unreadable_digest_fails_the_composition(tmp_path, helper_image):
    vast = _helper_project(tmp_path)
    token = set_aux_image_fixer(lambda spec: "")
    try:
        with pytest.raises(RuntimeError, match="digest"):
            _compose(vast, tmp_path / "out")
    finally:
        token.var.reset(token)


def test_a_redrawn_map_image_is_not_served_from_the_cache(tmp_path):
    """The ``.vast`` names the map YAML; only the variation knows the image it points at."""
    vast = _project(tmp_path)
    assert not _compose(vast, tmp_path / "out")
    assert _compose(vast, tmp_path / "out")
    _redraw_image(tmp_path)
    assert not _compose(vast, tmp_path / "out")


_GENERATOR = textwrap.dedent(f"""\
    import os
    from robovast.common.input_generation import BaseInputGenerator, write_manifest
    from robovast.common.variation.container_runner import ContainerSpec

    class Build(BaseInputGenerator):
        @classmethod
        def get_required_container(cls, parameters):
            return ContainerSpec(image="{_HELPER}")

        def __call__(self, vast_dir, out_dir, **params):
            src = os.path.join(vast_dir, "world.yaml")
            with open(os.path.join(out_dir, "scene.json"), "w") as fh:
                fh.write(open(src).read())
            write_manifest(out_dir, [src])
            return True, "built"
    """)


def test_a_pushed_generator_image_regenerates(tmp_path):
    (tmp_path / "gen.py").write_text(_GENERATOR)
    (tmp_path / "world.yaml").write_text("wall: 1\n")
    entries = [{"./gen.py:Build": {"out": "files/scene"}}]
    digests = {"now": "sha256:" + "1" * 64}

    def factory(spec):
        del spec
        workspace = tmp_path / "ws"
        workspace.mkdir(exist_ok=True)
        return _Runner(digests, str(workspace))

    run_input_generators(str(tmp_path), entries, container_runner_factory=factory)
    assert run_input_generators(str(tmp_path), entries,
                                container_runner_factory=factory)[0]["cached"] is True
    digests["now"] = "sha256:" + "2" * 64
    assert run_input_generators(str(tmp_path), entries,
                                container_runner_factory=factory)[0]["cached"] is False
