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

import io
import json
import os
import shutil
import sqlite3
import tarfile
from pathlib import Path

import pytest
import yaml

from robovast.common.store import _MIGRATIONS, SCHEMA_VERSION
from robovast.service.ingest import (STAGE_ABSENT, STAGE_DEGRADED, STAGE_FAILED, STAGE_MIGRATED,
                                     STAGE_NEWER, STAGE_OK, blocking_summary,
                                     claim_campaign_dir, extract_archive, ingest_campaign,
                                     missing_for_import, missing_for_import_in, read_campaign_id)

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
    assert stage["steps"] == ["1_to_2", "2_to_3", "3_to_4", "4_to_5", "5_to_6"]
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


@pytest.mark.parametrize("version,says", [
    (None, "declares no 'version:'"),
    ("4", "must be an integer"),
])
def test_a_config_with_no_usable_version_blocks_the_import(campaign, version, says):
    """A file with nothing to start the ladder from is the opposite of one from a newer
    robovast, and they need opposite recoveries. Importing it as displayable would defer the
    failure to whoever tries to re-run it, where the archive is all that is left to go on."""
    vast_path = next((campaign / "_config").glob("*.vast"))
    raw = yaml.safe_load(vast_path.read_text(encoding="utf-8"))
    if version is None:
        raw.pop("version", None)
    else:
        raw["version"] = version
    vast_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    report = ingest_campaign(campaign)
    stage = report["stages"]["config"]
    assert stage["verdict"] == STAGE_FAILED
    assert says in stage["detail"]
    assert stage["recovery"], "a blocking stage names what to do about it"
    assert report["ok"] is False and "config" in report["blocking"]


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


def test_the_importers_own_log_is_not_an_execution_record(tmp_path, campaign):
    """An import claims ``_execution/`` and opens its log there before extracting, so an
    archive without ``_execution/`` arrives with one holding only the importer's files."""
    shutil.rmtree(campaign / "_execution")
    target = claim_campaign_dir(tmp_path / "results", campaign.name)
    (target / "_execution" / "import.log").write_text("importing\n", encoding="utf-8")
    shutil.copytree(campaign, target, dirs_exist_ok=True)
    report = ingest_campaign(target)
    assert report["stages"]["layout"]["verdict"] == STAGE_DEGRADED
    assert "_execution" in report["stages"]["layout"]["detail"]


def test_a_store_indexing_no_runs_is_degraded_not_ok(campaign):
    """A campaign that lists and reports nothing is the shape of an archive stripped of its run
    directories. Passing that as `ok` would read as "checked, all fine"."""
    stage = ingest_campaign(campaign)["stages"]["campaign_store"]
    # The historic fixtures carry records but no run directories, which is exactly this case.
    assert stage["verdict"] == STAGE_DEGRADED
    assert stage["runs"] == 0
    assert "no runs" in stage["detail"]


def test_records_that_give_no_table_are_reported_absent(campaign):
    """An archive stripped of its run directories lists and opens but answers nothing."""
    stage = ingest_campaign(campaign)["stages"]["tables"]
    assert stage["verdict"] == STAGE_ABSENT
    assert "no data file" in stage["detail"]


def test_the_tables_stage_builds_nothing(campaign):
    """Importing is not building: a table is built the first time something names it."""
    run_dir = campaign / "nominal" / "0"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "metrics.csv").write_text("value\n1.5\n2.5\n", encoding="utf-8")

    stage = ingest_campaign(campaign)["stages"]["tables"]

    assert stage["verdict"] == STAGE_OK, stage
    assert "built the first time" in stage["detail"]
    assert not (campaign / ".cache" / "tables").exists()


def test_every_stage_carries_an_actionable_detail(campaign):
    """A verdict a reader cannot act on is not worth returning."""
    report = ingest_campaign(campaign)
    assert set(report["stages"]) == {"archive", "layout", "config", "completeness",
                                     "environment", "campaign_store", "tables"}
    for name, stage in report["stages"].items():
        assert stage["detail"].strip(), f"{name} has no detail"


