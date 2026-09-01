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

import os
import shutil
import sqlite3
from pathlib import Path

import pytest
import yaml

from robovast.common.store import _MIGRATIONS, SCHEMA_VERSION
from robovast.service.ingest import (STAGE_ABSENT, STAGE_DEGRADED, STAGE_FAILED, STAGE_MIGRATED,
                                     STAGE_NEWER, STAGE_OK, blocking_summary, ingest_campaign,
                                     missing_for_import, missing_for_import_in)

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
    assert stage["steps"] == ["1_to_2", "2_to_3"]
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


def test_a_store_from_an_older_robovast_migrates_without_being_asked(campaign):
    """An archived store must come up the schema ladder on import, with no flag.

    Found live, importing a real July-2026 campaign off the share: it failed on
    ``no such table: run``. Three things had to line up. The archived ``campaign.db`` is
    schema v1, from before that table existed; the ladder runs on a read-*write* open and
    every check here is read-only; and ``build_campaign_store`` will not rebuild it either,
    because its freshness shortcut compares mtimes and tar preserves them -- so a store
    archived beside its own tree always looks up to date.

    The population this blocked is exactly the one that most needs importing: campaigns old
    enough to predate the current schema. ``--rebuild-store`` is for a *corrupt* store, not
    for a merely old one, and requiring it here would have made the recovery a thing you had
    to already know.
    """
    # A genuine v1 store, built from the ladder's own first step rather than by mutating a
    # current one -- a hand-faked v1 is not v1, and the ladder rightly refuses it
    # ("duplicate column name"). _MIGRATIONS is append-only and indexed by the version it
    # upgrades *from*, so entry 0 is exactly what v1 was.
    store = campaign / "campaign.db"
    store.unlink(missing_ok=True)
    with sqlite3.connect(store) as conn:
        conn.executescript(_MIGRATIONS[0])
        conn.execute("PRAGMA user_version = 1")
    # Archived mtimes: the store looks no older than the tree it came with, which is what
    # sends build_campaign_store down its "already up to date" path.
    for path in campaign.rglob("*"):
        os.utime(path, (1_700_000_000, 1_700_000_000))
    os.utime(store, (1_700_000_100, 1_700_000_100))

    stage = ingest_campaign(campaign)["stages"]["campaign_store"]

    assert stage["verdict"] != STAGE_FAILED, stage["detail"]
    assert stage["schema_version"] == SCHEMA_VERSION
    # Provenance rides alongside the health verdict rather than replacing it -- the same
    # rule `rebuilt` follows, so a migrated store that still indexes nothing says both.
    assert stage["version"] == 1
    assert "migrated from schema v1" in stage["detail"]
    # Migrated in place, not rebuilt: the rows the controller recorded live are kept.
    assert stage["rebuilt"] is False
    with sqlite3.connect(store) as conn:
        conn.execute("SELECT count(*) FROM run").fetchone()


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
    assert set(report["stages"]) == {"layout", "config", "campaign_store", "index",
                                     "analysis_db"}
    for name, stage in report["stages"].items():
        assert stage["detail"].strip(), f"{name} has no detail"


def test_a_refusal_reports_the_reason_and_not_only_which_check_failed(tmp_path):
    """The stage names are an index, not a diagnosis.

    ``config, layout`` is what *every* incomplete archive says -- the same three words
    whether the ``.vast`` is missing, unparseable, or from a robovast that does not exist
    yet. The sentence that distinguishes them is already written by each stage, and until
    ``blocking_summary`` existed it reached only ``import.log``/``import.json``, which live
    inside the campaign and are therefore unreadable on a lane that publishes a campaign
    only once its import succeeded. The one message guaranteed to be seen carried the one
    form of the answer that says nothing.
    """
    bare = tmp_path / "c-2026-01-01-000000"
    (bare / "_execution").mkdir(parents=True)
    summary = blocking_summary(ingest_campaign(bare))
    assert "layout:" in summary and "config:" in summary, "each blocking stage is named"
    assert "_config/" in summary, "and says what is actually missing"


