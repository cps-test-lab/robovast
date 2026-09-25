# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``postprocessed`` must not depend on how long the service has been up.

It is read as "is there derived data here": the web UI gates its Results and Run views on
it. But two paths produced it, and they disagreed.

* the **live** ``ControllerState`` — ``_postprocess`` records ``True`` only when the ``.vast``
  declared ``results_processing.postprocessing`` *entries*, which is the narrower question of
  whether the stored archive is the postprocessed one;
* the **disk recovery** path — derives it from the campaign directory, and says so in its own
  comment: *"postprocessed is a fact about the campaign, not about who last drove it."*

So for as long as the service tracked it, a campaign could report ``False`` and the UI hide
the two views that read its results; after a restart dropped the entry, the same campaign on
the same bytes reported ``True`` and the views appeared. These pin the two answers together.

The evidence itself changed with the move to the central index -- from a finished
``_execution/data.db`` to postprocessing's provenance record -- but what these tests are for
did not: one predicate, and "finished" distinguished from "under way".
"""

import pytest
import yaml

from robovast.common.campaign_data import POSTPROCESSING_RECORD
from robovast.execution.control_server import ControllerState, Phase
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore
from tests.service.null_service import NullService


@pytest.fixture
def svc(tmp_path, monkeypatch):
    store = WorkspaceStore(registry=WorkspaceRegistry(root=str(tmp_path / "ws")))
    transport = NullService(store=store)
    results = tmp_path / "results"
    results.mkdir()
    transport._campaigns_root = lambda: results        # noqa: SLF001
    return transport


CID = "pilot-2026-08-09-120000"


def _record(campaign, entries):
    """Write postprocessing's provenance record with *entries* under it."""
    path = campaign / POSTPROCESSING_RECORD
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"entries": entries}), encoding="utf-8")


def _campaign(svc, *, with_derived_data: bool):
    """A finished campaign on disk, with or without its derived data.

    Named for what it means rather than for the file that used to prove it. It was
    `with_data_db`, and a defence of that name is what this comment used to be -- but a
    parameter named after a retired mechanism is the same trap as a predicate testing for
    one, and this suite exists because that trap keeps being sprung.
    """
    campaign = svc._campaigns_root() / CID              # noqa: SLF001
    (campaign / "_execution").mkdir(parents=True)
    if with_derived_data:
        _record(campaign, [{"output": "poses.csv", "plugin": "rosbags_process"}])
    return campaign


def _track(svc, *, postprocessed: bool):
    """Register a live entry whose state reports *postprocessed*, as the worker leaves it."""
    from robovast.service.service_base import _TrackedCampaign
    state = ControllerState(campaign_id=CID)
    state.set_phase(Phase.FINISHED)
    state.update(postprocessed=postprocessed)
    entry = _TrackedCampaign(CID, str(svc._campaigns_root()), state)   # noqa: SLF001
    with svc._lock:                                                  # noqa: SLF001
        svc._campaigns[CID] = entry                                  # noqa: SLF001
    return entry


def test_a_tracked_campaign_with_derived_data_reports_postprocessed(svc):
    """The bug: the .vast declared no postprocessing entries, so the live state says False —
    but data.db is there, which is what a reader is actually asking about."""
    _campaign(svc, with_derived_data=True)
    _track(svc, postprocessed=False)
    assert svc.get_status(CID).postprocessed is True


def test_the_listing_agrees_with_the_status(svc):
    """The web UI gates its buttons on the *listing*, so the two must not diverge."""
    _campaign(svc, with_derived_data=True)
    _track(svc, postprocessed=False)
    assert svc._summary_for(CID).postprocessed is True        # noqa: SLF001
    assert svc.get_status(CID).postprocessed is True


def test_a_restart_does_not_change_the_answer(svc):
    """Same campaign, same bytes, entry dropped — the disk path already said True, and the
    tracked path now agrees. This is the inconsistency that made the buttons come and go."""
    _campaign(svc, with_derived_data=True)
    _track(svc, postprocessed=False)
    tracked = svc.get_status(CID).postprocessed
    with svc._lock:                                          # noqa: SLF001
        svc._campaigns.pop(CID)                              # noqa: SLF001
    assert tracked is svc.get_status(CID).postprocessed is True


def test_without_derived_data_it_stays_false(svc):
    """Only ``data.db`` promotes it, so a campaign that produced none is still not
    postprocessed — the views would open on nothing."""
    _campaign(svc, with_derived_data=False)
    _track(svc, postprocessed=False)
    assert svc.get_status(CID).postprocessed is False


def test_a_state_that_already_says_true_is_left_alone(svc):
    """Promotion is one-way: what ``_postprocess`` recorded is never contradicted, so the
    archive decision that reads it is unaffected."""
    _campaign(svc, with_derived_data=False)
    _track(svc, postprocessed=True)
    assert svc.get_status(CID).postprocessed is True


# -- work in progress is not finished work -----------------------------------
#
# The rule was once "``data.db`` exists", and existence was not evidence that the build had
# finished: the builder unlinked any previous database and then connected, so the file was
# there from 0%. A 9 GB build across 1870 runs reported ``postprocessed: true`` for the twenty
# minutes it was being written — and because the web UI gates its Results views on this flag,
# it offered them over a database still being appended to. SQLite's WAL sidecars were what
# told the two apart.
#
# Derived data now goes to the central index, so there is no file to stat and no sidecar to
# read. What replaces both is *ordering*: postprocessing writes its provenance record last,
# after the ingest, so a run still under way has written none. These pin that — the predicate
# must stay one that a half-finished campaign cannot satisfy, whatever it reads.
#
# Querying the index instead was rejected: it would make a campaign's status depend on a
# service being up, so every campaign would read as un-postprocessed whenever the index was
# down. That is a statement about the index, not about the campaign.


