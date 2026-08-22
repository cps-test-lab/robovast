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

import tarfile
import time
from pathlib import Path

import pytest

from robovast.client.status import Phase
from robovast.service.interface import ImportCampaignRequest
from robovast.service.local_transport import LocalTransport

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "historic_campaigns"
_SOURCE = _FIXTURES / "v1-campaign-2025-03-04-101500"


def _archive(tmp_path, *, postprocessed=False, name="camp.tar.gz") -> Path:
    """A campaign archive, raw or postprocessed, built from the historic fixture."""
    import shutil
    staged = tmp_path / "staged" / _SOURCE.name
    shutil.copytree(_SOURCE, staged)
    (staged / "_execution").mkdir(exist_ok=True)
    if postprocessed:
        (staged / "_execution" / "data.db").write_bytes(b"")
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
    return transport


def _wait_done(service, campaign_id, timeout=30.0):
    """Block until the tracked entry leaves the live phases, or fail loudly."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        entry = service._campaigns.get(campaign_id)  # pylint: disable=protected-access
        if entry is not None and entry.state.snapshot().phase in (
                Phase.FINISHED, Phase.FAILED):
            return entry.state.snapshot()
        time.sleep(0.05)
    raise AssertionError(f"import of {campaign_id} did not finish within {timeout}s")


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
    inspectable, and removable with ``vast results delete``.
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
