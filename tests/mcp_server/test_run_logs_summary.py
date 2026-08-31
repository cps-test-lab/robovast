# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""What a summarized `search_run_logs` may claim about its own completeness.

A summary's value is its counts, so whether they cover every matching row or only the
first of them is the one thing a reader cannot check for themselves. The scan cap is per
campaign, and so the verdict has to be.
"""

import asyncio
import sqlite3

import pytest

from robovast.mcp_server import service_access
from robovast.mcp_server.plugins import run_logs

_CAMPAIGNS = ["camp-2026-07-16-120000", "camp-2026-07-16-130000"]

#: Matching rows written per campaign. Two campaigns of four, against the cap of six
#: below: neither campaign reaches the cap, while their sum passes it.
_ROWS_PER_CAMPAIGN = 4
_SCAN_CAP = 6


def _write_campaign(cdir, rows: int) -> None:
    (cdir / "_execution").mkdir(parents=True)
    db = sqlite3.connect(cdir / "_execution" / "data.db")
    db.execute(
        "CREATE TABLE run_log (config_name TEXT, run_id INTEGER, sim_time REAL, "
        "wall_ts REAL, time_source TEXT, in_window INTEGER, container TEXT, node TEXT, "
        "source TEXT, level TEXT, severity TEXT, message TEXT)")
    db.execute("CREATE TABLE scenario_timestamps (config_name TEXT, run_id INTEGER, "
               "wall_ts REAL)")
    db.executemany(
        "INSERT INTO run_log VALUES ('cfg-a', 0, ?, ?, 'clock', 1, 'main', 'n1', "
        "'stdout', 'ERROR', 'error', 'boom')",
        [(float(i), float(i)) for i in range(rows)])
    db.commit()
    db.close()


@pytest.fixture
def campaigns(tmp_path, monkeypatch):
    """Two campaigns with a merged run log each, reached over the local lane."""
    monkeypatch.setenv("ROBOVAST_WORKSPACES_ROOT", str(tmp_path / "workspaces"))
    for name in _CAMPAIGNS:
        _write_campaign(tmp_path / "results" / name, _ROWS_PER_CAMPAIGN)
    monkeypatch.setattr(service_access, "service_client", lambda: None)
    monkeypatch.setattr(run_logs, "_SUMMARY_SCAN", _SCAN_CAP)
    return tmp_path


def _summary(**kwargs) -> dict:
    return asyncio.run(run_logs.search_run_logs(
        "^camp-", campaign_regex=True, summarize=True, **kwargs))


def test_campaigns_read_whole_are_not_reported_as_truncated(campaigns):
    """The cap applies per campaign, so a sum across campaigns cannot decide it: two
    campaigns read completely were told their counts covered only a prefix."""
    result = _summary()
    assert result["lines_total"] == 2 * _ROWS_PER_CAMPAIGN
    assert result["severity_counts"]["error"] == 2 * _ROWS_PER_CAMPAIGN
    assert "truncated" not in result
    assert "counts cover" not in result["note"]


def test_a_campaign_that_reaches_the_cap_is_named(campaigns, tmp_path):
    """And named, not just counted: which campaign's numbers are partial decides where
    a reader looks next."""
    _write_campaign(tmp_path / "results" / "camp-2026-07-16-140000", _SCAN_CAP + 3)
    result = _summary(max_campaigns=3)
    assert result["truncated"] is True
    assert "camp-2026-07-16-140000" in result["note"]
    assert "camp-2026-07-16-120000" not in result["note"]


def test_a_summary_does_not_report_line_accounting_it_never_did(campaigns):
    """The filtering happens in SQL, so the text filter's `dropped` and
    `shutdown_dropped` are structurally zero here -- and a `shutdown_dropped: 0` beside a
    note saying the shutdown phase was excluded contradicts it."""
    result = _summary()
    assert "shutdown_dropped" not in result
    assert "dropped" not in result
    assert result["matched_lines"] == 2 * _ROWS_PER_CAMPAIGN
