# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``campaign_role_image`` — which image holds a campaign's container role.

The answer keys caches and attributes artifacts, so a wrong one is worse than none: naming
the scenario container's image for the *simulation* role sent the run view's geometry build
into an image with neither the world nor the exporter, which surfaced only as ``exit status
127`` from a docker command. Every path here therefore either names bytes or refuses.
"""

import pytest
import yaml

from robovast.common.campaign_data import RoleImageUnavailable, campaign_role_image

SIM_DIGEST = "reg/sim@sha256:" + "b" * 64
SCENARIO_DIGEST = "reg/scenario@sha256:" + "a" * 64
LOCAL_DIGEST = "sha256:" + "d" * 64
SIM_TAG = "reg/sim:latest"


def _campaign(tmp_path, *, meta, vast_execution=None):
    """A campaign directory with the two files the resolver reads."""
    (tmp_path / "_execution").mkdir(parents=True, exist_ok=True)
    (tmp_path / "_execution" / "execution.yaml").write_text(yaml.safe_dump(meta))
    if vast_execution is not None:
        (tmp_path / "_config").mkdir(parents=True, exist_ok=True)
        (tmp_path / "_config" / "p.vast").write_text(
            yaml.safe_dump({"version": 2, "execution": vast_execution}))
    return tmp_path


# A campaign whose simulator has its own container -- the shape that broke.
SEPARATE = {"containers": {"scenario": {"image": "reg/scenario:latest"},
                           "simulation": {"image": SIM_TAG}}}
# A stepped simulator: the block exists but names no image or command, so it IS the
# scenario container and the campaign's single image is legitimately the simulator's.
FOLDED = {"containers": {"scenario": {"image": "reg/combined:latest"}, "simulation": {}}}


def test_a_recorded_per_role_digest_is_the_answer(tmp_path):
    c = _campaign(tmp_path, meta={"image_revision": SCENARIO_DIGEST,
                                  "image_revisions": {"simulation": SIM_DIGEST,
                                                      "scenario": SCENARIO_DIGEST}})
    assert campaign_role_image(c, "simulation") == SIM_DIGEST


def test_the_scenario_image_is_never_substituted_for_a_separate_simulator(tmp_path):
    """The bug, as a test: a campaign with its own simulation container and no per-role
    digest must refuse rather than hand back the scenario container's image."""
    c = _campaign(tmp_path, meta={"image_revision": SCENARIO_DIGEST}, vast_execution=SEPARATE)
    with pytest.raises(RoleImageUnavailable) as err:
        campaign_role_image(c, "simulation")
    # The message is what the run view shows, so it has to name the role and what it found.
    assert "'simulation'" in str(err.value) and SIM_TAG in str(err.value)
    assert SCENARIO_DIGEST not in str(err.value)


def test_a_folded_simulator_uses_the_campaign_image(tmp_path):
    """A stepped simulator IS the scenario container, so there the campaign-level digest is
    the right answer -- this is the one case the old unconditional fallback got right."""
    c = _campaign(tmp_path, meta={"image_revision": SCENARIO_DIGEST}, vast_execution=FOLDED)
    assert campaign_role_image(c, "simulation") == SCENARIO_DIGEST


def test_a_declared_tag_is_resolved_to_bytes(tmp_path):
    """What rescues campaigns recorded before their lane wrote per-role digests: the frozen
    .vast still names the image, and the lane turns that tag into bytes."""
    c = _campaign(tmp_path, meta={"image_revision": SCENARIO_DIGEST}, vast_execution=SEPARATE)
    seen = []

    def resolve(ref):
        seen.append(ref)
        return LOCAL_DIGEST

    assert campaign_role_image(c, "simulation", resolve_digest=resolve) == LOCAL_DIGEST
    assert seen == [SIM_TAG]  # the simulator's tag, not the campaign's


def test_the_recorded_images_map_is_preferred_over_the_snapshot(tmp_path):
    """``images`` is what the run recorded; the snapshot is only what was authored."""
    c = _campaign(tmp_path,
                  meta={"image_revision": SCENARIO_DIGEST, "images": {"simulation": "reg/ran:1"}},
                  vast_execution=SEPARATE)
    seen = []
    campaign_role_image(c, "simulation", resolve_digest=lambda r: (seen.append(r), LOCAL_DIGEST)[1])
    assert seen == ["reg/ran:1"]


def test_a_tag_that_cannot_be_resolved_is_refused(tmp_path):
    """A lane that cannot resolve (the cluster) must not fall back to something pullable-looking."""
    c = _campaign(tmp_path, meta={"image_revision": SCENARIO_DIGEST}, vast_execution=SEPARATE)
    with pytest.raises(RoleImageUnavailable):
        campaign_role_image(c, "simulation", resolve_digest=lambda _ref: None)


def test_a_recorded_non_digest_does_not_count(tmp_path):
    """``image_revisions`` carrying a tag is not an answer -- it cannot key a cache."""
    c = _campaign(tmp_path, meta={"image_revisions": {"simulation": "reg/sim:latest"}})
    with pytest.raises(RoleImageUnavailable) as err:
        campaign_role_image(c, "simulation")
    assert "does not name a digest" in str(err.value)


def test_a_bare_local_image_id_counts(tmp_path):
    """The local lane records ``docker inspect``'s bare id: immutable, just not a registry digest."""
    c = _campaign(tmp_path, meta={"image_revisions": {"simulation": LOCAL_DIGEST}})
    assert campaign_role_image(c, "simulation") == LOCAL_DIGEST


def test_a_campaign_without_execution_metadata_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        campaign_role_image(tmp_path, "simulation")


def test_without_a_readable_snapshot_the_campaign_image_is_still_used(tmp_path):
    """Refusal requires *positive* knowledge of a separate container.

    A campaign whose ``.vast`` cannot be read (or that declares no containers at all, so it
    has exactly one) offers no evidence that the simulator ran anywhere else, and withholding
    geometry from a shape we merely failed to inspect would break every single-container
    campaign. This is the rule the scene-cache and scene-route suites depend on.
    """
    c = _campaign(tmp_path, meta={"image_revision": SCENARIO_DIGEST})  # no _config/*.vast
    assert campaign_role_image(c, "simulation") == SCENARIO_DIGEST
