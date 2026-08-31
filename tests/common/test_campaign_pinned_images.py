# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``campaign_pinned_images`` — which images a campaign can be RE-RUN from.

A third resolver beside ``campaign_role_image`` and ``campaign_execution_image``, and the
distinction is the whole point: those two identify bytes for a cache key and pick an image to
postprocess in, where this one has to name something a *new run* can actually start. Bytes that
merely identify are not enough, so the local ``sha256:<id>`` the cache resolver happily accepts
is refused here — compose reads it as ``name:tag`` and goes looking for ``docker.io/library/sha256``.

Two traps these tests pin down:

- **the lanes disagree under the same keys.** On the cluster ``images`` is what the ``.vast``
  *declared* (for a ``build:`` project, the symbolic ref itself), while locally it is the
  plan-resolved built ref. Getting that backwards runs the base image without the campaign's own
  code — a campaign that finishes and measured nothing.
- **which containers to pin comes from the record, not the declaration.** ``images`` is written
  from the execution mapping *after* ``apply_backend``, so its keys are the containers that
  actually ran. Re-deriving that would need the campaign's plugins installed just to learn that a
  stepped simulator's ``simulation`` block is really the scenario container — and a real campaign
  in this repo (``tiago-stepped-parity``) declares exactly that, and records ``scenario``.
