# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Taking a campaign *in* is a tracked operation, not a call that blocks until it is over.

Four properties this defends, each of which was a wrong answer at some point:

* the campaign is registered **before** any bytes move, so it appears in the campaign view
  at ``importing`` while it is still arriving rather than materialising at the end;
* postprocessing is chained exactly when the archive arrived **raw** -- carrying no
  postprocessing record, so its own steps and the campaign-end pass never ran -- and a
  postprocessed one must not be recomputed. An archive never carries tables either way:
  every table is built from the records the first time something names it;
* the campaign is made **durable before** that postprocess rather than after it, because the
  postprocess reads the campaign from its durable home;
* a failed import is **kept**, as a failed campaign. Deleting the tree was tried and was
  strictly worse: registering the campaign is what makes it visible while it arrives, and
  that entry outlives the failure, so removing the directory left it listed as ``failed``
  with no log and no report -- listed *and* undiagnosable.
"""

import json
import sys
import tarfile
import threading
import time
import traceback
from pathlib import Path

import pytest

from robovast.client.status import Phase
from robovast.service.interface import ImportCampaignRequest
from robovast.execution.status_recovery import reconstruct_status_from_disk
from tests.robovast_decode.conftest import make_campaign, make_roqsim_campaign
from tests.service.null_service import NullService
_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "historic_campaigns"
_SOURCE = _FIXTURES / "v1-campaign-2025-03-04-101500"


def _archive(tmp_path, *, postprocessed=False, name="camp.tar.gz",
             runs: int = 0) -> Path:
    """A campaign archive, raw or postprocessed, built from the historic fixture.

    *runs* writes an ``_execution/outcome.json`` carrying that run tally, which is what a
    real archive holds -- the historic fixtures predate the durable outcome and have none,
    so a test about what an import REPORTS needs one or it asserts 0 == 0.
    """
    import shutil
    staged = tmp_path / "staged" / _SOURCE.name
    shutil.copytree(_SOURCE, staged)
    (staged / "_execution").mkdir(exist_ok=True)
    if postprocessed:
        # Postprocessing's provenance record, which is what says an archive carries derived
        # data. This wrote an empty `_execution/data.db` until that file was retired -- and
        # since the predicate under test read the same file, the fixture and the code agreed
        # with each other while both were wrong about the world.
        import yaml as _yaml
        record = staged / "_transient" / "postprocessing.yaml"
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(
            _yaml.safe_dump({"entries": [{"plugin": "rosbags_tf_to_csv",
                                          "output": "poses.csv"}]}),
            encoding="utf-8")
    if runs:
        (staged / "_execution" / "outcome.json").write_text(json.dumps({
            "phase": "finished", "mode": "batch", "batches_done": 1,
            "postprocessed": postprocessed,
            "runs": {"completed": runs, "total": runs,
                     "no_result": 0, "failed": 0, "killed": 0, "invalid": 0},
        }))
    out = tmp_path / name
    with tarfile.open(out, "w:gz") as tar:
        tar.add(staged, arcname=_SOURCE.name)
    return out


@pytest.fixture(name="service")
def _service(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBOVAST_RESULTS_DIR", str(tmp_path / "results"))
    transport = NullService()
    monkeypatch.setattr(type(transport), "_campaigns_root",
                        lambda self: tmp_path / "results")

    # Never run the real postprocessing chain from these tests.
    #
    # A raw archive chains postprocessing, whose auto-appended rosbags step shells out to
    # docker_exec.sh -- so importing the historic fixture, which pins an image in its v1
    # `execution.image`, makes a unit test pull a container image. Where that pull is slow
    # or the daemon is unreachable the step does not fail, it blocks: the plugin reads the
    # subprocess line by line with no deadline, so the import worker parks in
    # `for line in process.stdout` and the campaign never leaves `postprocessing`.
    #
    # That is what made these tests pass on a developer machine, where the image is already
    # local, and hang on CI, where it is not. Stubbing it here rather than in the two tests
    # that hit it because nothing in this file is ABOUT what postprocessing computes -- the
    # three tests that care about the chain being run at all install their own stub over
    # this one and assert on that.
    monkeypatch.setattr(type(transport), "_postprocess_campaign",
                        lambda self, campaign_id, campaign_dir, **kw: (True, "stubbed"))
    return transport


def _stuck_report(service, campaign_id) -> str:
    """What the import was doing when it ran out of time.

    An expired deadline says only that the worker did not finish, which is the one thing
    already known. Everything that distinguishes the possible causes -- a phase it never
    left, a step that logged its start and not its end, a thread parked in a network read --
    is still in the process at that moment and is gone the instant the assertion is raised.

    Best-effort throughout: this runs while a test is already failing, and a diagnostic that
    raises replaces a real failure with its own.
    """
    parts = []
    try:
        entry = service._campaigns.get(campaign_id)  # pylint: disable=protected-access
        snap = entry.state.snapshot() if entry is not None else None
        parts.append(f"tracked phase: {getattr(snap, 'phase', None)!r} "
                     f"stage: {getattr(snap, 'stage', None)!r} "
                     f"error: {getattr(snap, 'error', None)!r}")
    except Exception as exc:  # pylint: disable=broad-except
        parts.append(f"tracked entry unreadable: {exc!r}")

    # The campaign log is where the import narrates itself, so its last lines name the step
    # that started and never returned.
    try:
        log = (Path(service._campaigns_root())  # pylint: disable=protected-access
               / campaign_id / "_execution" / "import.log")
        if log.exists():
            tail = log.read_text(errors="replace").splitlines()[-25:]
            parts.append("import.log tail:\n  " + "\n  ".join(tail))
        else:
            parts.append(f"no import.log at {log}")
    except Exception as exc:  # pylint: disable=broad-except
        parts.append(f"import.log unreadable: {exc!r}")

    # The worker runs in a thread, so its stack is the answer to "where is it blocked?" --
    # a pip install waiting on credentials and a busy loop look identical from outside.
    try:
        named = {t.ident: t.name for t in threading.enumerate()}
        for ident, frame in sys._current_frames().items():  # pylint: disable=protected-access
            if ident == threading.get_ident():
                continue
            stack = "".join(traceback.format_stack(frame)).rstrip()
            parts.append(f"thread {named.get(ident, ident)!r}:\n{stack}")
    except Exception as exc:  # pylint: disable=broad-except
        parts.append(f"thread stacks unavailable: {exc!r}")

    return "\n".join(parts)


def _wait_done(service, campaign_id, timeout=30.0):
    """Block until the tracked entry leaves the live phases, or fail loudly."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        entry = service._campaigns.get(campaign_id)  # pylint: disable=protected-access
        if entry is not None and entry.state.snapshot().phase in (
                Phase.FINISHED, Phase.FAILED):
            return entry.state.snapshot()
        time.sleep(0.05)
    raise AssertionError(
        f"import of {campaign_id} did not finish within {timeout}s\n"
        f"{_stuck_report(service, campaign_id)}")