def test_a_refusal_reports_the_reason_and_not_only_which_check_failed(tmp_path):
    """The stage names are an index, not a diagnosis.

    ``config, layout`` is what *every* incomplete archive says, whether the ``.vast`` is
    missing, unparseable, or from a newer robovast. The sentence that distinguishes them is
    carried in the error, because ``import.log``/``import.json`` live inside a campaign that is
    published only once its import succeeded.
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


def test_the_export_check_reads_a_list_of_paths_as_readily_as_a_tree():
    """Paths, not a directory: an export decides what it is about to write from the member
    list it is about to write, before there is a tree anywhere to stat. Both callers hand
    over campaign-relative paths and get the same answer."""
    assert missing_for_import(["_execution/controller.log", "config1/1/test.xml"]), \
        "no _config/ at all is the shape a campaign that died before setup exports as"
    assert missing_for_import(["_config/", "_config/scenario.osc"]), \
        "a _config/ carrying no .vast is refused for the .vast, not for the directory"
    assert missing_for_import(["_config/nav.vast", "_execution/data.db"]) == []
    # Absence of derived data is not incompleteness: raw is the normal thing to share.
    assert missing_for_import(["_config/nav.vast"]) == []


# -- what an import runs afterwards -------------------------------------------


def test_an_archive_that_arrived_postprocessed_is_not_recomputed(tmp_path, monkeypatch):
    """The postprocessing record is the evidence the pass finished; an archive carrying it
    is not postprocessed again, and its tables are built from its records on first use."""
    from tests.service.null_service import NullService
    from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore

    results = tmp_path / "results"
    cid = "arrived-2026-09-01-101500"
    target = results / cid
    (target / "_execution").mkdir(parents=True)
    record = target / "_transient" / "postprocessing.yaml"
    record.parent.mkdir(parents=True)
    record.write_text(yaml.safe_dump({"entries": [{"plugin": "p", "output": "poses.csv"}]}),
                      encoding="utf-8")

    store = WorkspaceStore(registry=WorkspaceRegistry(root=str(tmp_path / "ws")))
    transport = NullService(store=store)
    transport._campaigns_root = lambda: results             # noqa: SLF001
    ran = []
    monkeypatch.setattr(transport, "_postprocess_campaign",
                        lambda *a, **k: ran.append(a) or (True, ""))

    transport._postprocess_after_import(_State(), cid, target)   # noqa: SLF001

    assert not ran, "a campaign that arrived with derived data must not be recomputed"


def test_a_raw_archive_still_gets_postprocessed(tmp_path, monkeypatch):
    """The other half: without the record the campaign-end pass never ran, so run it."""
    from tests.service.null_service import NullService
    from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore

    results = tmp_path / "results"
    cid = "raw-2026-09-01-101501"
    target = results / cid
    (target / "_execution").mkdir(parents=True)

    store = WorkspaceStore(registry=WorkspaceRegistry(root=str(tmp_path / "ws")))
    transport = NullService(store=store)
    transport._campaigns_root = lambda: results             # noqa: SLF001
    ran = []
    monkeypatch.setattr(transport, "_postprocess_campaign",
                        lambda *a, **k: ran.append(a) or (True, ""))

    transport._postprocess_after_import(_State(), cid, target)   # noqa: SLF001

    assert ran, "a raw archive has not been postprocessed and must be"


class _State:
    """The minimum of the live-entry state the import chain writes through."""

    def set_phase(self, *_a, **_k):
        pass

    def update(self, *_a, **_k):
        pass


def test_a_snapshot_import_is_degraded_and_says_what_is_missing(campaign):
    """A campaign archived mid-run imports, and cannot import quietly.

    Degraded rather than refused: a snapshot is a real campaign with runs missing, and the
    person importing it may hold the only copy. But nothing else in the report can notice —
    every other stage inspects what is *present*, and a snapshot differs from a finished
    campaign only in what is absent — so the marker the archiver wrote is the campaign
    saying so itself.
    """

    from robovast.execution.campaign_archive import SNAPSHOT_MEMBER

    marker = campaign / SNAPSHOT_MEMBER
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"complete": False, "runs_completed": 3, "runs_total": 20}),
                      encoding="utf-8")

    report = ingest_campaign(campaign)

    assert report["ok"] is True, "a snapshot must not be refused"
    stage = report["stages"]["completeness"]
    assert stage["verdict"] == "degraded"
    assert "3/20 runs" in stage["detail"]


def _bomb(tmp_path, unpacked: int) -> Path:
    """One campaign whose single file unpacks to *unpacked* bytes of zeros -- kilobytes packed."""
    out = tmp_path / "bomb.tar.gz"
    with tarfile.open(out, "w:gz") as tar:
        tar.add(_FIXTURES / "v1-campaign-2025-03-04-101500",
                arcname="bomb-2026-01-01-000000")
        info = tarfile.TarInfo("bomb-2026-01-01-000000/zeros.bin")
        info.size = unpacked
        tar.addfile(info, io.BytesIO(bytes(unpacked)))
    return out


def test_an_archive_that_unpacks_past_the_room_above_the_reserve_is_refused(
        tmp_path, monkeypatch):
    """The compressed size says nothing about what extraction writes: the member sizes the
    index already carries are summed and held to the room above the reserve, before a byte
    is written. Only when asked -- reading an id alone stays a read of the index."""
    from robovast.common.errors import InsufficientStorageError
    archive = _bomb(tmp_path, 4 * 1024 * 1024)
    assert archive.stat().st_size < 1024 * 1024
    monkeypatch.setattr("robovast.common.disk_reserve.room_bytes", lambda _path: 1024 * 1024)

    with pytest.raises(InsufficientStorageError, match="unpacks to .* GB free above its reserve"):
        read_campaign_id(archive, fits_in=tmp_path / "results")
    assert read_campaign_id(archive) == "bomb-2026-01-01-000000"

    monkeypatch.setattr("robovast.common.disk_reserve.room_bytes", lambda _path: 10 ** 9)
    assert read_campaign_id(archive, fits_in=tmp_path / "results") == "bomb-2026-01-01-000000"


def test_an_archive_of_many_empty_entries_is_held_to_the_blocks_they_take(tmp_path, monkeypatch):
    """Sizes of zero still take a block each: an archive of empty entries is charged for them."""
    from robovast.common.errors import InsufficientStorageError
    out = tmp_path / "entries.tar.gz"
    with tarfile.open(out, "w:gz") as tar:
        tar.add(_FIXTURES / "v1-campaign-2025-03-04-101500",
                arcname="entries-2026-01-01-000000")
        for i in range(2000):
            info = tarfile.TarInfo(f"entries-2026-01-01-000000/empty/{i}")
            info.type = tarfile.DIRTYPE if i % 2 else tarfile.REGTYPE
            tar.addfile(info)
    monkeypatch.setattr("robovast.common.disk_reserve.block_bytes", lambda _path: 4096)
    monkeypatch.setattr("robovast.common.disk_reserve.room_bytes", lambda _path: 2000 * 4096 - 1)

    with pytest.raises(InsufficientStorageError, match="unpacks to"):
        read_campaign_id(out, fits_in=tmp_path / "results")


# -- what the configuration needs from this deployment -----------------------

def _vast(campaign) -> Path:
    return next((campaign / "_config").glob("*.vast"))


def _declare(campaign, **sections):
    """Add *sections* to the campaign's frozen ``.vast`` (the version-1 fixture's)."""
    vast = _vast(campaign)
    raw = yaml.safe_load(vast.read_text(encoding="utf-8"))
    for key, value in sections.items():
        if key == "variations":
            raw.setdefault("configuration", [{"name": "cfg"}])[0]["variations"] = value
        elif key in ("postprocessing", "metadata_processing", "health_checks"):
            raw.setdefault("results_processing", {})[key] = value
        else:
            raw[key] = value
    vast.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")


def test_what_the_configuration_names_and_this_deployment_lacks_is_degraded_by_name(campaign):
    """A raw import chains postprocessing straight away, so what it would lack is named in the
    import report. Never blocking: the campaign lists without any of it."""
    _declare(campaign,
             variations=[{"NoSuchVariation": {}}, {"ParameterVariationList": {}}],
             postprocessing=["rosbags_tf_to_csv", {"no_such_command": {}},
                             "./missing.py:Step"],
             plugins=["robovast-no-such-plugin==1.0"])
    stage = ingest_campaign(campaign)["stages"]["environment"]
    assert stage["verdict"] == STAGE_DEGRADED
    for name in ("NoSuchVariation", "no_such_command", "./missing.py:Step",
                 "robovast-no-such-plugin==1.0"):
        assert name in stage["detail"]
    for present in ("ParameterVariationList", "rosbags_tf_to_csv"):
        assert present not in stage["detail"]


def test_a_configuration_this_deployment_can_run_is_ok(campaign):
    _declare(campaign, variations=[{"ParameterVariationList": {}}],
             postprocessing=["rosbags_tf_to_csv"])
    assert ingest_campaign(campaign)["stages"]["environment"]["verdict"] == STAGE_OK


def test_a_configuration_that_cannot_be_parsed_is_reported_not_raised(campaign):
    """The config stage refuses such a file by name; the environment stage must not turn that
    refusal into an exception that loses the whole report."""
    _vast(campaign).write_text("version: [unclosed\n", encoding="utf-8")
    report = ingest_campaign(campaign)
    assert report["stages"]["config"]["verdict"] == STAGE_FAILED
    assert report["stages"]["environment"]["verdict"] == STAGE_ABSENT


def test_no_configuration_is_not_reported_as_needing_nothing(campaign):
    _vast(campaign).unlink()
    assert ingest_campaign(campaign)["stages"]["environment"]["verdict"] == STAGE_ABSENT


def test_what_a_raw_import_postprocessing_runs_is_checked_in_full(campaign):
    """Postprocessing also runs the metadata processors and the health checks the campaign
    declares, and a health check that is not installed is skipped rather than failing -- so
    the import report is the one place that says it will not run."""
    (_vast(campaign).parent / "check.py").write_text("class Check: pass\n", encoding="utf-8")
    _declare(campaign,
             postprocessing=["rosbags_to_cvs"],
             metadata_processing=["no_such_processor"],
             health_checks=["no_such_check", "./check.py:Check", "./absent.py:Check"])
    stage = ingest_campaign(campaign)["stages"]["environment"]
    assert stage["verdict"] == STAGE_DEGRADED
    for name in ("rosbags_to_cvs", "no_such_processor", "no_such_check", "./absent.py:Check"):
        assert name in stage["detail"]
    assert "./check.py:Check" not in stage["detail"]


def test_a_config_that_is_not_a_mapping_blocks_the_import_by_name(campaign):
    """A .vast whose document is a list parses as YAML but holds no configuration. The config
    stage refuses it and says why, rather than the import raising and reporting nothing."""
    vast_path = next((campaign / "_config").glob("*.vast"))
    vast_path.write_text("- a list\n- not a mapping\n", encoding="utf-8")
    report = ingest_campaign(campaign)
    stage = report["stages"]["config"]
    assert stage["verdict"] == STAGE_FAILED
    assert "not a mapping" in stage["detail"]
    assert report["ok"] is False and "config" in report["blocking"]
    assert report["stages"]["environment"]["verdict"] == STAGE_ABSENT


# -- the archive layout ------------------------------------------------------

def _stamp(campaign, **fields):
    from robovast.common.migrations.archive import ARCHIVE_STAMP
    (campaign / ARCHIVE_STAMP).write_text(json.dumps(fields), encoding="utf-8")


def test_an_archive_without_a_stamp_is_the_current_layout_and_is_left_unstamped(campaign):
    from robovast.common.migrations.archive import ARCHIVE_LAYOUT, ARCHIVE_STAMP
    report = ingest_campaign(campaign)
    assert report["ok"] is True
    stage = report["stages"]["archive"]
    assert stage["verdict"] == STAGE_OK
    assert stage["version"] == ARCHIVE_LAYOUT
    assert not (campaign / ARCHIVE_STAMP).exists()


def test_an_archive_at_the_current_layout_is_ok(campaign):
    from robovast.common.migrations.archive import ARCHIVE_LAYOUT
    _stamp(campaign, layout=ARCHIVE_LAYOUT)
    report = ingest_campaign(campaign)
    assert report["stages"]["archive"]["verdict"] == STAGE_OK
    assert report["ok"] is True


def test_an_archive_from_a_newer_layout_is_newer_and_named(campaign):
    """Somebody's data from a newer robovast still lists; the stage says which layout and
    which robovast wrote it, rather than refusing or passing it silently."""
    from robovast.common.migrations.archive import ARCHIVE_LAYOUT
    _stamp(campaign, layout=ARCHIVE_LAYOUT + 1, robovast="99.0.0")
    report = ingest_campaign(campaign)
    stage = report["stages"]["archive"]
    assert stage["verdict"] == STAGE_NEWER
    assert f"layout {ARCHIVE_LAYOUT + 1}" in stage["detail"] and "99.0.0" in stage["detail"]
    assert f"up to {ARCHIVE_LAYOUT}" in stage["detail"]
    assert report["ok"] is True


def test_a_stamp_that_states_no_layout_blocks_the_import(campaign):
    _stamp(campaign, layout="one")
    report = ingest_campaign(campaign)
    assert report["ok"] is False and report["blocking"] == ["archive"]
    assert "'one'" in report["stages"]["archive"]["detail"]


# -- extraction stays inside the campaign ------------------------------------

def _results_with_victim(tmp_path):
    """A results tree holding another campaign, whose files an archive must not reach."""
    results = tmp_path / "results"
    victim = results / "victim-2026-01-01-000000"
    (victim / "_config").mkdir(parents=True)
    (victim / "campaign.db").write_bytes(b"original")
    return results, victim


def _archive_of(tmp_path, campaign_id, members) -> Path:
    """An archive of the version-1 fixture under *campaign_id*, plus *members* after it.

    Each member is ``(name, payload)`` for a file, ``(name, ("symlink", target))`` or
    ``(name, ("hardlink", target))``.
    """
    out = tmp_path / "archive.tar.gz"
    with tarfile.open(out, "w:gz") as tar:
        tar.add(_FIXTURES / "v1-campaign-2025-03-04-101500", arcname=campaign_id)
        for name, what in members:
            info = tarfile.TarInfo(name)
            if isinstance(what, tuple):
                info.type = tarfile.SYMTYPE if what[0] == "symlink" else tarfile.LNKTYPE
                info.linkname = what[1]
                tar.addfile(info)
            else:
                info.size = len(what)
                tar.addfile(info, io.BytesIO(what))
    return out


@pytest.mark.parametrize("members", [
    pytest.param([("camp-2026-02-02-000000/../victim-2026-01-01-000000/campaign.db", b"x")],
                 id="dot-dot-at-the-top"),
    pytest.param([("camp-2026-02-02-000000/_config/../../victim-2026-01-01-000000/campaign.db",
                   b"x")], id="dot-dot-below"),
    pytest.param([("camp-2026-02-02-000000/aside", ("symlink", "../victim-2026-01-01-000000")),
                  ("camp-2026-02-02-000000/aside/campaign.db", b"x")],
                 id="through-a-symlink-to-a-sibling"),
    pytest.param([("camp-2026-02-02-000000/twin",
                   ("hardlink", "camp-2026-02-02-000000/../victim-2026-01-01-000000/campaign.db"))],
                 id="hard-link-to-a-sibling"),
    pytest.param([("other-2026-03-03-000000/campaign.db", b"x")], id="another-campaign"),
])
def test_a_member_that_leaves_its_campaign_directory_is_refused_by_name(tmp_path, members):
    """The archive is extracted beside every other campaign, so "inside the results tree"
    is not confinement: a member that resolves to a sibling campaign would overwrite its
    records, and Python's data filter allows it. Every member is held to the campaign's own
    directory instead, and the extraction fails naming the one that left it."""
    results, victim = _results_with_victim(tmp_path)
    campaign_id = "camp-2026-02-02-000000"
    (results / campaign_id / "_execution").mkdir(parents=True)
    archive = _archive_of(tmp_path, campaign_id, members)

    with pytest.raises(ValueError, match="outside the") as refused:
        extract_archive(archive, results, campaign_id)

    assert "victim-2026-01-01-000000" in str(refused.value) or "other-2026" in str(refused.value)
    assert (victim / "campaign.db").read_bytes() == b"original"
    assert not (results / "other-2026-03-03-000000").exists()


def test_links_within_the_campaign_survive_the_confinement(tmp_path):
    """The ``job`` symlinks beside a campaign's runs and a hard link to one of its own files
    are what an archive of one campaign carries, and both land."""
    results, _ = _results_with_victim(tmp_path)
    campaign_id = "camp-2026-02-02-000000"
    (results / campaign_id / "_execution").mkdir(parents=True)
    archive = _archive_of(tmp_path, campaign_id, [
        (f"{campaign_id}/cfg/0/job", ("symlink", "../../_execution")),
        (f"{campaign_id}/twin", ("hardlink", f"{campaign_id}/_config/campaign.vast")),
    ])

    extract_archive(archive, results, campaign_id)

    landed = results / campaign_id
    assert (landed / "cfg" / "0" / "job").is_symlink()
    assert (landed / "cfg" / "0" / "job" / "execution.yaml").is_file()
    assert (landed / "twin").read_bytes() == (landed / "_config" / "campaign.vast").read_bytes()


def test_a_dot_rooted_archive_extracts_under_its_campaign_name(tmp_path):
    """``tar -C <dir> .`` names every member under ``./``; that root writes nothing and the
    campaign lands under its own name."""
    results, _ = _results_with_victim(tmp_path)
    campaign_id = "v1-campaign-2025-03-04-101500"
    staging = tmp_path / "staging"
    shutil.copytree(_FIXTURES / campaign_id, staging / campaign_id)
    archive = tmp_path / "dotted.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(staging, arcname=".")

    extract_archive(archive, results, campaign_id)

    assert (results / campaign_id / "_config" / "campaign.vast").is_file()
