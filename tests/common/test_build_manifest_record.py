# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The build lock has to outlive the image it was baked into.

Baking it into the image is right: it travels if the image is copied or retagged, and it is there
for anyone holding it. But it is *only* there, so the moment the image is deleted the lock goes
with it -- and that is precisely when "rebuild it and install the same versions" becomes the
question. A lock that exists exactly as long as the thing it describes answers nothing.

So the campaign keeps its own copy, and the pre-flight prefers it.
"""

import json
import types

import yaml

from robovast.common.campaign_data import read_build_manifests, write_build_manifests

_LOCK = {"apt": {"tree": "2.2.1-1"}, "pip": {"numpy": "1.12.1"},
         "vcs": {"pkg @ git+https://h/r@main": "a" * 40}}


def test_a_lock_round_trips_per_role(tmp_path):
    write_build_manifests(tmp_path, {"sut": _LOCK, "scenario": {"apt": {"curl": "8.5.0-2"}}})
    read_back = read_build_manifests(tmp_path)
    assert read_back["sut"] == _LOCK
    assert read_back["scenario"]["apt"]["curl"] == "8.5.0-2"


def test_an_empty_lock_writes_no_file(tmp_path):
    """An image that reported nothing is not the same as a role with an empty lock, and a file
    holding `{}` would read as the latter -- which a rebuild would act on."""
    write_build_manifests(tmp_path, {"sut": {}})
    assert not (tmp_path / "_execution" / "build_manifest").exists() or \
        list((tmp_path / "_execution" / "build_manifest").glob("*.json")) == []


def test_nothing_recorded_reads_as_unknown(tmp_path):
    """`{}` means unknown, and a caller must not treat it as "installed nothing" -- that would
    make a rebuild install an empty set rather than the author's intent."""
    assert read_build_manifests(tmp_path) == {}


def test_an_unparseable_record_is_skipped_not_raised(tmp_path):
    """Read on paths that must not fail over provenance. A record that cannot be parsed is not a
    record, and the other roles' locks are still worth having."""
    target = tmp_path / "_execution" / "build_manifest"
    target.mkdir(parents=True)
    (target / "sut.json").write_text("{not json", encoding="utf-8")
    (target / "scenario.json").write_text(json.dumps(_LOCK), encoding="utf-8")
    read_back = read_build_manifests(tmp_path)
    assert "sut" not in read_back
    assert read_back["scenario"] == _LOCK


def test_the_preflight_reads_the_lock_after_the_image_is_gone(tmp_path):
    """The whole point. The recorded image is gone, and the lock is still reported --
    which is what tells a caller a rebuild would install the same versions rather than
    re-resolving the author's loose specs into something else.
    """
    from robovast.service.retrigger import check

    campaign = tmp_path / "c-2026-01-01-000000"
    (campaign / "_config").mkdir(parents=True)
    (campaign / "_config" / "campaign.vast").write_text(yaml.safe_dump({
        "version": 3, "metadata": {"name": "p"},
        "execution": {"containers": {"sut": {"image": "family:robovast"}},
                      "scenario_file": "s.osc", "runs": 1}}), encoding="utf-8")
    (campaign / "_execution").mkdir()
    (campaign / "_execution" / "execution.yaml").write_text(yaml.safe_dump({
        "execution_type": "local",
        "images": {"sut": "an-image-that-does-not-exist:zzz"},
        "image_revisions": {"sut": "an-image-that-does-not-exist:zzz"}}), encoding="utf-8")
    write_build_manifests(campaign, {"sut": _LOCK})

    axis = check(campaign, campaign.name)["axes"]["images"]
    assert axis["locks"] == {"sut": {"apt": 1, "pip": 1, "vcs": 1}}
    assert "build lock" in axis["detail"]