def test_the_campaign_is_named_and_listed_before_any_extraction(service, tmp_path):
    # The id comes out of the archive's member list, which is why the campaign can be
    # registered up front. A ref, not a prose message: create, retrigger and import all
    # mean "a campaign now exists, here is its id", and an id a caller has to parse out
    # of a sentence breaks the first time the sentence is reworded.
    ref = service.import_campaign(ImportCampaignRequest(
        archive_path=str(_archive(tmp_path))))
    assert ref.campaign_id == _SOURCE.name
    _wait_done(service, ref.campaign_id)


def test_a_raw_archive_says_up_front_that_postprocessing_follows(service, tmp_path):
    # Knowable before anything moves: `_execution/data.db` is visible in the tar index.
    ref = service.import_campaign(ImportCampaignRequest(
        archive_path=str(_archive(tmp_path))))
    assert "postprocessing" in ref.note
    _wait_done(service, ref.campaign_id)


def test_a_postprocessed_archive_is_not_recomputed(service, tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(type(service), "_postprocess_campaign",
                        lambda self, cid, d, **k: ran.append(cid) or (True, "ok"))
    ref = service.import_campaign(ImportCampaignRequest(
        archive_path=str(_archive(tmp_path, postprocessed=True))))
    assert ref.note == "", "nothing to warn about: the tables are already there"
    _wait_done(service, ref.campaign_id)
    assert ran == [], "a campaign that arrived with its metrics must be left alone"


def test_a_raw_archive_is_postprocessed_once_it_lands(service, tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(type(service), "_postprocess_campaign",
                        lambda self, cid, d, **k: ran.append(cid) or (True, "ok"))
    ref = service.import_campaign(ImportCampaignRequest(
        archive_path=str(_archive(tmp_path))))
    _wait_done(service, ref.campaign_id)
    assert ran == [ref.campaign_id]


def test_a_second_import_of_the_same_campaign_is_refused_before_it_transfers(service,
                                                                            tmp_path):
    archive = _archive(tmp_path)
    ref = service.import_campaign(ImportCampaignRequest(archive_path=str(archive)))
    _wait_done(service, ref.campaign_id)
    # RuntimeError is the interface's word for a conflict -- a 409, not a silent overwrite.
    # A campaign's records are evidence; replacing them is asked for explicitly.
    with pytest.raises(RuntimeError, match="already here"):
        service.import_campaign(ImportCampaignRequest(archive_path=str(archive)))
    service.import_campaign(ImportCampaignRequest(archive_path=str(archive), force=True))
    _wait_done(service, ref.campaign_id)


def test_a_failed_import_is_kept_so_its_reason_can_be_read(service, tmp_path, monkeypatch):
    """A failed import stays put, with the log that explains it.

    Found live. The campaign is registered *before* the transfer -- that is what puts it in
    the campaign view at ``importing`` -- and the tracked entry survives the failure. So
    deleting the directory afterwards did not unlist anything; it only removed the
    ``import.log`` and ``import.json`` that said what went wrong, leaving a campaign listed
    as ``failed`` with nothing behind it. Keeping it makes it an ordinary failed campaign:
    inspectable, and removable with ``vast campaign delete``.
    """
    def _boom(*_a, **_k):
        raise OSError("disk went away mid-extraction")
    monkeypatch.setattr("robovast.service.ingest.extract_archive", _boom)

    ref = service.import_campaign(ImportCampaignRequest(
        archive_path=str(_archive(tmp_path))))
    status = _wait_done(service, ref.campaign_id)
    assert status.phase == Phase.FAILED
    assert status.error

    campaign = service._campaigns_root() / ref.campaign_id  # pylint: disable=protected-access
    assert campaign.is_dir(), "the failed import must remain, or its reason is unreadable"
    log = (campaign / "_execution" / "import.log").read_text(encoding="utf-8")
    assert "disk went away mid-extraction" in log
    # Durable too: the tracked entry carrying `error` lives only in this process, so a
    # service restart must still find this campaign failed rather than merely unfinished.
    outcome = (campaign / "_execution" / "outcome.json").read_text(encoding="utf-8")
    assert "failed" in outcome


def test_an_archive_that_is_not_the_campaign_it_was_fetched_as_is_refused(
        service, tmp_path, monkeypatch):
    """The id is claimed from the object's *name*; the tree lands under the tar's own.

    Nothing compared them. A mismatch extracted as some other campaign, ingested the empty
    directory claimed here, and reported that directory's symptom -- ``config, layout`` --
    under an id that was not the one that failed, while the campaign that did arrive sat in
    the results root unregistered. Every part of that is silent, and the reader's first
    question ("why does the error name a different campaign?") had no answer anywhere.
    """
    archive = _archive(tmp_path, name="renamed.tar.gz")
    # The share path takes the id from the object name, so this is a fetch that hands back
    # an archive of some *other* campaign -- a mislabelled object, or a name reused.
    monkeypatch.setattr(type(service), "_resolve_import_source",
                        lambda self, request: ("other-2026-01-01-000000",
                                               lambda _log: (archive, False), True))

    ref = service.import_campaign(ImportCampaignRequest(share_archive="whatever"))
    status = _wait_done(service, ref.campaign_id)

    assert status.phase == Phase.FAILED
    assert _SOURCE.name in status.error and "other-2026-01-01-000000" in status.error, \
        "both names, or the reader cannot tell which of the two is wrong"
    results = service._campaigns_root()  # pylint: disable=protected-access
    assert not (results / _SOURCE.name).exists(), \
        "and nothing is extracted: a stray unregistered campaign is what this prevents"


def test_an_import_names_exactly_one_source(service, tmp_path):
    with pytest.raises(ValueError, match="exactly one source"):
        service.import_campaign(ImportCampaignRequest())
    with pytest.raises(ValueError, match="exactly one source"):
        service.import_campaign(ImportCampaignRequest(
            archive_path=str(_archive(tmp_path)), share_archive="whatever"))


def test_a_campaigns_job_symlinks_survive_the_hardened_extraction(service, tmp_path):
    # Extraction uses `filter='data'`, which refuses absolute paths and ../ escapes -- an
    # archive from elsewhere is untrusted input. A campaign's own `<config>/<run>/job`
    # links point *within* the campaign, so they must come through it intact; if they did
    # not, every imported campaign would lose the path from a run to its job artifacts.
    import shutil
    staged = tmp_path / "staged" / _SOURCE.name
    shutil.copytree(_SOURCE, staged)
    (staged / "_jobs" / "batch-0" / "job-0").mkdir(parents=True, exist_ok=True)
    run_dir = staged / "config-a" / "0"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "job").symlink_to("../../_jobs/batch-0/job-0")
    archive = tmp_path / "with-links.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(staged, arcname=_SOURCE.name)

    ref = service.import_campaign(ImportCampaignRequest(archive_path=str(archive)))
    _wait_done(service, ref.campaign_id)
    landed = service._campaigns_root() / ref.campaign_id / "config-a" / "0" / "job"  # pylint: disable=protected-access
    assert landed.is_symlink()


def test_an_imported_campaign_reports_what_it_actually_holds(service, tmp_path):
    """The tracked entry must adopt the campaign's own record before it finishes.

    That entry is constructed EMPTY -- it exists so the campaign is visible while its
    bytes arrive -- and it shadows the durable ``outcome.json`` for as long as it lives.
    So an import ended reporting ``0 runs`` and ``postprocessed: false`` over a campaign
    whose tables were all present, and the status went on to advise running postprocessing
    that would recompute every one of them.
    """
    archive = _archive(tmp_path, postprocessed=True, runs=26)
    ref = service.import_campaign(ImportCampaignRequest(archive_path=str(archive)))
    status = _wait_done(service, ref.campaign_id)

    on_disk = reconstruct_status_from_disk(
        service.campaign_dir(ref.campaign_id))  # pylint: disable=protected-access
    assert on_disk.runs.total > 0, "fixture must carry runs for this to test anything"

    assert status.postprocessed is True
    assert status.runs.total == on_disk.runs.total
    assert status.runs.completed == on_disk.runs.completed
    assert status.mode == on_disk.mode


def test_a_raw_import_also_reports_its_run_tally(service, tmp_path, monkeypatch):
    """A raw archive reports its run tally, not only its postprocessing verdict.

    Both arrival paths go through the same adoption, so neither can report an empty
    campaign; this is the raw half of that property.
    """
    monkeypatch.setattr(type(service), "_postprocess_campaign",
                        lambda self, cid, d, **k: (True, "ok"))
    ref = service.import_campaign(ImportCampaignRequest(
        archive_path=str(_archive(tmp_path, runs=26))))
    status = _wait_done(service, ref.campaign_id)
    assert status.runs.total == 26


def _object_store_archive(tmp_path, staged: Path, name: str) -> Path:
    """An archive as an exporter reading an object store writes one.

    Built member by member: regular files only -- a bucket has no directories -- each mode
    set explicitly, and the ``job`` symlinks synthesised at the end from
    ``_transient/job_links.yaml``, because a bucket holds no links either. Shares hold
    archives of this shape, and importing one is how a campaign kept in an object store
    reaches this service.
    """
    import os
    import yaml as _yaml
    out = tmp_path / name
    with tarfile.open(out, "w:gz") as tar:
        for path in sorted(p for p in staged.rglob("*") if p.is_file()):
            rel = path.relative_to(staged).as_posix()
            info = tarfile.TarInfo(name=f"{staged.name}/{rel}")
            info.size = path.stat().st_size
            info.mode = 0o755 if os.access(path, os.X_OK) else 0o644
            with open(path, "rb") as body:
                tar.addfile(info, body)
        manifest = staged / "_transient" / "job_links.yaml"
        for link, target in (_yaml.safe_load(manifest.read_text()) or {}).items():
            info = tarfile.TarInfo(name=f"{staged.name}/{link}")
            info.type = tarfile.SYMTYPE
            info.linkname = target
            info.mode = 0o777
            tar.addfile(info)
    return out


def test_an_archive_exported_from_an_object_store_imports(service, tmp_path):
    """A campaign kept in an object store arrives through a share, as such an archive.

    So it must import: no directory members, links added after the files they point at,
    executables marked by mode alone -- and the files such a campaign carries (a conversion
    output list, archived log sections) are data like any other, not a reason to refuse.
    """
    import shutil
    import yaml as _yaml
    staged = tmp_path / "staged" / _SOURCE.name
    shutil.copytree(_SOURCE, staged)
    (staged / "_jobs" / "batch-0" / "job-0").mkdir(parents=True, exist_ok=True)
    (staged / "_jobs" / "batch-0" / "job-0" / "controller.log").write_text("ran\n")
    (staged / "_transient").mkdir(exist_ok=True)
    (staged / "_transient" / "job_links.yaml").write_text(_yaml.safe_dump(
        {"config-a/0/job": "../../_jobs/batch-0/job-0"}))
    (staged / "config-a" / "0").mkdir(parents=True, exist_ok=True)
    script = staged / "_config" / "files" / "prepare.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/bin/sh\necho prepared\n")
    script.chmod(0o755)
    exec_dir = staged / "_execution"
    (exec_dir / "sections").mkdir(parents=True, exist_ok=True)
    (exec_dir / "controller.log").write_text("ran the campaign\n")
    (exec_dir / "sections" / "0001-postprocessing.log").write_text("first postprocess\n")
    (exec_dir / "postprocessing.log").write_text("second postprocess\n")
    (exec_dir / "conversion_outputs.txt").write_text("config-a/0/poses.csv\n")

    archive = _object_store_archive(tmp_path, staged, "from-a-bucket.tar.gz")
    ref = service.import_campaign(ImportCampaignRequest(archive_path=str(archive)))
    status = _wait_done(service, ref.campaign_id)

    assert status.phase == Phase.FINISHED, status.error
    landed = service.campaign_dir(ref.campaign_id)
    link = landed / "config-a" / "0" / "job"
    assert link.is_symlink() and (link / "controller.log").read_text() == "ran\n"
    assert (landed / "_config" / "files" / "prepare.sh").stat().st_mode & 0o111
    messages = [row.message for row in service.get_campaign_logs(ref.campaign_id).rows]
    assert messages.index("first postprocess") < messages.index("second postprocess")


