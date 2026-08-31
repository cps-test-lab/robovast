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
from robovast.service.local_transport import LocalTransport
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore


@pytest.fixture
def svc(tmp_path, monkeypatch):
    store = WorkspaceStore(registry=WorkspaceRegistry(root=str(tmp_path / "ws")))
    transport = LocalTransport(store=store)
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


def _campaign(svc, *, with_data_db: bool):
    """A finished campaign on disk, with or without its derived data.

    The parameter keeps its name because it is what these tests mean -- "did postprocessing
    leave derived data" -- even though the thing on disk that proves it is now the provenance
    record rather than the SQLite file it names.
    """
    campaign = svc._campaigns_root() / CID              # noqa: SLF001
    (campaign / "_execution").mkdir(parents=True)
    if with_data_db:
        _record(campaign, [{"output": "poses.csv", "plugin": "rosbags_process"}])
    return campaign


def _track(svc, *, postprocessed: bool):
    """Register a live entry whose state reports *postprocessed*, as the worker leaves it."""
    from robovast.service.local_transport import _LocalCampaign
    state = ControllerState(campaign_id=CID)
    state.set_phase(Phase.FINISHED)
    state.update(postprocessed=postprocessed)
    entry = _LocalCampaign(CID, str(svc._campaigns_root()), state)   # noqa: SLF001
    with svc._lock:                                                  # noqa: SLF001
        svc._campaigns[CID] = entry                                  # noqa: SLF001
    return entry


def test_a_tracked_campaign_with_derived_data_reports_postprocessed(svc):
    """The bug: the .vast declared no postprocessing entries, so the live state says False —
    but data.db is there, which is what a reader is actually asking about."""
    _campaign(svc, with_data_db=True)
    _track(svc, postprocessed=False)
    assert svc.get_status(CID).postprocessed is True


def test_the_listing_agrees_with_the_status(svc):
    """The web UI gates its buttons on the *listing*, so the two must not diverge."""
    _campaign(svc, with_data_db=True)
    _track(svc, postprocessed=False)
    assert svc._summary_for(CID).postprocessed is True        # noqa: SLF001
    assert svc.get_status(CID).postprocessed is True


def test_a_restart_does_not_change_the_answer(svc):
    """Same campaign, same bytes, entry dropped — the disk path already said True, and the
    tracked path now agrees. This is the inconsistency that made the buttons come and go."""
    _campaign(svc, with_data_db=True)
    _track(svc, postprocessed=False)
    tracked = svc.get_status(CID).postprocessed
    with svc._lock:                                          # noqa: SLF001
        svc._campaigns.pop(CID)                              # noqa: SLF001
    assert tracked is svc.get_status(CID).postprocessed is True


def test_without_derived_data_it_stays_false(svc):
    """Only ``data.db`` promotes it, so a campaign that produced none is still not
    postprocessed — the views would open on nothing."""
    _campaign(svc, with_data_db=False)
    _track(svc, postprocessed=False)
    assert svc.get_status(CID).postprocessed is False


def test_a_state_that_already_says_true_is_left_alone(svc):
    """Promotion is one-way: what ``_postprocess`` recorded is never contradicted, so the
    archive decision that reads it is unaffected."""
    _campaign(svc, with_data_db=False)
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
    return _campaign(svc, with_data_db=False)


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


def test_a_record_declaring_no_entries_is_not_postprocessed(svc):
    """The record is written even when every step failed or none was configured. Reading its
    mere presence as success would promote a campaign with no derived data at all — the one
    direction of error a reader cannot detect by looking."""
    campaign = _campaign(svc, with_data_db=False)
    _record(campaign, [])
    _track(svc, postprocessed=False)
    assert svc.get_status(CID).postprocessed is False


def test_a_recorded_failure_is_never_promoted(svc):
    """A step can fail after other steps have already derived data, so a record with entries
    in it does not mean the run succeeded. Promoting on it would put "results are ready" over
    the top of the ``postprocessing_error`` that says they are not."""
    _campaign(svc, with_data_db=True)
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