"""

import pytest
import yaml

from robovast.common.campaign_data import (CampaignImageUnpinnable, campaign_images,
                                           campaign_pinned_images)

CLUSTER_DIGEST = "harbor.example/robovast/exp@sha256:" + "9" * 64
SIM_DIGEST = "harbor.example/robovast/sim@sha256:" + "b" * 64
#: What ``docker inspect --format={{.Id}}`` prints. Identifies bytes, cannot be started
#: anywhere but the host that built them.
LOCAL_ID = "sha256:" + "d" * 64
#: What a ``build:<tag>`` project records as its declared image on the cluster lane.
SYMBOLIC = "build:roqsim-basic-nav-roqsim"


def _campaign(tmp_path, **meta):
    (tmp_path / "_execution").mkdir(parents=True, exist_ok=True)
    (tmp_path / "_execution" / "execution.yaml").write_text(yaml.safe_dump(meta))
    return tmp_path


# -- cluster lane ----------------------------------------------------------------


def test_a_single_container_cluster_campaign_pins_from_the_campaign_level_digest(tmp_path):
    """The real shape of most cluster campaigns: one container, recorded as ``scenario``."""
    c = _campaign(tmp_path, execution_type="cluster", images={"scenario": SYMBOLIC},
                  image_revision=CLUSTER_DIGEST)
    assert campaign_pinned_images(c) == {"scenario": CLUSTER_DIGEST}


def test_the_scenario_container_is_recorded_under_its_pod_name(tmp_path):
    """The cluster keys ``image_revisions`` by *pod* container name, where main is ``robovast``.

    Reading only ``image_revisions['scenario']`` would miss the pin entirely and refuse a
    campaign that recorded exactly what was needed.
    """
    c = _campaign(tmp_path, execution_type="cluster",
                  images={"scenario": SYMBOLIC, "simulation": "reg/sim:latest"},
                  image_revisions={"robovast": CLUSTER_DIGEST, "simulation": SIM_DIGEST})
    assert campaign_pinned_images(c) == {"scenario": CLUSTER_DIGEST,
                                        "simulation": SIM_DIGEST}


def test_an_infrastructure_sidecar_is_not_a_campaign_container(tmp_path):
    """``s3-init`` shows up in ``image_revisions`` but never in ``images``, which is why the
    container set is taken from the latter."""
    c = _campaign(tmp_path, execution_type="cluster", images={"scenario": SYMBOLIC},
                  image_revisions={"robovast": CLUSTER_DIGEST,
                                   "s3-init": "ghcr.io/x/sidecar@sha256:" + "e" * 64})
    assert campaign_pinned_images(c) == {"scenario": CLUSTER_DIGEST}


def test_a_declared_symbolic_build_ref_is_refused_not_pinned(tmp_path):
    """``images`` on the cluster is the declaration, not what ran.

    For a ``build:`` project it is the symbolic ref, which no runtime can start; for any other it
    is the *base* image, which would run without the campaign's code. Neither is a pin.
    """
    c = _campaign(tmp_path, execution_type="cluster", images={"scenario": SYMBOLIC})
    with pytest.raises(CampaignImageUnpinnable) as e:
        campaign_pinned_images(c)
    assert SYMBOLIC in str(e.value)


def test_a_container_that_owns_one_gets_no_campaign_level_substitute(tmp_path):
    """The substitution this resolver exists to prevent.

    The campaign recorded several containers, so a missing pin for one of them is a real gap —
    handing it the campaign-level image would silently run the scenario's bytes in its place.
    """
    c = _campaign(tmp_path, execution_type="cluster",
                  images={"scenario": SYMBOLIC, "simulation": "reg/sim:latest"},
                  image_revisions={"robovast": CLUSTER_DIGEST})
    with pytest.raises(CampaignImageUnpinnable) as e:
        campaign_pinned_images(c)
    assert "simulation" in str(e.value)


# -- local lane ------------------------------------------------------------------


def test_local_images_are_the_plan_resolved_built_refs_and_are_usable(tmp_path):
    c = _campaign(tmp_path, execution_type="local", image="built/c:1",
                  image_revision="unknown",
                  images={"simulation": "built/a:1", "sut": "built/b:1"},
                  image_revisions=None)
    assert campaign_pinned_images(c) == {"simulation": "built/a:1", "sut": "built/b:1"}


def test_a_bare_local_image_id_is_not_a_pin(tmp_path):
    """It names bytes, which is why the cache resolver takes it — but it cannot be started.

    Here it is the *only* thing recorded for the scenario container, and ``images`` has nothing,
    so there is no usable ref at all.
    """
    c = _campaign(tmp_path, execution_type="local", images={"scenario": ""},
                  image_revisions={"scenario": LOCAL_ID})
    assert campaign_pinned_images(c) == {}      # no recorded container to pin


def test_unknown_is_not_mistaken_for_a_pin(tmp_path):
    """``docker inspect`` failing writes the literal string ``unknown``."""
    c = _campaign(tmp_path, execution_type="cluster", images={"scenario": SYMBOLIC},
                  image_revision="unknown")
    with pytest.raises(CampaignImageUnpinnable):
        campaign_pinned_images(c)


# -- nothing recorded ------------------------------------------------------------


def test_a_campaign_that_recorded_no_containers_pins_nothing(tmp_path):
    """Not an error here: whether it matters depends on whether the ``.vast`` builds anything,
    which only the caller can read (see ``retrigger.prepare``)."""
    c = _campaign(tmp_path, execution_type="cluster")
    assert campaign_pinned_images(c) == {}


def test_a_campaign_with_no_execution_yaml_pins_nothing(tmp_path):
    """The usual shape of a failed cluster campaign — ``_config/`` frozen, the batch never
    finished. A bare ``FileNotFoundError`` from a reader would be the wrong answer."""
    assert campaign_pinned_images(tmp_path) == {}


def test_the_refusal_names_every_source_it_tried(tmp_path):
    """Shown to whoever clicked retrigger, so it has to distinguish "recorded nothing" from
    "recorded something unusable"."""
    c = _campaign(tmp_path, execution_type="local", images={"sut": ""},
                  image_revisions={"sut": LOCAL_ID})
    # Nothing recorded for `sut` that can be started, and it is not the scenario container.
    c = _campaign(tmp_path, execution_type="cluster", images={"sut": "reg/sut:latest"})
    with pytest.raises(CampaignImageUnpinnable) as e:
        campaign_pinned_images(c)
    msg = str(e.value)
    assert "image_revisions" in msg and "images" in msg and "execution_type" in msg
    assert "build context" in msg          # says why no retry would help


# -- campaign_images: the data, and what the launch record says about building ----


def _with_launch(tmp_path, launch: dict):
    """Give a campaign a ``launch.yaml``. Its ``images`` key is the 'did it build?' signal."""
    (tmp_path / "_execution").mkdir(parents=True, exist_ok=True)
    (tmp_path / "_execution" / "launch.yaml").write_text(yaml.safe_dump(launch))
    return tmp_path


def test_the_launch_record_distinguishes_built_nothing_from_no_record(tmp_path):
    """``built`` is False when the record answered, None when there is no record.

    This is the distinction the refusal turns on. ``launch.yaml`` is re-recorded with ``images``
    only once a build has produced something, so a record carrying none is positive evidence of
    a campaign that built nothing — as opposed to one too old to have a record, where the caller
    has to fall back to reading the configuration.
    """
    built_none = _with_launch(tmp_path / "a", {"campaign_name": "x", "runs": 0})
    _campaign(built_none, execution_type="cluster", images={"sut": "reg.example/x:latest"})
    assert campaign_images(built_none).built is False

    no_record = _campaign(tmp_path / "b", execution_type="cluster",
                          images={"sut": "reg.example/x:latest"})
    assert campaign_images(no_record).built is None


def test_unpinnable_names_the_container_and_keeps_the_diagnostic(tmp_path):
    """The caller needs the NAMES to apply a policy, and the text to explain a refusal."""
    c = _campaign(tmp_path, execution_type="cluster",
                  images={"simulation": "reg.example/sim:latest",
                          "sut": "reg.example/sut:latest"})
    images = campaign_images(c)
    assert images.pins == {}
    assert sorted(images.unpinnable) == ["simulation", "sut"]
    assert "images['sut']='reg.example/sut:latest'" in images.unpinnable["sut"]


def test_a_cluster_digest_in_images_is_a_pin(tmp_path):
    """Once the lane records what RAN rather than what was declared, ``images`` is startable.

    The lane check is about the *shape* of the ref, not the lane: a digest names the same bytes
    everywhere, which is the property a tag lacks.
    """
    c = _campaign(tmp_path, execution_type="cluster", images={"sut": SIM_DIGEST})
    assert campaign_pinned_images(c) == {"sut": SIM_DIGEST}


def test_a_cluster_tag_in_images_is_still_not_a_pin(tmp_path):
    """The half of the old rule that was right, and must not be lost with the other half."""
    c = _campaign(tmp_path, execution_type="cluster", images={"sut": "reg.example/sut:latest"})
    with pytest.raises(CampaignImageUnpinnable):
        campaign_pinned_images(c)
