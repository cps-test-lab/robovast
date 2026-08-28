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
