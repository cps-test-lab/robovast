# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Old campaigns stay readable and re-runnable — checked, not claimed.

Every guarantee built for archived campaigns (the migration ladder, the three read policies,
the container-protocol window, the retrigger pre-flight) degrades silently the moment nobody
exercises it against a genuinely old campaign. Nothing else in this suite does: the rest
construct configs at the *current* version, which is precisely the case that cannot regress.

So this runs against committed campaign directories frozen at the versions robovast has
actually shipped. See ``tests/fixtures/historic_campaigns/README.md`` for what each is and why
neither carries provenance records.

What is deliberately NOT asserted here is a real run: that needs Docker and a built image, so
it lives in the image workflow's integration test, which retriggers the campaign it just ran.
"""

import pathlib
import shutil

import pytest
import yaml

from robovast.common.common import load_config
from robovast.common.migrations import SUPPORTED_CONFIG_VERSION, config_version
from robovast.service import retrigger
from robovast.service.retrigger import AXIS_BLOCKED, AXIS_OK, AXIS_UNKNOWN, AXIS_UPGRADABLE

_FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "historic_campaigns"


def _campaigns():
    return sorted(d for d in _FIXTURES.iterdir() if d.is_dir())


class _Request:
    """Stand-in for CreateCampaignRequest, which ``prepare`` takes by injection."""

    _FIELDS = ("config_filter", "campaign_name", "runs", "postprocess", "upload_to_share",
               "show_gui", "description", "workspace_id", "config_path")

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        for field in self._FIELDS:
            self.__dict__.setdefault(field, None)


def test_there_are_fixtures_for_every_shipped_config_version():
    """A version with no fixture is a version nobody tests migrating from.

    Asserted rather than assumed, because the failure is invisible: adding a ladder step
    without a fixture leaves the suite passing while the new step never meets an old campaign.
    """
    versions = {config_version(yaml.safe_load(
        next(d.glob("_config/*.vast")).read_text(encoding="utf-8")))
        for d in _campaigns()}
    assert versions == set(range(1, SUPPORTED_CONFIG_VERSION + 1)), (
        f"fixtures cover config versions {sorted(versions)}, but robovast ships "
        f"1..{SUPPORTED_CONFIG_VERSION}. Add a campaign directory under {_FIXTURES.name}/.")


@pytest.mark.parametrize("campaign", _campaigns(), ids=lambda d: d.name)
def test_an_archived_campaign_can_be_read_and_is_not_rewritten(campaign):
    """The cheapest guarantee and the worst to lose: a published dataset must stay readable by
    the tool that produced it. Reading must also leave the archived file alone, because it is
    the record of what its author wrote."""
    vast_path = next(campaign.glob("_config/*.vast"))
    before = vast_path.read_bytes()

    config = load_config(str(vast_path), upgrade=True)
    assert config["version"] == SUPPORTED_CONFIG_VERSION
    # Whatever version it started at, the shape the rest of robovast reads must be present.
    assert config["execution"]["containers"], "migration produced no containers"
    assert vast_path.read_bytes() == before, "reading an archived config rewrote it"


@pytest.mark.parametrize("campaign", _campaigns(), ids=lambda d: d.name)
def test_the_strict_policy_still_refuses_an_old_config(campaign):
    """Leniency is scoped to *reading an archive*. Authoring or launching from an old file must
    still refuse, or the migration becomes an invisible rewrite of what someone asked to run."""
    vast_path = next(campaign.glob("_config/*.vast"))
    if config_version(yaml.safe_load(vast_path.read_text(encoding="utf-8"))) == \
            SUPPORTED_CONFIG_VERSION:
        pytest.skip("already current; nothing for the strict policy to refuse")
    with pytest.raises(ValueError, match="not the current version"):
        load_config(str(vast_path))


@pytest.mark.parametrize("campaign", _campaigns(), ids=lambda d: d.name)
def test_the_preflight_reports_every_axis_and_stays_runnable(campaign):
    """These campaigns predate plugins.yaml and providers.yaml, so those axes are `unknown` --
    and `unknown` must not block. Refusing a campaign for lacking a record nobody wrote would
    refuse exactly the campaigns this exists to rescue."""
    report = retrigger.check(campaign, campaign.name)

    assert set(report["axes"]) == {"config", "host", "images", "plugins", "providers"}
    assert all(axis["detail"] for axis in report["axes"].values()), "every axis needs a detail"

    assert report["axes"]["config"]["verdict"] in (AXIS_OK, AXIS_UPGRADABLE)
    assert report["axes"]["images"]["verdict"] == AXIS_OK, report["axes"]["images"]["detail"]
    for axis in ("plugins", "providers"):
        assert report["axes"][axis]["verdict"] == AXIS_UNKNOWN
    assert report["runnable"] is True, report["blocking"]


@pytest.mark.parametrize("campaign", _campaigns(), ids=lambda d: d.name)
def test_an_archived_campaign_can_be_prepared_for_relaunch(campaign, tmp_path):
    """The whole point, short of actually running it: staging succeeds, the staged config is at
    the current version, and the source is untouched."""
    source = tmp_path / campaign.name
    shutil.copytree(campaign, source)
    before = next(source.glob("_config/*.vast")).read_bytes()

    plan = retrigger.prepare(source, source.name, workspaces_root=tmp_path / "ws",
                             description_limit=200, request_model=_Request)
    try:
        staged = yaml.safe_load(pathlib.Path(plan.config_path).read_text(encoding="utf-8"))
        assert staged["version"] == SUPPORTED_CONFIG_VERSION
        assert plan.pinned_images, "the recorded image should be pinnable"
        assert next(source.glob("_config/*.vast")).read_bytes() == before
    finally:
        plan.discard()


def test_a_migrated_relaunch_keeps_the_authors_comments(tmp_path):
    """A migration is exactly when someone opens the staged config to work out what it does,
    so stripping the notes that explain it is worst at precisely that moment."""
    campaign = next(d for d in _campaigns() if d.name.startswith("v1-"))
    source = tmp_path / campaign.name
    shutil.copytree(campaign, source)

    plan = retrigger.prepare(source, source.name, workspaces_root=tmp_path / "ws",
                             description_limit=200, request_model=_Request)
    try:
        assert plan.config_migration["from"] == 1
        text = pathlib.Path(plan.config_path).read_text(encoding="utf-8")
        assert "A version-1 campaign, as they were actually written." in text
        assert f"version: {SUPPORTED_CONFIG_VERSION}" in text
    finally:
        plan.discard()


def test_a_recorded_pilot_stays_a_pilot(tmp_path):
    """launch.yaml exists so a re-run of a one-config pilot does not become the full sweep.
    Silently widening a re-run would burn a sweep's worth of compute on a question nobody asked.
    """
    campaign = next(d for d in _campaigns() if (d / "_execution" / "launch.yaml").exists())
    source = tmp_path / campaign.name
    shutil.copytree(campaign, source)
    recorded = yaml.safe_load((campaign / "_execution" / "launch.yaml").read_text(
        encoding="utf-8"))

    plan = retrigger.prepare(source, source.name, workspaces_root=tmp_path / "ws",
                             description_limit=200, request_model=_Request)
    try:
        assert plan.request.config_filter == recorded["config_filter"]
        assert plan.request.runs == recorded["runs"]
    finally:
        plan.discard()


def test_a_campaign_whose_image_vanished_is_blocked_with_a_recovery(tmp_path):
    """The variant-2 case. The image is gone, so it must say so *and* say what to do -- naming
    the record that holds what it was built from, rather than suggesting a newer image, which
    would silently run different code."""
    campaign = next(d for d in _campaigns() if d.name.startswith("v2-"))
    source = tmp_path / campaign.name
    shutil.copytree(campaign, source)
    execution = source / "_execution" / "execution.yaml"
    record = yaml.safe_load(execution.read_text(encoding="utf-8"))
    # A campaign that recorded no image at all: what a run that died before its first batch
    # leaves behind, and what an archive stripped of its execution record looks like.
    for key in ("image", "image_revision", "images", "image_revisions"):
        record.pop(key, None)
    execution.write_text(yaml.safe_dump(record), encoding="utf-8")

    report = retrigger.check(source, source.name)
    images = report["axes"]["images"]
    assert images["verdict"] in (AXIS_UNKNOWN, AXIS_BLOCKED)
    assert "image" in images["detail"].lower()
