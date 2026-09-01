# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Which campaigns a `search_run_logs` call actually searches.

Comparing a known set -- a seed sweep, a before/after pair -- is the question this tool
exists for, and it could only be asked as a regex that happened to match exactly those
ids and nothing else. ``extra_campaign_ids`` names them instead; these lock what that
means when it meets ``max_campaigns`` and a pattern.
"""

import asyncio
import sqlite3

import pytest

from robovast.mcp_server import service_access
from robovast.mcp_server.plugins import run_logs

_A = "seed-a-2026-07-16-120000"
_B = "seed-b-2026-07-16-130000"
_C = "seed-c-2026-07-16-140000"


def _write_campaign(cdir) -> None:
    (cdir / "_execution").mkdir(parents=True)
    db = sqlite3.connect(cdir / "_execution" / "data.db")
    db.execute(
        "CREATE TABLE run_log (config_name TEXT, run_id INTEGER, sim_time REAL, "
        "wall_ts REAL, time_source TEXT, in_window INTEGER, container TEXT, node TEXT, "
        "source TEXT, level TEXT, severity TEXT, message TEXT)")
    db.execute("CREATE TABLE scenario_timestamps (config_name TEXT, run_id INTEGER, "
               "wall_ts REAL)")
    db.execute("CREATE TABLE runs (config_name TEXT, run_id INTEGER, passed INTEGER, "
               "status TEXT, clock_map_source TEXT)")
    db.execute(
        "INSERT INTO run_log VALUES ('cfg-a', 0, 1.0, 1.0, 'clock', 1, 'main', 'n1', "
        "'stdout', 'ERROR', 'error', 'boom')")
    db.commit()
    db.close()


@pytest.fixture
def campaigns(tmp_path, monkeypatch):
    """Three campaigns with a merged run log each, reached over the local lane."""
    monkeypatch.setenv("ROBOVAST_WORKSPACES_ROOT", str(tmp_path / "workspaces"))
    for name in (_A, _B, _C):
        _write_campaign(tmp_path / "results" / name)
    monkeypatch.setattr(service_access, "service_client", lambda: None)
    return tmp_path


def _searched(**kwargs) -> list:
    result = asyncio.run(run_logs.search_run_logs(**kwargs))
    assert result["campaigns_skipped"] == [], result["campaigns_skipped"]
    return [c["campaign_id"] for c in result["campaigns"]]


def test_a_named_set_is_searched_without_contriving_a_regex(campaigns):
    assert _searched(campaign_id=_A, extra_campaign_ids=[_B, _C]) == [_A, _B, _C]


def test_max_campaigns_does_not_cap_campaigns_named_by_hand(campaigns):
    """It bounds a regex's fan-out -- the cost nobody can see coming, since a pattern's
    match count is not knowable when it is written. A list is knowable, and dropping the
    third of three ids someone enumerated answers a question they did not ask."""
    assert _searched(campaign_id=_A, extra_campaign_ids=[_B, _C],
                     max_campaigns=1) == [_A, _B, _C]


def test_a_named_campaign_survives_a_regex_that_fills_the_cap(campaigns):
    """The cap applies to the pattern's matches, then the named ones are added. Appending
    first and truncating after dropped exactly the campaign the caller was surest about."""
    searched = _searched(campaign_id="^seed-a-", campaign_regex=True,
                         extra_campaign_ids=[_C], max_campaigns=1)
    assert sorted(searched) == [_A, _C]


def test_a_campaign_both_matched_and_named_is_attached_once(campaigns):
    """A duplicate would spend two of the query's attach slots on one campaign, and
    report it twice. (Order here is the listing's -- newest first -- not this test's.)"""
    searched = _searched(campaign_id="^seed-", campaign_regex=True,
                         extra_campaign_ids=[_A])
    assert sorted(searched) == [_A, _B, _C]
    assert len(searched) == len(set(searched))


def test_a_regex_that_overflows_the_cap_still_says_so(campaigns):
    """The note is about the pattern, and naming campaigns beside it must not silence it.

    ``_A`` is the OLDEST, so a cap of one on a newest-first listing never reaches it: it
    is here only because it was named.
    """
    result = asyncio.run(run_logs.search_run_logs(
        campaign_id="^seed-", campaign_regex=True, extra_campaign_ids=[_A],
        max_campaigns=1))
    assert "2 of 3 matching campaign(s) not searched" in result["note"]
    assert sorted(c["campaign_id"] for c in result["campaigns"]) == [_A, _C]


def test_a_named_set_larger_than_one_query_can_attach_is_batched(campaigns, tmp_path):
    """More campaigns than SQLite will attach at once is a batching job, not a refusal.

    The set a caller names is not bounded by what one query can hold, and the two limits
    meet here: past the attach budget the tool must split the search and still search
    every campaign.
    """
    extra = []
    for i in range(run_logs._MAX_ATTACHED + 3):  # noqa: SLF001
        name = f"batch-{i}-2026-07-16-150000"
        _write_campaign(tmp_path / "results" / name)
        extra.append(name)
    assert len(extra) > run_logs._MAX_ATTACHED  # noqa: SLF001 - the point of the test
    assert sorted(_searched(campaign_id=_A, extra_campaign_ids=extra)) == sorted([_A, *extra])
