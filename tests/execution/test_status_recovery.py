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


def test_derives_finished_without_outcome(tmp_path):
    """No durable record: derive an optimistic 'finished' from artifacts."""
    campaign = tmp_path / "camp-2026-01-01-000002"
    campaign.mkdir()
    st = reconstruct_status_from_disk(campaign, expected_total=5)
    assert st.phase == Phase.FINISHED
    assert st.runs.total == 5  # expected_total surfaced when no artifacts counted


# -- registry terminal vocabulary must not drift from the canonical phases ----

def test_registry_terminal_statuses_match_canonical():
    from robovast.mcp_server.campaign_registry import TERMINAL_STATUSES
    assert TERMINAL_STATUSES == frozenset(TERMINAL_PHASES)
    # The drift that used to bite: 'stopped' must be terminal.
    assert "stopped" in TERMINAL_STATUSES
    assert is_terminal("stopped")


def test_dead_local_classifier_prefers_outcome_json(tmp_path):
    """A reaped local campaign (no exit code) is classified as its durable record says."""
    from robovast.mcp_server.plugins.campaign_control import \
        _classify_dead_local_for

    campaign_id = "camp-2026-01-01-000003"
    campaign = tmp_path / campaign_id
    campaign.mkdir()
    write_execution_outcome(
        campaign, Status(phase=Phase.STOPPED, campaign_id=campaign_id))

    classify = _classify_dead_local_for(str(tmp_path))
    # exit_code is None (reaped by a sweep) -> must trust outcome.json, not collapse.
    assert classify({"campaign_id": campaign_id, "exit_code": None}) == Phase.STOPPED
    # A captured exit code still wins (authoritative).
    assert classify({"campaign_id": campaign_id, "exit_code": 0}) == "finished"
