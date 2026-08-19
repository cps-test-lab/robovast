# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Taking in a campaign somebody else produced, and saying what happened.

"Did the ingest work?" is not one bit. A campaign archive carries two schema ladders of its own
beyond the ``.vast``'s version, and each can independently be older, newer, absent or corrupt --
with different recoveries. Reporting only success/failure would hide which, and most of these are
recoverable.

The two properties worth defending: a *degraded* ingest is still usable and must not be thrown
away to keep a boolean clean, and every non-ok stage has to name what to do about it.
"""

import shutil
import sqlite3
from pathlib import Path

import pytest
import yaml

from robovast.service.ingest import (STAGE_ABSENT, STAGE_DEGRADED, STAGE_FAILED, STAGE_MIGRATED,
                                     STAGE_NEWER, STAGE_OK, STAGE_REBUILT, ingest_campaign)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "historic_campaigns"


@pytest.fixture(name="campaign")
def _campaign(tmp_path):
    """A copy of the version-1 historic fixture, safe to mutate."""
    source = _FIXTURES / "v1-campaign-2025-03-04-101500"
    target = tmp_path / source.name
    shutil.copytree(source, target)
    return target


def test_a_raw_archive_with_no_store_is_registered_not_rejected(campaign):
    """The normal case for a downloaded archive, and the reason build_campaign_store exists:
    a store is reconstructed by scanning the results tree. Rejecting this would reject most of
    what anyone actually receives."""
    report = ingest_campaign(campaign)
    assert report["ok"] is True
    assert report["stages"]["campaign_store"]["rebuilt"] is True
    assert (campaign / "campaign.db").exists()


def test_a_rebuilt_store_is_reported_distinctly_from_a_recorded_one(campaign):
    """A reconstructed store is derived from the results tree rather than written live by the
    controller. A reader comparing two campaigns should be able to see that difference -- it is a
    recovered fact, not a recorded one."""
    first = ingest_campaign(campaign)["stages"]["campaign_store"]
    assert first["rebuilt"] is True
    second = ingest_campaign(campaign)["stages"]["campaign_store"]
    assert second["rebuilt"] is False, "an existing store is not a rebuild"
    # And it rides alongside the health verdict rather than replacing it, so a store that is
    # both reconstructed and thin reports both facts instead of only whichever came first.
    assert second["verdict"] in (STAGE_OK, STAGE_DEGRADED)


def test_an_old_config_migrates_and_the_archive_is_untouched(campaign):
    vast_path = next((campaign / "_config").glob("*.vast"))
    before = vast_path.read_bytes()
    stage = ingest_campaign(campaign)["stages"]["config"]
    assert stage["verdict"] == STAGE_MIGRATED
    assert stage["steps"] == ["1_to_2"]
    assert "not modified" in stage["detail"]
    assert vast_path.read_bytes() == before


def test_a_config_from_a_newer_robovast_is_reported_not_migrated(campaign):
    """A format cannot be migrated backwards, so the only honest answer is which robovast is
    needed -- and it must not read as a corrupt file."""
    vast_path = next((campaign / "_config").glob("*.vast"))
    raw = yaml.safe_load(vast_path.read_text(encoding="utf-8"))
    raw["version"] = 99
    vast_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    stage = ingest_campaign(campaign)["stages"]["config"]
    assert stage["verdict"] == STAGE_NEWER
    assert "newer robovast" in stage["detail"]


def test_a_store_from_a_newer_robovast_says_what_would_be_lost(campaign):
    """CampaignStore deliberately reads a newer store best-effort rather than refusing. That is
    respected -- but silently omitting whatever the newer schema added is exactly the kind of
    quiet incompleteness this report exists to surface."""
    ingest_campaign(campaign)
    with sqlite3.connect(campaign / "campaign.db") as conn:
        conn.execute("PRAGMA user_version = 99")
    stage = ingest_campaign(campaign)["stages"]["campaign_store"]
    assert stage["verdict"] == STAGE_NEWER
    assert stage["schema_version"] == 99
    assert "upgrade robovast" in stage["detail"]
    assert "silently omit" in stage["detail"]


def test_a_corrupt_store_names_a_recovery_that_works(campaign):
    """A hint that is an action, not a diagnosis -- and the action is asserted to actually work,
    because a recovery nobody tested is a recovery nobody can rely on."""
    ingest_campaign(campaign)
    (campaign / "campaign.db").write_bytes(b"not a database at all")

    broken = ingest_campaign(campaign)["stages"]["campaign_store"]
    assert broken["verdict"] == STAGE_FAILED
    assert broken["recovery"] == "--rebuild-store"

    fixed = ingest_campaign(campaign, rebuild_store=True)["stages"]["campaign_store"]
    assert fixed["rebuilt"] is True
    assert fixed["verdict"] != STAGE_FAILED


def test_a_directory_without_a_frozen_config_is_refused(tmp_path):
    """"Not a campaign" and "a campaign with problems" need different answers. Registering a
    half-campaign would make every later reader fail on it instead of the import saying so once."""
    bare = tmp_path / "c-2026-01-01-000000"
    (bare / "_execution").mkdir(parents=True)
    report = ingest_campaign(bare)
    assert report["ok"] is False
    assert report["stages"]["layout"]["verdict"] == STAGE_FAILED
    assert "not re-runnable" in report["stages"]["layout"]["detail"] or \
        "frozen configuration" in report["stages"]["layout"]["detail"]


def test_a_missing_execution_record_degrades_rather_than_failing(tmp_path, campaign):
    """Provenance is what makes a campaign verifiable, not what makes it readable. Someone who
    has the data should still get the data -- flagged."""
    shutil.rmtree(campaign / "_execution")
    report = ingest_campaign(campaign)
    assert report["stages"]["layout"]["verdict"] == STAGE_DEGRADED
    assert report["ok"] is True, "degraded must not block: the campaign is still usable"


def test_a_store_indexing_no_runs_is_degraded_not_ok(campaign):
    """A campaign that lists and reports nothing is the shape of an archive stripped of its run
    directories. Passing that as `ok` would read as "checked, all fine"."""
    stage = ingest_campaign(campaign)["stages"]["campaign_store"]
    # The historic fixtures carry records but no run directories, which is exactly this case.
    assert stage["verdict"] == STAGE_DEGRADED
    assert stage["runs"] == 0
    assert "no runs" in stage["detail"]


def test_a_raw_archive_reports_the_analysis_db_as_recoverable(campaign):
    """Absent is expected for a pre-postprocess archive, so it names the command that produces
    it rather than reading as damage."""
    stage = ingest_campaign(campaign)["stages"]["analysis_db"]
    assert stage["verdict"] == STAGE_ABSENT
    assert "postprocess" in stage["detail"]


def test_every_stage_carries_an_actionable_detail(campaign):
    """A verdict a reader cannot act on is not worth returning."""
    report = ingest_campaign(campaign)
    assert set(report["stages"]) == {"layout", "config", "campaign_store", "analysis_db"}
    for name, stage in report["stages"].items():
        assert stage["detail"].strip(), f"{name} has no detail"