def test_the_controller_persists_it_while_the_images_are_still_here(tmp_path, monkeypatch):
    """Copied at the end of the run, from the one point that runs in Python on both lanes with the
    campaign root and the resolved images both in hand -- the local lane writes execution.yaml from
    a generated shell script, so nothing earlier knows the directory."""
    from robovast.execution import controller as controller_module

    monkeypatch.setattr("robovast.service.image_build.read_image_build_manifest",
                        lambda image: _LOCK if image == "img:1" else {})

    campaign = tmp_path / "c-2026-01-01-000000"
    (campaign / "_execution").mkdir(parents=True)
    instance = controller_module.CampaignController.__new__(controller_module.CampaignController)
    instance.campaign_root = str(campaign)
    instance._persist_build_manifests(  # pylint: disable=protected-access
        {"images": {"sut": "img:1", "scenario": "img:none"}})

    read_back = read_build_manifests(campaign)
    assert read_back == {"sut": _LOCK}, "a role whose image reported no lock writes no file"


def test_persisting_never_fails_the_campaign(tmp_path, monkeypatch):
    """Bookkeeping must not turn a finished campaign into a failed one -- the runs already
    happened, and their data is what matters."""
    from robovast.execution import controller as controller_module

    def boom(_image):
        raise RuntimeError("docker exploded")

    monkeypatch.setattr("robovast.service.image_build.read_image_build_manifest", boom)
    instance = controller_module.CampaignController.__new__(controller_module.CampaignController)
    instance.campaign_root = str(tmp_path)
    instance._persist_build_manifests({"images": {"sut": "img:1"}})  # pylint: disable=protected-access


def test_the_lock_is_read_from_the_image_that_ran_not_the_one_declared(tmp_path,
                                                                      monkeypatch):
    """``images`` is not the right ref to ask on every lane.

    On the cluster lane it records what the ``.vast`` declared, and for a container robovast
    builds that is its **base**. The base's lock describes different software than the image
    the campaign actually ran -- it is missing exactly the packages the campaign added, which
    are the ones a re-run has to pin. ``image_revisions`` is the digest of what ran.
    """
    from robovast.execution.controller import CampaignController

    asked = []

    def _local(image):
        asked.append(image)
        return {"apt": {"tree": "2.2.1-1"}} if "@sha256:" in image else {}

    monkeypatch.setattr("robovast.service.image_build.read_image_build_manifest", _local)

    controller = CampaignController.__new__(CampaignController)
    controller.campaign_root = str(tmp_path)
    controller.backend = types.SimpleNamespace(read_build_lock=lambda _image: {})

    controller._persist_build_manifests({
        "images": {"sut": "reg.example.com/robovast:latest"},
        "image_revisions": {"sut": "reg.example.com/sut@sha256:" + "a" * 64},
    })

    assert asked == ["reg.example.com/sut@sha256:" + "a" * 64]
    assert read_build_manifests(tmp_path)["sut"]["apt"]["tree"] == "2.2.1-1"


def test_the_lane_is_asked_when_no_local_image_can_answer(tmp_path, monkeypatch):
    """The cluster case: the controller pod has no runtime, so the local read returns nothing
    for an image that certainly has a lock. Before the lane was asked, that silence was taken
    for "no lock" and the campaign kept no copy at all."""
    from robovast.execution.controller import CampaignController

    monkeypatch.setattr("robovast.service.image_build.read_image_build_manifest",
                        lambda _image: {})

    controller = CampaignController.__new__(CampaignController)
    controller.campaign_root = str(tmp_path)
    controller.backend = types.SimpleNamespace(
        read_build_lock=lambda _image: {"apt": {"curl": "8.5.0-2"}})

    controller._persist_build_manifests({"images": {"sut": "reg.example.com/sut:t"}})

    assert read_build_manifests(tmp_path)["sut"]["apt"]["curl"] == "8.5.0-2"


def test_a_lane_that_cannot_read_one_writes_nothing(tmp_path, monkeypatch):
    """No lock anywhere is a fact worth leaving absent, not an empty file that reads as
    "installed nothing"."""
    from robovast.execution.controller import CampaignController

    monkeypatch.setattr("robovast.service.image_build.read_image_build_manifest",
                        lambda _image: {})

    controller = CampaignController.__new__(CampaignController)
    controller.campaign_root = str(tmp_path)
    controller.backend = types.SimpleNamespace(read_build_lock=lambda _image: {})

    controller._persist_build_manifests({"images": {"sut": "reg.example.com/sut:t"}})

    assert read_build_manifests(tmp_path) == {}


