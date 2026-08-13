# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Phase B: one canonical Status-from-disk reconstruction + no terminal-vocab drift."""

from pathlib import Path

from robovast.common.campaign_data import write_execution_outcome
from robovast.execution.control_server import (TERMINAL_PHASES, Phase, Status,
                                               is_terminal)
from robovast.execution.status_recovery import reconstruct_status_from_disk


def test_missing_dir_is_unknown(tmp_path):
    st = reconstruct_status_from_disk(tmp_path / "camp-does-not-exist")
    assert st.phase == Phase.UNKNOWN


def test_outcome_json_wins(tmp_path):
    """The durable terminal record is preferred over any derived 'finished'."""
    campaign = tmp_path / "camp-2026-01-01-000000"
    campaign.mkdir()
    write_execution_outcome(
        campaign, Status(phase=Phase.FAILED, campaign_id=campaign.name,
                         error="boom"))
    st = reconstruct_status_from_disk(campaign)
    assert st.phase == Phase.FAILED
    assert st.error == "boom"


def test_stopped_outcome_survives_reconstruction(tmp_path):
    """A cooperatively-stopped campaign reconstructs as 'stopped', not 'finished'."""
    campaign = tmp_path / "camp-2026-01-01-000001"
    campaign.mkdir()
    write_execution_outcome(
        campaign, Status(phase=Phase.STOPPED, campaign_id=campaign.name))
    assert reconstruct_status_from_disk(campaign).phase == Phase.STOPPED


def test_incomplete_artifacts_without_outcome_are_crashed(tmp_path):
    """No durable record and missing verdicts: the campaign did not finish.

    This used to derive ``finished`` regardless, which is what a campaign looks like
    after its service is restarted out from under it — jobs still running, most runs
    without a verdict yet. Reported as finished, a reader (and a waiter) stops looking.
    """
    campaign = tmp_path / "camp-2026-01-01-000002"
    campaign.mkdir()
    st = reconstruct_status_from_disk(campaign, expected_total=5)
    assert st.phase == Phase.CRASHED
    assert st.runs.total == 5  # expected_total surfaced when no artifacts counted
    # Nothing on disk delivered a verdict, so nothing may be reported as passing:
    # the five runs are resultless, not silently complete.
    assert (st.runs.completed, st.runs.failed, st.runs.no_result) == (0, 0, 5)


def test_complete_artifacts_without_outcome_are_finished(tmp_path):
    """A full set of verdicts *is* evidence of an ending, so it still derives one.

    This is the campaign that predates the durable record, or whose record was never
    uploaded — it plainly ran to completion, and must not be relabelled crashed.
    """
    campaign = tmp_path / "camp-2026-01-01-000005"
    campaign.mkdir()
    _store_with_runs(campaign, {"passed": 3, "failed": 1})
    st = reconstruct_status_from_disk(campaign)
    assert st.phase == Phase.FINISHED
    assert (st.runs.completed, st.runs.failed, st.runs.no_result) == (4, 1, 0)


def test_a_non_terminal_record_is_crashed(tmp_path):
    """The finish tail journals a record *before* the campaign ends on the lane whose
    worker owns the ending. Reconstruction only runs when nothing is driving the
    campaign, so such a record means the driver died mid-flight — reporting its phase
    verbatim would block every waiter on a campaign nobody will advance."""
    campaign = tmp_path / "camp-2026-01-01-000006"
    campaign.mkdir()
    write_execution_outcome(
        campaign, Status(phase=Phase.FINISHING, campaign_id=campaign.name))
    assert reconstruct_status_from_disk(campaign).phase == Phase.CRASHED


# -- the run tally comes from the run table, not from the journal --------------

def _store_with_runs(campaign: Path, statuses: dict[str, int]) -> None:
    """Write a minimal ``campaign.db`` whose ``run`` rows carry *statuses*.

    Only the columns :func:`read_run_counts` reads are needed — it aggregates
    ``run.status`` alone — so this stays independent of the full store schema.
    """
    import sqlite3
    with sqlite3.connect(campaign / "campaign.db") as conn:
        conn.execute("CREATE TABLE run (id INTEGER PRIMARY KEY, status TEXT)")
        conn.executemany("INSERT INTO run (status) VALUES (?)",
                         [(s,) for s, n in statuses.items() for _ in range(n)])


def test_run_table_supersedes_a_stale_outcome_tally(tmp_path):
    """A journal claiming no failures cannot outvote the runs that failed.

    The regression: an ``outcome.json`` written before the controller tallied failing
    trials reports ``failed: 0`` for a campaign whose trials failed, and every reader
    of the status then presents it as clean.
    """
    campaign = tmp_path / "camp-2026-01-01-000003"
    campaign.mkdir()
    write_execution_outcome(
        campaign, Status(phase=Phase.FINISHED, campaign_id=campaign.name,
                         runs={"completed": 80, "total": 80, "failed": 0}))
    _store_with_runs(campaign, {"passed": 31, "failed": 47, "error": 2})
    st = reconstruct_status_from_disk(campaign)
    assert st.phase == Phase.FINISHED          # the journal still owns the phase
    # ...and an errored run is a failure like any other, as CampaignSummary tallies it.
    assert (st.runs.completed, st.runs.total) == (80, 80)
    assert (st.runs.failed, st.runs.no_result) == (49, 0)


def test_run_table_fills_a_derived_tally(tmp_path):
    """No journal at all: the run table still decides passed vs failed."""
    campaign = tmp_path / "camp-2026-01-01-000004"
    campaign.mkdir()
    _store_with_runs(campaign, {"passed": 1, "failed": 1})
    st = reconstruct_status_from_disk(campaign)
    assert st.phase == Phase.FINISHED
    assert (st.runs.completed, st.runs.total, st.runs.failed) == (2, 2, 1)


def test_search_tally_grows_past_the_last_batch(tmp_path):
    """A search journal counts its last batch; the run table counts every one.

    ``total`` has to follow the larger of the two, or the recovered status reports
    more completed runs than it claims to have expected.
    """
    campaign = tmp_path / "camp-2026-01-01-000005"
    campaign.mkdir()
    write_execution_outcome(
        campaign, Status(phase=Phase.FINISHED, campaign_id=campaign.name, mode="search",
                         runs={"completed": 16, "total": 16, "failed": 0}))
    _store_with_runs(campaign, {"passed": 60, "failed": 20})
    st = reconstruct_status_from_disk(campaign)
    assert (st.runs.completed, st.runs.total, st.runs.failed) == (80, 80, 20)


def test_empty_run_table_leaves_the_journal_alone(tmp_path):
    """A store with no run rows says nothing, so the journal's tally stands."""
    campaign = tmp_path / "camp-2026-01-01-000006"
    campaign.mkdir()
    write_execution_outcome(
        campaign, Status(phase=Phase.FINISHED, campaign_id=campaign.name,
                         runs={"completed": 4, "total": 4, "failed": 1}))
    _store_with_runs(campaign, {})
    st = reconstruct_status_from_disk(campaign)
    assert (st.runs.completed, st.runs.total, st.runs.failed) == (4, 4, 1)


# -- terminal vocabulary must not drift: 'stopped' stays terminal -------------

def test_stopped_is_terminal():
    assert "stopped" in {str(p) for p in TERMINAL_PHASES}
    assert is_terminal("stopped")
