# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""How many campaigns one query may attach, and what happens past that.

Each extra campaign takes TWO of SQLite's attached-schema slots (its ``data.db`` under
the alias, its ``campaign.db`` under ``<alias>_campaign``) and one slot is already spent
on the primary campaign's own store. Attaching more than fits used to fail silently --
``_attach_ro`` logged at debug level -- so the query came back "no such table: c6.runs"
and the campaign looked empty rather than unattached.
"""

import sqlite3

import pytest

from robovast.results_processing.data_query import (DataQueryError, max_extra_campaigns,
                                                    query_data_db)


def _campaign(root, name):
    """A campaign directory with both databases, each holding one row."""
    cdir = root / name
    (cdir / "_execution").mkdir(parents=True)
    for path, table in ((cdir / "_execution" / "data.db", "runs"),
                        (cdir / "campaign.db", "campaign")):
        db = sqlite3.connect(path)
        db.execute(f"CREATE TABLE {table} (run_id INTEGER, config_name TEXT, x REAL)")
        db.execute(f"INSERT INTO {table} VALUES (0, 'cfg', 1.0)")
        db.commit()
        db.close()
    return cdir


def _extras(root, count):
    return {f"c{i}": _campaign(root, f"extra-{count}-{i}") for i in range(1, count + 1)}


def test_budget_is_derived_from_sqlite_not_assumed():
    """Four on a stock build: (10 attached slots - 1 for the primary) // 2 per campaign."""
    with sqlite3.connect(":memory:") as conn:
        limit = conn.getlimit(sqlite3.SQLITE_LIMIT_ATTACHED)
    assert max_extra_campaigns() == (limit - 1) // 2


def test_every_attached_campaign_is_queryable_at_the_budget(tmp_path):
    """Both schemas of every extra campaign resolve -- no silently dropped store."""
    primary = _campaign(tmp_path, "primary")
    count = max_extra_campaigns()
    extras = _extras(tmp_path, count)
    sql = "SELECT " + ", ".join(
        f"(SELECT COUNT(*) FROM c{i}.runs) AS d{i}, "
        f"(SELECT COUNT(*) FROM c{i}_campaign.campaign) AS s{i}"
        for i in range(1, count + 1))
    row = query_data_db(primary, sql, 10, extra_dirs=extras)["rows"][0]
    assert all(value == 1 for value in row.values())


def test_over_the_budget_is_refused_by_name(tmp_path):
    """Refused up front, saying how many fit -- not left to fail as a missing table."""
    primary = _campaign(tmp_path, "primary")
    count = max_extra_campaigns() + 1
    extras = _extras(tmp_path, count)
    sql = "SELECT " + ", ".join(f"(SELECT COUNT(*) FROM c{i}.runs) AS d{i}"
                                for i in range(1, count + 1))
    with pytest.raises(DataQueryError) as excinfo:
        query_data_db(primary, sql, 10, extra_dirs=extras)
    message = str(excinfo.value)
    assert f"at most {max_extra_campaigns()}" in message
    assert "no such table" not in message


def test_run_logs_batches_within_the_budget():
    """The log search's batch size is the budget, not SQLITE_LIMIT_ATTACHED itself."""
    from robovast.mcp_server.plugins import run_logs
    assert run_logs._MAX_ATTACHED == max_extra_campaigns()  # noqa: SLF001