def _building(svc):
    """A campaign whose postprocessing is under way: no record written yet."""
    return _campaign(svc, with_derived_data=False)


def test_a_campaign_still_being_postprocessed_is_not_postprocessed(svc):
    _building(svc)
    _track(svc, postprocessed=False)
    assert svc.get_status(CID).postprocessed is False


def test_the_listing_agrees_while_the_build_runs(svc):
    """The UI gates its Results views on the *listing*, so it must not offer them mid-build."""
    _building(svc)
    _track(svc, postprocessed=False)
    assert svc._summary_for(CID).postprocessed is False       # noqa: SLF001


def test_it_flips_once_postprocessing_writes_its_record(svc):
    """The record is written after the ingest, which is the moment the campaign becomes
    readable as a whole. That ordering is the whole guarantee, so this is the transition
    every completed postprocessing run makes."""
    campaign = _building(svc)
    _track(svc, postprocessed=False)
    assert svc.get_status(CID).postprocessed is False
    _record(campaign, [{"output": "poses.csv", "plugin": "rosbags_process"}])
    assert svc.get_status(CID).postprocessed is True


def test_a_record_declaring_no_entries_is_postprocessed(svc):
    """A campaign with no steps of its own writes a record with no entries once its tables are
    built; that pass finished, so it is postprocessed. A failed pass is told apart by its
    recorded error (below), not by an empty record."""
    campaign = _campaign(svc, with_derived_data=False)
    _record(campaign, [])
    _track(svc, postprocessed=False)
    assert svc.get_status(CID).postprocessed is True


def test_a_recorded_failure_is_never_promoted(svc):
    """A step can fail after other steps have already derived data, so a record with entries
    in it does not mean the run succeeded. Promoting on it would put "results are ready" over
    the top of the ``postprocessing_error`` that says they are not."""
    _campaign(svc, with_derived_data=True)
    entry = _track(svc, postprocessed=False)
    entry.state.update(postprocessing_error="conversion failed")
    assert svc.get_status(CID).postprocessed is False


def test_a_restart_mid_build_does_not_change_the_answer(svc):
    """The same pairing the tests above make for a finished campaign: one predicate, so the
    tracked and the disk-recovered answers cannot drift apart while the work is under way."""
    _building(svc)
    _track(svc, postprocessed=False)
    tracked = svc.get_status(CID).postprocessed
    with svc._lock:                                           # noqa: SLF001
        svc._campaigns.pop(CID)                               # noqa: SLF001
    assert tracked is svc.get_status(CID).postprocessed is False


# -- a replay reads as postprocessed, and yields what the live watcher wrote -----------------
#
# A campaign's tables are written twice over: as the run goes, by the watcher that follows
# its recordings and derives its job's tables whole (:mod:`robovast_decode.live`), and again
# by a replay, which clears them and builds every table the records can give. Both are the
# same decoder over the same records, so the rows are the same -- and a replayed campaign is
# a postprocessed one.

_VAST = """\
version: 5
execution:
  containers: {}
results_processing:
  postprocessing:
    - rosbags_tf_to_csv: {frames: all, require: [base_link, robot_gt]}
    - rosbags_rosout_to_csv
    - rosbags_clock_to_csv
"""
_TABLES = ["poses", "rosout", "run_log", "run_clock", "scenario_timestamps"]


def _sorted_rows(table):
    import json
    from robovast_decode.tables import CONTEXT_COLUMNS
    table = table.drop_columns([c for c in CONTEXT_COLUMNS if c in table.column_names])
    return sorted(json.dumps(r, sort_keys=True, default=str) for r in table.to_pylist())


def test_a_replayed_campaign_reads_as_postprocessed_with_the_live_tables(svc):
    import pyarrow.parquet as pq

    from robovast.results_processing.campaign_tables import write_decoder_config
    from robovast.results_processing.postprocessing import run_postprocessing
    from robovast_decode.live import Watcher
    from tests.results_processing.conftest import write_campaign_db
    from tests.robovast_decode.conftest import make_campaign

    root = svc._campaigns_root() / CID                     # noqa: SLF001
    make_campaign(root, verdict=False)
    write_campaign_db(root, CID)
    (root / "_config").mkdir()
    (root / "_config" / "campaign.vast").write_text(_VAST)
    write_decoder_config(str(root), str(root / "_config" / "campaign.vast"))
    (root / "_jobs" / "job-0" / "logs" / "system.log").write_text(
        "[INFO] [1780000000.0] [scenario_execution_ros]: Executing scenario 'nav-0'\n"
        "[INFO] [1780000001.0] [scenario_execution_ros]: Scenario 'nav-0' succeeded.\n")

    # The live side: the watcher follows the run and finalises it at the verdict.
    watcher = Watcher(str(root))
    watcher.demand("cfg/0", _TABLES)
    assert set(_TABLES) <= watcher.following("cfg/0")
    (root / "cfg" / "0" / "test.xml").write_text("<testsuite/>")
    watcher.changed([str(root / "cfg" / "0" / "test.xml")])
    assert watcher.following("cfg/0") == set()
    live = {t: pq.read_table(root / ".cache" / "tables" / t / "cfg" / "0.parquet")
            for t in _TABLES}
    assert svc.get_status(CID).postprocessed is False, "tables alone are not a postprocess"

    ok, message = run_postprocessing(str(root.parent), campaign=CID, replay=True,
                                     skip_metadata=True)
    assert ok, message
    assert svc.get_status(CID).postprocessed is True
    for table, rows in live.items():
        replayed = pq.read_table(root / ".cache" / "tables" / table / "cfg" / "0.parquet")
        assert replayed.schema == rows.schema, table
        assert _sorted_rows(replayed) == _sorted_rows(rows), table
