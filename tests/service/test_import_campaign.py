# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Taking a campaign *in* is a tracked operation, not a call that blocks until it is over.

Three properties this defends, each of which was a wrong answer at some point:

* the campaign is registered **before** any bytes move, so it appears in the campaign view
  at ``importing`` while it is still arriving rather than materialising at the end;
* postprocessing is chained exactly when the archive arrived **raw**, because a campaign
  with no metric tables is not one anybody can query -- and a postprocessed one must not be
  recomputed;
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
from robovast.service.local_transport import LocalTransport

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
    transport = LocalTransport()
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


def test_a_failed_import_publishes_its_reason_on_a_lane_that_drops_the_scratch(
        service, tmp_path, monkeypatch):
    """Keeping the tree is only half the promise: it has to be kept where clients read.

    Found live on the cluster lane. Publishing happens only on success, and that lane's
    ``list_files``/``read_file`` answer from the object store -- so a failed import left
    ``import.log`` and ``import.json`` on a pod's scratch, and ``/results/<id>`` answered
    *no directory* for the one campaign whose files anybody wanted to open. The card showed
    the refusal and nothing behind it could be read: the "worst of both" the keep-the-tree
    fix was written against, still standing on the lane where campaigns actually run.

    The local lane cannot catch it -- publishing is a no-op there and the scratch *is* the
    durable home -- so the lane under test drops its tree the way a real one does.
    """
    import shutil

    published = {}

    def _publish_and_drop(self, campaign_id, target):
        published[campaign_id] = (Path(target) / "_execution" / "import.log").read_text(
            encoding="utf-8")
        shutil.rmtree(target, ignore_errors=True)

    def _boom(*_a, **_k):
        raise OSError("disk went away mid-extraction")

    monkeypatch.setattr("robovast.service.ingest.extract_archive", _boom)
    monkeypatch.setattr(type(service), "_publish_failed_import", _publish_and_drop)

    ref = service.import_campaign(ImportCampaignRequest(
        archive_path=str(_archive(tmp_path))))
    status = _wait_done(service, ref.campaign_id)

    assert status.phase == Phase.FAILED
    assert ref.campaign_id in published, "the failure must be published, not only recorded"
    # Published *after* the log handler is closed and the outcome written, or what goes up
    # is a truncated account of a failure -- the one file that exists to explain it.
    assert "disk went away mid-extraction" in published[ref.campaign_id]


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
        service._campaign_dir(ref.campaign_id))  # pylint: disable=protected-access
    assert on_disk.runs.total > 0, "fixture must carry runs for this to test anything"

    assert status.postprocessed is True
    assert status.runs.total == on_disk.runs.total
    assert status.runs.completed == on_disk.runs.completed
    assert status.mode == on_disk.mode


def test_the_report_survives_a_lane_that_drops_its_scratch_copy(service, tmp_path,
                                                               monkeypatch):
    """A lane whose durable home is elsewhere DELETES the tree once it is published.

    The cluster service does exactly that -- a multi-gigabyte campaign left on a pod's
    scratch is how the pod fills its disk -- so anything that reads the campaign after
    publishing reads a directory that is gone and reconstructs zeros. That is the empty
    report this whole adoption exists to prevent, and the local lane cannot catch it:
    publishing is a no-op there, so the tree survives whatever the order.
    """
    import shutil

    dropped = []

    def _publish_and_drop(self, campaign_id, target):
        dropped.append(campaign_id)
        shutil.rmtree(target, ignore_errors=True)

    monkeypatch.setattr(type(service), "_publish_imported_campaign", _publish_and_drop)
    ref = service.import_campaign(ImportCampaignRequest(
        archive_path=str(_archive(tmp_path, postprocessed=True, runs=26))))
    status = _wait_done(service, ref.campaign_id)

    assert dropped == [ref.campaign_id], "the lane under test must have dropped the tree"
    assert status.runs.total == 26
    assert status.postprocessed is True


def test_a_raw_import_also_reports_its_run_tally(service, tmp_path, monkeypatch):
    """The raw path recorded the postprocessing verdict but never the run tally.

    Both arrival paths go through the same adoption, so neither can report an empty
    campaign; this is the half that a fix aimed only at the postprocessed branch misses.
    """
    monkeypatch.setattr(type(service), "_postprocess_campaign",
                        lambda self, cid, d, **k: (True, "ok"))
    ref = service.import_campaign(ImportCampaignRequest(
        archive_path=str(_archive(tmp_path, runs=26))))
    status = _wait_done(service, ref.campaign_id)
    assert status.runs.total == 26