def test_the_export_refuses_what_the_import_would_refuse(campaign):
    """One definition of "is this a campaign", asked on the way out as well as in.

    An archive with no frozen config uploads, lists and downloads exactly like a good one
    and fails only at the far end of a transfer, on somebody else's service, where nobody
    can repair the source. The predicate that refuses it there has to be the same one, or
    the two drift and the export starts writing archives the import has learned to reject.
    """
    assert missing_for_import_in(campaign) == [], "the fixture is a complete campaign"
    assert ingest_campaign(campaign)["ok"] is True

    shutil.rmtree(campaign / "_config")
    assert missing_for_import_in(campaign), "and this is the shape the import refuses"
    assert ingest_campaign(campaign)["ok"] is False


def test_the_export_check_reads_object_keys_as_readily_as_a_tree():
    """Paths, not a directory: the cluster lane exports from an object store.

    It has no tree to stat, so a predicate written against the filesystem would simply not
    be asked there -- which is the lane where the campaign that produced the bad archive
    lives. Both callers hand over campaign-relative paths and get the same answer.
    """
    assert missing_for_import(["_execution/controller.log", "config1/1/test.xml"]), \
        "no _config/ at all is the shape a campaign that died before setup exports as"
    assert missing_for_import(["_config/", "_config/scenario.osc"]), \
        "a _config/ carrying no .vast is refused for the .vast, not for the directory"
    assert missing_for_import(["_config/nav.vast", "_execution/data.db"]) == []
    # Absence of derived data is not incompleteness: raw is the normal thing to share.
    assert missing_for_import(["_config/nav.vast"]) == []


# -- the index stage: importing IS ingesting ---------------------------------

DSN = os.environ.get("ROBOVAST_TEST_PG_DSN")
pg = pytest.mark.skipif(not DSN, reason="ROBOVAST_TEST_PG_DSN is not set")


def test_an_unreachable_index_degrades_the_import_rather_than_failing_it(
        campaign, monkeypatch):
    """The campaign imported fine and its files are intact; only the queryable copy is not.

    Discarding a campaign somebody already has, to keep a boolean clean, is the trade this
    module refuses everywhere else. But it must not be SILENT either -- a campaign that
    lists and opens and answers nothing, with no stage saying why, is exactly what this
    stage exists to make visible.
    """
    from robovast.common.errors import IndexUnreachableError

    monkeypatch.setattr("robovast.common.index_db.connect",
                        lambda *a, **k: (_ for _ in ()).throw(
                            IndexUnreachableError("ROBOVAST_INDEX_DSN is not set")))

    report = ingest_campaign(campaign)

    assert report["ok"] is True, "an unreachable index must not discard the campaign"
    stage = report["stages"]["index"]
    assert stage["verdict"] == STAGE_DEGRADED
    assert "not queryable" in stage["detail"]
    assert "ROBOVAST_INDEX_DSN" in stage["detail"], "it must name what is wrong"


@pg
def test_importing_a_campaign_loads_its_rows_without_running_postprocessing(
        campaign, monkeypatch):
    """An imported campaign must be queryable, and the archive already holds what it takes.

    Before this, the import only REPORTED that the rows were absent and pointed at
    postprocessing -- which re-runs the plugin pipeline against the campaign's own image to
    regenerate derived files the archive already carries. Ingestion is not a command a user
    issues; it happens wherever results arrive, and an import is one of those places.
    """
    import psycopg

    from robovast.results_processing import index_schema

    schema = "import_index_test"
    with psycopg.connect(DSN, autocommit=True) as setup:
        for stmt in (f"DROP SCHEMA IF EXISTS {schema} CASCADE",
                     f"DROP SCHEMA IF EXISTS {index_schema.CAMPAIGN_SCHEMA} CASCADE",
                     f"CREATE SCHEMA {schema}"):
            setup.execute(stmt)
    monkeypatch.setenv("ROBOVAST_INDEX_DSN", f"{DSN} options=-csearch_path={schema}")

    run_dir = campaign / "nominal" / "0"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "metrics.csv").write_text("value\n1.5\n2.5\n", encoding="utf-8")

    report = ingest_campaign(campaign)

    assert report["stages"]["index"]["verdict"] == STAGE_OK, report["stages"]["index"]
    with psycopg.connect(f"{DSN} options=-csearch_path={schema}", autocommit=True) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM metrics WHERE campaign_id = %s",
                            (campaign.name,)).fetchone()[0]
    assert rows == 2, "the archive's own derived files are what make it queryable"

    with psycopg.connect(DSN, autocommit=True) as teardown:
        teardown.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        teardown.execute(f"DROP SCHEMA IF EXISTS {index_schema.CAMPAIGN_SCHEMA} CASCADE")