# -- what an archive carries, and what is built from it ------------------------------------

_CURRENT = _FIXTURES / "v5-campaign-2026-09-23-090000"


def _recorded_archive(tmp_path, name: str, fill) -> Path:
    """An archive of a campaign whose runs carry a recording and nothing derived from it.

    *fill* writes the run directories (the decoder fixtures' campaign makers); the frozen
    configuration comes from the current-version fixture, so the ``config`` stage reads it
    as it is. No ``.cache/`` goes in: an archive never carries tables, whichever service
    wrote it, because the tables are built from the records wherever the campaign is read.
    """
    import shutil
    staged = tmp_path / "staged" / name
    fill(staged)
    shutil.copytree(_CURRENT / "_config", staged / "_config")
    (staged / "_execution").mkdir(exist_ok=True)
    out = tmp_path / f"{name}.tar.gz"
    with tarfile.open(out, "w:gz") as tar:
        tar.add(staged, arcname=name)
    return out


@pytest.mark.parametrize("fill, table", [
    pytest.param(make_campaign, "poses", id="rosbag2"),
    pytest.param(make_roqsim_campaign, "sim_poses", id="roqsim_bag"),
])
def test_an_imported_recording_is_a_table_the_first_time_something_names_it(
        service, tmp_path, fill, table):
    """An import builds no table: what the records can give is listed, and built on use.

    The scenario recording (``rosbag2/``) gives ``poses``, roqsim's own (``roqsim_bag/``)
    gives ``sim_poses``. After the import each is described as built for none of its runs,
    a query over it answers by building it, and it is then described as built for all.
    The postprocessing chain is stubbed by the fixture, so nothing here rides on the
    campaign-end pass: the engine is what answers.
    """
    runs = (("cfg", 0), ("cfg", 1))
    archive = _recorded_archive(tmp_path, "recorded-2026-01-01-000000",
                                lambda root: fill(root, runs=runs))
    ref = service.import_campaign(ImportCampaignRequest(archive_path=str(archive)))
    status = _wait_done(service, ref.campaign_id)
    assert status.phase == Phase.FINISHED, status.error

    report = json.loads((service.campaign_dir(ref.campaign_id) / "_execution"
                         / "import.json").read_text(encoding="utf-8"))
    assert report["stages"]["tables"]["verdict"] == "ok"
    assert "first time" in report["stages"]["tables"]["detail"]

    def described():
        return {t.table: t for t in service.describe_campaign_data(ref.campaign_id).tables}

    before = described()[table]
    assert (before.built, before.runs) == (0, len(runs)), "an archive carries no tables"
    assert before.columns == [], "columns are known once it is built for some run"

    result = service.query_campaign_data_sql(
        ref.campaign_id, f"SELECT config_name, run_id, count(*) AS n FROM {table} "
                         f"GROUP BY 1, 2 ORDER BY 2")
    assert [(r["run_id"], r["n"] > 0) for r in result.rows] == [(0, True), (1, True)]

    after = described()[table]
    assert (after.built, after.runs) == (len(runs), len(runs))
    assert "frame" in " ".join(after.columns)
