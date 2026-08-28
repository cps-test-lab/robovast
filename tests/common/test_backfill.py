# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Enriching an already-recorded campaign, without corrupting what it recorded.

The temptation with backfill is to make old campaigns *look* complete. That is the one thing it
must not do: a campaign's own record is evidence, and a reader who cannot tell which values the
campaign reported from which were inferred a year later can cite none of them. So the
tests below are mostly about restraint -- additive writes, unknowns kept as unknowns with their
reason, and a local image id refused rather than promoted to something that looks pullable.
"""

import pathlib

import yaml

from robovast.common.backfill import (BACKFILL_KEY, BACKFILL_VERSION, apply_backfill,
                                      plan_backfill)


def _campaign(tmp_path: pathlib.Path, execution: dict | None,
              name: str = "c-2026-01-01-000000") -> pathlib.Path:
    root = tmp_path / name
    root.mkdir(parents=True)
    if execution is not None:
        (root / "_execution").mkdir()
        (root / "_execution" / "execution.yaml").write_text(yaml.safe_dump(execution),
                                                            encoding="utf-8")
    return root


def _read(campaign: pathlib.Path) -> dict:
    return yaml.safe_load((campaign / "_execution" / "execution.yaml").read_text(
        encoding="utf-8"))


def test_a_planned_backfill_writes_nothing(tmp_path):
    """It runs over published data, so the change has to be inspectable before it happens."""
    campaign = _campaign(tmp_path, {"robovast_version": "2.0.0"})
    before = (campaign / "_execution" / "execution.yaml").read_bytes()
    plan_backfill(campaign)
    assert (campaign / "_execution" / "execution.yaml").read_bytes() == before


def test_recorded_values_are_never_overwritten(tmp_path):
    """The core rule. Replacing part of a campaign's record with something inferred later makes
    the rest unciteable, because nothing distinguishes the two."""
    recorded = {"robovast_version": "2.0.0", "execution_type": "local",
                "image": "ghcr.io/x/y:1", "runs": 5,
                "image_revisions": {"scenario": "ghcr.io/x/y@sha256:" + "a" * 64}}
    campaign = _campaign(tmp_path, dict(recorded))
    apply_backfill(campaign)
    after = _read(campaign)
    for key, value in recorded.items():
        assert after[key] == value, f"backfill changed the recorded {key}"
    assert BACKFILL_KEY in after


def test_a_package_version_is_not_mistaken_for_a_revision(tmp_path):
    """`robovast_version` is not reliably a revision at all -- it falls back to the installed
    semver when the git lookup failed. Resolving `2.0.0` as though it were a commit is exactly
    the confusion this field caused before, so it must be reported as underivable."""
    campaign = _campaign(tmp_path, {"robovast_version": "2.0.0"})
    revision = plan_backfill(campaign)["derived"]["robovast_revision"]
    assert revision["value"] is None
    assert "package version" in revision["unknown"]


def test_a_full_sha_is_kept_as_is(tmp_path):
    campaign = _campaign(tmp_path, {"robovast_version": "b" * 40})
    revision = plan_backfill(campaign)["derived"]["robovast_revision"]
    assert revision["value"] == "b" * 40
    assert "already" in revision["source"]


def test_a_dirty_marker_does_not_defeat_resolution(tmp_path):
    """`code_revision` folded +dirty into the same string, so a recorded revision may carry it.
    The suffix says the tree was dirty; it does not stop the sha being a sha."""
    campaign = _campaign(tmp_path, {"robovast_version": "c" * 40 + "+dirty"})
    assert plan_backfill(campaign)["derived"]["robovast_revision"]["value"] == "c" * 40


def test_an_unreachable_short_revision_is_unknown_not_guessed(tmp_path):
    """A campaign recorded elsewhere may name a commit this clone has never had. Reporting that
    honestly is the difference between "cannot resolve" and a wrong 40 characters."""
    campaign = _campaign(tmp_path, {"robovast_version": "deadbee"})
    revision = plan_backfill(campaign)["derived"]["robovast_revision"]
    assert revision["value"] is None
    assert "not reachable" in revision["unknown"]


def test_a_local_image_id_is_refused_rather_than_promoted(tmp_path):
    """The important image case. `docker inspect .Id` is what the local lane recorded, and it
    cannot be pulled anywhere -- compose reads `sha256:<hex>` as `name:tag` and goes looking for
    docker.io/library/sha256. Presenting it as a digest would produce a re-run that fails at
    pull time for reasons nothing explains."""
    campaign = _campaign(tmp_path, {
        "robovast_version": "2.0.0",
        "images": {"scenario": "ghcr.io/x/y:1"},
        "image_revisions": {"scenario": "sha256:" + "d" * 64}})
    entry = plan_backfill(campaign)["derived"]["images"]["per_role"]["scenario"]
    assert entry["value"] is None
    assert "cannot be pulled" in entry["unknown"]


def test_a_registry_digest_is_recognised(tmp_path):
    campaign = _campaign(tmp_path, {
        "robovast_version": "2.0.0",
        "image_revisions": {"sut": "ghcr.io/x/y@sha256:" + "e" * 64}})
    entry = plan_backfill(campaign)["derived"]["images"]["per_role"]["sut"]
    assert entry["value"].endswith("e" * 64)


def test_backfill_is_idempotent(tmp_path):
    """It runs over published data; a second pass must change nothing."""
    campaign = _campaign(tmp_path, {"robovast_version": "2.0.0"})
    apply_backfill(campaign)
    first = (campaign / "_execution" / "execution.yaml").read_bytes()
    second_plan = apply_backfill(campaign)
    assert second_plan["already_present"] is True
    assert "written" not in second_plan
    assert (campaign / "_execution" / "execution.yaml").read_bytes() == first


def test_force_re_derives_an_existing_block(tmp_path):
    """What a bumped BACKFILL_VERSION needs: without it an old block would be permanent, and
    "already backfilled" would mean "by some earlier version, contents unknown"."""
    campaign = _campaign(tmp_path, {"robovast_version": "2.0.0"})
    apply_backfill(campaign)
    _read(campaign)  # sanity: parses
    plan = apply_backfill(campaign, force=True)
    assert plan.get("written") is True
    assert _read(campaign)[BACKFILL_KEY]["backfill_version"] == BACKFILL_VERSION


def test_a_campaign_with_no_execution_record_is_reported_not_created(tmp_path):
    """A campaign that died before writing execution.yaml has nothing to enrich. Creating the
    file would invent a record for a run nobody has evidence of."""
    campaign = _campaign(tmp_path, None)
    plan = plan_backfill(campaign)
    assert "unavailable" in plan
    assert not (campaign / "_execution").exists()
    apply_backfill(campaign)
    assert not (campaign / "_execution").exists()


def test_the_rewrite_leaves_no_temporary_file_behind(tmp_path):
    """Written beside the original and moved into place, because a crash midway through
    rewriting published data would otherwise leave a campaign with no execution record."""
    campaign = _campaign(tmp_path, {"robovast_version": "2.0.0"})
    apply_backfill(campaign)
    leftovers = [p.name for p in (campaign / "_execution").iterdir()
                 if "tmp" in p.name]
    assert leftovers == []
