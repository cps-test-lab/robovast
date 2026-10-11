# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""What a summarized `search_run_logs` may claim about its own completeness.

A summary's value is its counts, so whether they cover every matching row or only the
first of them is the one thing a reader cannot check for themselves. The scan cap is per
campaign, and so the verdict has to be: the sum across campaigns is trivially available and
is the wrong number to compare the cap against.
"""

import pytest
import yaml

from robovast.mcp_server import service_access
from robovast.mcp_server.plugins import run_logs
from tests.robovast_data.conftest import write_store

_CAMPAIGNS = ["camp-2026-07-16-120000", "camp-2026-07-16-130000"]

#: Matching rows written per campaign. Two campaigns of four, against the cap of six
#: below: neither campaign reaches the cap, while their sum passes it.
_ROWS_PER_CAMPAIGN = 4
_SCAN_CAP = 6

_TEST_XML = ('<testsuite errors="0" failures="0" tests="1"><testcase time="50"><properties>'
             '<property name="start_time" value="99.0"/></properties></testcase></testsuite>')


def _write_campaign(cdir, rows: int) -> None:
    """One campaign on disk whose one run's job logged *rows* error lines, then a verdict.

    The verdict is logged last, by scenario-execution's own logger, so ``scenario_timestamps``
    has a row and ``hide_shutdown`` -- on by default -- trims nothing that is counted here.
    """
    (cdir / "_transient").mkdir(parents=True)
    job = cdir / "_jobs" / "job-0"
    (job / "logs").mkdir(parents=True)
    lines = [f"[ERROR] [{100.0 + i}] [n1]: boom" for i in range(rows)]
    lines.append(f"[INFO] [{100.0 + rows}] [scenario_execution_ros]: "
                 "Scenario 'trial' succeeded.")
    (job / "logs" / "system.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    run = cdir / "cfg-a" / "0"
    run.mkdir(parents=True)
    (run / "test.xml").write_text(_TEST_XML, encoding="utf-8")
    (cdir / "_transient" / "job_links.yaml").write_text(
        yaml.safe_dump({"cfg-a/0/job": "../../_jobs/job-0"}), encoding="utf-8")
    write_store(cdir, {"cfg-a": {"runs": {0: "passed"}}})


@pytest.fixture
def campaigns(tmp_path, monkeypatch):
    """Two campaigns' logs under one results root, reached through a service stand-in.

    They share ``cfg-a``/run 0 on purpose: those are the keys a query spanning both would
    collide on, and nothing about the shape of the answer would say so.
    """
    monkeypatch.setenv("ROBOVAST_WORKSPACES_ROOT", str(tmp_path / "workspaces"))
    for name in _CAMPAIGNS:
        _write_campaign(tmp_path / "results" / name, _ROWS_PER_CAMPAIGN)
    from tests.service.null_service import serving
    service = serving(tmp_path / "results", tmp_path / "workspaces")
    monkeypatch.setattr(service_access, "service_client", lambda: service)
    monkeypatch.setattr(run_logs, "_SUMMARY_SCAN", _SCAN_CAP)
    return tmp_path


def _summary(**kwargs) -> dict:
    # `min_severity` keeps the verdict line out of the counts: it is written to give the
    # run a scenario verdict, not to be one of the matches being counted.
    return run_logs.search_run_logs(
        "^camp-", campaign_regex=True, summarize=True, min_severity="error", **kwargs)


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
    third = tmp_path / "results" / "camp-2026-07-16-140000"
    _write_campaign(third, _SCAN_CAP + 3)
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


def test_a_search_of_one_campaign_does_not_count_the_others_rows(campaigns):
    """Both campaigns hold ``cfg-a``/run 0, so a count that read the other one too would be
    exactly double and look like a perfectly ordinary result."""
    result = run_logs.search_run_logs(
        _CAMPAIGNS[0], summarize=True, min_severity="error")
    assert result["lines_total"] == _ROWS_PER_CAMPAIGN
    assert result["severity_counts"]["error"] == _ROWS_PER_CAMPAIGN