def test_every_backend_answers_the_lock_hook():
    """A default on the ABC, so a lane needing nothing inherits the right behaviour."""
    from robovast.execution.backends import ExecutionBackend

    assert ExecutionBackend.read_build_lock(types.SimpleNamespace(), "img") == {}


def test_a_missing_lock_for_an_image_we_built_is_reported(tmp_path, monkeypatch, caplog):
    """A silent absence is what let this go unnoticed: the campaign looks complete either
    way, and the cost surfaces only once the image is gone."""
    from robovast.execution.controller import CampaignController

    monkeypatch.setattr("robovast.service.image_build.read_image_build_manifest",
                        lambda _image: {})

    controller = CampaignController.__new__(CampaignController)
    controller.campaign_root = str(tmp_path)
    controller.backend = types.SimpleNamespace(read_build_lock=lambda _image: {})

    with caplog.at_level("WARNING"):
        controller._persist_build_manifests({
            "images": {"sut": "reg.example.com/sut:t", "vendor": "vendor.example.com/x:1"},
            # `declared` marks an image the author described and robovast did not build --
            # there was never a lock of ours in it, so it must not be reported as missing one.
            "image_build_refs": {"sut": {"revision": "abc"},
                                 "vendor": {"source": "https://v.example.com", "declared": True}},
        })

    assert "No build lock recorded for sut" in caplog.text
    assert "vendor" not in caplog.text


def test_a_role_the_campaign_does_not_name_is_still_asked(tmp_path, monkeypatch):
    """``images`` holds only the containers whose image the ``.vast`` names, so a container
    robovast supplies is absent from it. Iterating that map alone never put the question for
    such a role -- and the report, which measures against the images we BUILT, then listed it
    as missing a lock. Two different sets, so a question never asked read as a failed read.
    """
    from robovast.execution.controller import CampaignController

    asked = []

    def _lock(image):
        asked.append(image)
        return {"apt": {"tree": "2.2.1-1"}}

    monkeypatch.setattr("robovast.service.image_build.read_image_build_manifest",
                        lambda _image: {})

    controller = CampaignController.__new__(CampaignController)
    controller.campaign_root = str(tmp_path)
    controller.backend = types.SimpleNamespace(read_build_lock=_lock)

    controller._persist_build_manifests({
        # `scenario` is in neither `images` nor a declaration -- robovast supplies it.
        "images": {"sut": "reg.example.com/robovast:latest"},
        "image_revisions": {"sut": "reg.example.com/sut@sha256:" + "a" * 64,
                            "scenario": "reg.example.com/robovast@sha256:" + "b" * 64},
        "image_build_refs": {"sut": {"revision": "abc"}, "scenario": {"revision": "abc"}},
    })

    assert sorted(asked) == ["reg.example.com/robovast@sha256:" + "b" * 64,
                             "reg.example.com/sut@sha256:" + "a" * 64]
    assert set(read_build_manifests(tmp_path)) == {"scenario", "sut"}


def test_nothing_is_reported_missing_once_every_role_answers(tmp_path, monkeypatch, caplog):
    """The report and the read now share one set, so a lock that was found cannot also be
    announced as absent."""
    from robovast.execution.controller import CampaignController

    monkeypatch.setattr("robovast.service.image_build.read_image_build_manifest",
                        lambda _image: {"apt": {"tree": "1.0"}})

    controller = CampaignController.__new__(CampaignController)
    controller.campaign_root = str(tmp_path)
    controller.backend = types.SimpleNamespace(read_build_lock=lambda _i: {})

    with caplog.at_level("WARNING"):
        controller._persist_build_manifests({
            "images": {"sut": "reg.example.com/sut:t"},
            "image_build_refs": {"sut": {"revision": "abc"},
                                 "scenario": {"revision": "abc"}},
            "image_revisions": {"scenario": "reg.example.com/robovast@sha256:" + "b" * 64},
        })

    assert "No build lock recorded" not in caplog.text
