# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""What a summarized `search_run_logs` may claim about its own completeness.

A summary's value is its counts, so whether they cover every matching row or only the
first of them is the one thing a reader cannot check for themselves. The scan cap is per
campaign, and so the verdict has to be.

Every campaign's log now lives in one ``run_log`` table of the central index, which is
what makes this worth pinning rather than obvious: the sum across campaigns is trivially
available and is the wrong number to compare the cap against.

Needs Postgres: set ``ROBOVAST_TEST_PG_DSN`` or these skip.
"""

import asyncio
import os

import pytest

from robovast.mcp_server import service_access
from robovast.mcp_server.plugins import run_logs

DSN = os.environ.get("ROBOVAST_TEST_PG_DSN")

#: Namespaced so a shared test database can hold several suites at once.
SCHEMA = "mcp_logs_test"

_CAMPAIGNS = ["camp-2026-07-16-120000", "camp-2026-07-16-130000"]

#: Matching rows written per campaign. Two campaigns of four, against the cap of six
#: below: neither campaign reaches the cap, while their sum passes it.
_ROWS_PER_CAMPAIGN = 4
_SCAN_CAP = 6

#: The columns of a merged ``run_log``, minus ``campaign_id``/``config_name``/``run_id``:
#: those are the index's context columns and the ingest derives them from the directory.
_HEADER = ("sim_time,wall_ts,time_source,in_window,container,node,source,level,severity,"
           "message")

#: Logged last, and by scenario-execution's own logger, so the ingest derives a
#: ``scenario_timestamps`` row from it. Without a verdict there is no such table and
#: ``hide_shutdown`` -- on by default -- has nothing to correlate against. Its wall stamp is
#: after every line below, so nothing is trimmed and the counts stay the ones under test.
_VERDICT = ("1000.0,1000.0,clock,1,main,scenario_execution,stdout,INFO,other,"
            "Scenario 'trial' succeeded.")


def _write_campaign(cdir, rows: int) -> None:
    """One campaign on disk: a ``run_log.csv`` the ingest turns into indexed rows."""
    # `_execution/` no longer holds a data.db, but its presence is still what marks a
    # directory as a campaign root for the path->campaign_id resolution.
    (cdir / "_execution").mkdir(parents=True)
    run = cdir / "cfg-a" / "0"
    run.mkdir(parents=True)
    lines = [_HEADER]
    lines += [f"{float(i)},{float(i)},clock,1,main,n1,stdout,ERROR,error,boom"
              for i in range(rows)]
    lines.append(_VERDICT)
    (run / "run_log.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def campaigns(tmp_path, monkeypatch):
    """Two campaigns' logs in one index, reached over the local lane.

    They share ``cfg-a``/run 0 on purpose: those are the keys that collide once the logs
    are one table, so a query that forgot ``campaign_id`` reads both and nothing about the
    shape of the answer says so.
    """
    if not DSN:
        pytest.skip("ROBOVAST_TEST_PG_DSN is not set")
    psycopg = pytest.importorskip("psycopg")
    from robovast.common import index_db

    with psycopg.connect(DSN, autocommit=True) as setup:
        for statement in (f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE",
                          "DROP SCHEMA IF EXISTS campaign CASCADE",
                          f"CREATE SCHEMA {SCHEMA}"):
            setup.execute(statement)
    monkeypatch.setenv(index_db.DSN_ENV, f"{DSN} options=-csearch_path={SCHEMA}")
    monkeypatch.setenv("ROBOVAST_WORKSPACES_ROOT", str(tmp_path / "workspaces"))

    for name in _CAMPAIGNS:
        _write_campaign(tmp_path / "results" / name, _ROWS_PER_CAMPAIGN)
        _ingest(tmp_path / "results" / name)
    monkeypatch.setattr(service_access, "service_client", lambda: None)
    monkeypatch.setattr(run_logs, "_SUMMARY_SCAN", _SCAN_CAP)
    yield tmp_path
    with psycopg.connect(DSN, autocommit=True) as teardown:
        teardown.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        teardown.execute("DROP SCHEMA IF EXISTS campaign CASCADE")


def _ingest(root) -> None:
    from robovast.results_processing import campaign_ingest, index_query, index_views

    with index_query.open_index(readonly=False) as conn:
        campaign_ingest.ingest_campaign(conn, str(root), root.name)
        index_views.create_views(conn)


def _summary(**kwargs) -> dict:
    # `min_severity` keeps the verdict line out of the counts: it is written to give the
    # index a scenario verdict, not to be one of the matches being counted.
    return asyncio.run(run_logs.search_run_logs(
        "^camp-", campaign_regex=True, summarize=True, min_severity="error", **kwargs))


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
    _ingest(third)
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
    """The scoping bug the single table makes possible, and which nothing else reveals:
    both campaigns hold ``cfg-a``/run 0, so an unscoped count is exactly double and looks
    like a perfectly ordinary result."""
    result = asyncio.run(run_logs.search_run_logs(
        _CAMPAIGNS[0], summarize=True, min_severity="error"))
    assert result["lines_total"] == _ROWS_PER_CAMPAIGN
    assert result["severity_counts"]["error"] == _ROWS_PER_CAMPAIGN
