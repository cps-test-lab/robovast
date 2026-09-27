# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A replay composes against the digests its source recorded, never against the environment.

A retrigger and an adoption after a service restart both compose the campaign again. Composition
is where a ``family:`` ref becomes an image, so it is where a replay would pick up whatever
``ROBOVAST_PROJECT`` / ``ROBOVAST_PROJECT_TAG`` say by then -- and where a cache entry composed
from those tags would hand a replay configurations made by other bytes. Three things hold it:

- ``family:`` refs resolve to the recorded digest of their container, and one the record does
  not fix is refused rather than resolved;
- the recorded digests are part of the composition cache key, so a replay never reuses an entry
  composed from tags;
- a step served from a cache -- a composition, an up-to-date input generator -- still has its
  helper images fixed, because checking the cache resolves them and the replay of that
  campaign recomposes and runs them.
"""

import pytest

from robovast.common.config_generation import (_build_generate_cache_key,
                                               _cached_entry_is_current, set_aux_image_fixer)
from robovast.common.errors import CampaignConfigError
from robovast.common.execution import resolve_family_images_in_containers
from robovast.common.input_generation import run_input_generators
from robovast.execution.backends import RunOptions
from robovast.execution.controller import _replayed_pins

SIM_DIGEST = "registry.example.com/robovast/robovast-roqsim@sha256:" + "a" * 64
SCENARIO_DIGEST = "registry.example.com/robovast/robovast@sha256:" + "b" * 64


@pytest.fixture
def moved_project(monkeypatch):
    """The environment a replay meets: the project and its tag moved on since the launch."""
    monkeypatch.setenv("ROBOVAST_PROJECT", "registry.example.com/elsewhere")
    monkeypatch.setenv("ROBOVAST_PROJECT_TAG", "moved-on")


def test_a_family_ref_resolves_to_its_recorded_digest(moved_project):
    containers = {"simulation": {"image": "family:robovast-roqsim"},
                  "scenario": {"image": "family:robovast"}}

    resolve_family_images_in_containers(
        containers, project="registry.example.com/also-ignored", tag="x",
        pins={"simulation": SIM_DIGEST, "scenario": SCENARIO_DIGEST})

    assert containers == {"simulation": {"image": SIM_DIGEST},
                          "scenario": {"image": SCENARIO_DIGEST}}


def test_a_family_ref_the_record_does_not_fix_is_refused_by_name(moved_project):
    containers = {"simulation": {"image": "family:robovast-roqsim"},
                  "sut": {"image": "family:robovast"}}

    with pytest.raises(CampaignConfigError) as e:
        resolve_family_images_in_containers(containers, pins={"simulation": SIM_DIGEST})

    assert "sut (family:robovast)" in str(e.value)
    assert "simulation" not in str(e.value)


def test_a_stated_image_is_left_as_written(moved_project):
    """Only ``family:`` refs are composition's to resolve; the plan substitutes the recorded
    digest for a stated one by container name."""
    containers = {"sut": {"image": "registry.example.com/team/sut:1"}}

    resolve_family_images_in_containers(containers, pins={})

    assert containers == {"sut": {"image": "registry.example.com/team/sut:1"}}


def test_a_fresh_launch_resolves_from_its_project(moved_project):
    containers = {"simulation": {"image": "family:robovast-roqsim"}}

    resolve_family_images_in_containers(containers, project="registry.example.com/dev",
                                        tag="feature-x")

    assert containers["simulation"]["image"] == \
        "registry.example.com/dev/robovast-roqsim:feature-x"


def test_only_a_replay_hands_composition_pins():
    assert _replayed_pins(RunOptions(images={"scenario": SCENARIO_DIGEST})) is None
    assert _replayed_pins(RunOptions(images={"scenario": SCENARIO_DIGEST},
                                     images_fixed=True)) == {"scenario": SCENARIO_DIGEST}


def _key(vast, **kwargs):
    return _build_generate_cache_key(
        variation_file=str(vast), vast_dir=str(vast.parent), scenario_file="",
        run_files=[], analysis_files=[], configurations=[], **kwargs).fingerprint()


def test_a_replay_never_shares_a_cache_entry_with_a_composition_from_tags(tmp_path):
    vast = tmp_path / "campaign.vast"
    vast.write_text("version: 1\n", encoding="utf-8")

    from_tags = _key(vast)
    replay = _key(vast, image_pins={"simulation": SIM_DIGEST})
    other_record = _key(vast, image_pins={"simulation": SCENARIO_DIGEST})

    assert from_tags != replay
    assert replay != other_record
    assert replay == _key(vast, image_pins={"simulation": SIM_DIGEST})


def test_a_cached_composition_hands_its_helper_images_to_the_campaigns_fixer():
    """No helper runs for a cache hit, and a replay of the campaign recomposes and runs one --
    so checking the entry fixes the images it was composed with, like a started helper's."""
    fixed = []

    def fixer(spec):
        fixed.append((spec.container_name(), spec.image))
        return SIM_DIGEST

    token = set_aux_image_fixer(fixer)
    try:
        assert _cached_entry_is_current(
            {"aux_containers": ["aux-robovast-roqsim"],
             "aux_container_images": {"aux-robovast-roqsim": "family:robovast-roqsim"},
             "aux_image_digests": {"aux-robovast-roqsim": "sha256:" + "a" * 64}})
    finally:
        token.var.reset(token)

    assert fixed == [("aux-robovast-roqsim", "family:robovast-roqsim")]


def test_an_up_to_date_generator_still_fixes_its_helper_image(tmp_path):
    """A generator's cache is not archived with its campaign, so a replay regenerates and runs
    the helper an up-to-date entry skipped."""
    (tmp_path / "gen.py").write_text(
        "import os\n"
        "from robovast.common.input_generation import BaseInputGenerator, write_manifest\n"
        "from robovast.common.variation.container_runner import ContainerSpec\n"
        "class G(BaseInputGenerator):\n"
        "    @classmethod\n"
        "    def get_required_container(cls, parameters):\n"
        "        return ContainerSpec(image='registry.example.com/team/gen:1')\n"
        "    def __call__(self, vast_dir, out_dir, **kw):\n"
        "        open(os.path.join(out_dir, 'x'), 'w').write('x')\n"
        "        write_manifest(out_dir, [os.path.join(vast_dir, 'gen.py')])\n")
    entries = [{"./gen.py:G": {"out": "files/x"}}]

    class _Runner:
        workspace = str(tmp_path)

        def close(self):
            pass

    fixed = []

    def fixer(spec):
        fixed.append(spec.image)
        return "registry.example.com/team/gen@sha256:" + "c" * 64

    token = set_aux_image_fixer(fixer)
    try:
        run_input_generators(str(tmp_path), entries,
                             container_runner_factory=lambda spec: _Runner())
        fixed.clear()
        assert run_input_generators(str(tmp_path), entries)[0]["cached"] is True
    finally:
        token.var.reset(token)

    assert fixed == ["registry.example.com/team/gen:1"]


def test_the_helper_images_cross_the_cache_and_the_isolated_boundary():
    """A plain top-level key, so ``_result_to_transport`` keeps it for the cache entry a hit
    reads and for the isolated worker's result."""
    from robovast.common.config_generation import _result_from_transport, _result_to_transport

    images = {"aux-robovast-roqsim": "family:robovast-roqsim"}
    transport = _result_to_transport({"configs": [], "aux_container_images": images,
                                      "_output_dir": "/tmp/x", "_transient_files": []})
    assert _result_from_transport(transport, None)["aux_container_images"] == images
