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


# -- terminal vocabulary must not drift: 'stopped' stays terminal -------------

def test_stopped_is_terminal():
    assert "stopped" in {str(p) for p in TERMINAL_PHASES}
    assert is_terminal("stopped")
