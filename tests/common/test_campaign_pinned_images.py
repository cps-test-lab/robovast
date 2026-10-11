# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``campaign_pinned_images`` — the digests a replay of a campaign runs.

A retrigger and an adoption after a service restart both replay a campaign, and neither
resolves an image again: whatever ``ROBOVAST_PROJECT``, the family's floating tags or the
composition cache say by then, the replay runs the bytes its source ran. So the launch record
has to fix a digest for everything the campaign runs -- every container, the sidecar and every
aux helper -- and a record that does not cannot be replayed at all.

Two traps these tests pin down:

- **a tag is never a pin.** It names whatever was pushed there last. So is ``execution.yaml``'s
  record not a substitute for the launch record: a record that has to be pieced together from
  two files is one written before the rule, and may be missing an image nobody knows about.
- **which containers must be pinned comes from the records, not the declaration.**
  ``execution.yaml``'s ``images`` is written after ``apply_backend``, so its keys are the
  containers that actually ran, and a container it names that the launch record lacks is a gap.
"""

import pytest
import yaml

from robovast.common.campaign_data import (CampaignImageUnpinnable, LaunchImages,
                                           campaign_images, campaign_pinned_images)

SCENARIO_DIGEST = "harbor.example/robovast/exp@sha256:" + "9" * 64
SIM_DIGEST = "harbor.example/robovast/sim@sha256:" + "b" * 64
SIDECAR_DIGEST = "harbor.example/robovast/robovast-sidecar@sha256:" + "c" * 64
AUX_DIGEST = "harbor.example/robovast/robovast-roqsim@sha256:" + "d" * 64


def _campaign(tmp_path, *, launch=None, **execution):
    """A campaign directory with the records given; ``None`` leaves a record out."""
    (tmp_path / "_execution").mkdir(parents=True, exist_ok=True)
    if execution:
        (tmp_path / "_execution" / "execution.yaml").write_text(yaml.safe_dump(execution))
    if launch is not None:
        (tmp_path / "_execution" / "launch.yaml").write_text(yaml.safe_dump(
            {"config_filter": "", "campaign_name": None, "runs": 0, **launch}))
    return tmp_path


def _complete(**overrides):
    """A launch record as a launch writes it now: every image a digest."""
    return {"images": {"scenario": SCENARIO_DIGEST, "simulation": SIM_DIGEST},
            "sidecar_image": SIDECAR_DIGEST,
            "aux_images": {"aux-robovast-roqsim": AUX_DIGEST}, **overrides}


# -- a complete record -----------------------------------------------------------


def test_a_complete_record_is_replayed_as_recorded(tmp_path):
    c = _campaign(tmp_path, launch=_complete(),
                  execution_type="cluster", images={"scenario": "build:x",
                                                    "simulation": "reg.example/sim:latest"})
    assert campaign_pinned_images(c) == LaunchImages(
        containers={"scenario": SCENARIO_DIGEST, "simulation": SIM_DIGEST},
        sidecar=SIDECAR_DIGEST, aux={"aux-robovast-roqsim": AUX_DIGEST})


def test_a_campaign_that_died_before_its_first_batch_is_replayable(tmp_path):
    """The launch record is written before the first job, ``execution.yaml`` with it -- so a
    campaign cut short in its first batch is replayable from the one record it has."""
    c = _campaign(tmp_path, launch=_complete())
    assert campaign_pinned_images(c).containers["scenario"] == SCENARIO_DIGEST


def test_a_campaign_that_ran_no_aux_helper_needs_none(tmp_path):
    """Composition asked for no auxiliary container, so there is none to record."""
    c = _campaign(tmp_path, launch=_complete(aux_images={}))
    assert campaign_pinned_images(c).aux == {}


def test_data_plane_containers_in_image_revisions_are_not_campaign_containers(tmp_path):
    """A pod read reports ``fetch-inputs``, ``uploader`` and the scenario's pod name
    ``robovast`` in ``image_revisions``; the sidecar entry covers the first two, and none of
    them is a container the launch record keys by name."""
    c = _campaign(tmp_path, launch=_complete(), execution_type="cluster",
                  images={"scenario": "build:x", "simulation": "reg.example/sim:latest"},
                  image_revisions={"robovast": SCENARIO_DIGEST, "simulation": SIM_DIGEST,
                                   "fetch-inputs": SIDECAR_DIGEST})
    assert campaign_images(c).missing == {}


# -- what cannot be replayed -------------------------------------------------------


def test_a_campaign_without_a_launch_record_cannot_be_replayed(tmp_path):
    """Not even with every digest in ``execution.yaml``: that record never held the sidecar or
    the aux helpers, so it cannot say everything the campaign ran."""
    c = _campaign(tmp_path, execution_type="cluster", images={"scenario": "build:x"},
                  image_revisions={"scenario": SCENARIO_DIGEST})
    with pytest.raises(CampaignImageUnpinnable) as e:
        campaign_pinned_images(c)
    assert "launch.yaml" in str(e.value)


def test_a_record_from_before_every_image_was_recorded_names_what_it_lacks(tmp_path):
    """The shape of a campaign launched before the record held every image: container
    digests, and no sidecar."""
    c = _campaign(tmp_path, launch={"images": {"scenario": SCENARIO_DIGEST}})
    with pytest.raises(CampaignImageUnpinnable) as e:
        campaign_pinned_images(c)
    assert "the sidecar image" in str(e.value)
    assert "sidecar_image=None" in str(e.value)


def test_a_tag_in_the_record_is_not_a_pin(tmp_path):
    """A tag names whatever was pushed there last, however it reached the record."""
    c = _campaign(tmp_path, launch=_complete(images={"scenario": SCENARIO_DIGEST,
                                                     "sut": "reg.example/sut:latest"}))
    with pytest.raises(CampaignImageUnpinnable) as e:
        campaign_pinned_images(c)
    assert "container 'sut'" in str(e.value)
    assert "reg.example/sut:latest" in str(e.value)


def test_a_container_that_ran_but_is_not_in_the_record_is_a_gap(tmp_path):
    """``execution.yaml`` says a ``sut`` ran; the launch record fixes no digest for it -- the
    shape of a record that held only the containers a campaign built."""
    c = _campaign(tmp_path, launch=_complete(),
                  execution_type="cluster",
                  images={"scenario": "build:x", "simulation": "reg.example/sim:latest",
                          "sut": "reg.example/sut:latest"})
    with pytest.raises(CampaignImageUnpinnable) as e:
        campaign_pinned_images(c)
    assert "container 'sut'" in str(e.value)


def test_a_record_with_no_container_images_is_a_gap(tmp_path):
    """A campaign that died while composing fixed its sidecar and nothing else."""
    c = _campaign(tmp_path, launch={"sidecar_image": SIDECAR_DIGEST})
    with pytest.raises(CampaignImageUnpinnable) as e:
        campaign_pinned_images(c)
    assert "every container" in str(e.value)


def test_an_aux_tag_is_a_gap(tmp_path):
    c = _campaign(tmp_path, launch=_complete(aux_images={"aux-tool": "reg.example/tool:1"}))
    with pytest.raises(CampaignImageUnpinnable) as e:
        campaign_pinned_images(c)
    assert "auxiliary container 'aux-tool'" in str(e.value)


def test_every_gap_is_named_at_once(tmp_path):
    """Shown to whoever asked for the re-run, so it names all of them rather than one per
    attempt, each with what the record held."""
    c = _campaign(tmp_path, launch={"images": {"scenario": "reg.example/exp:latest"},
                                    "aux_images": {"aux-tool": "reg.example/tool:1"}})
    images = campaign_images(c)
    assert sorted(images.missing) == ["auxiliary container 'aux-tool'",
                                      "container 'scenario'", "the sidecar image"]
    assert "images['scenario']='reg.example/exp:latest'" in images.missing["container 'scenario'"]
    # The digests it does hold are still reported, for a caller that shows them.
    assert images.pins.containers == {}
