# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Reading a campaign's own record does not require its measurements.

``open_campaign_db`` is the postprocessed metrics and rightly refuses a campaign with no
``data.db``. A search notebook wants the other half: what was proposed, in which round, and
what it scored — written by the controller as the search runs. Tying that to postprocessing
would make a batch or archive view unavailable while the search is going and on any campaign
that was never postprocessed, which is exactly when watching a search is worth anything.

Before this existed each search notebook opened ``campaign.db`` with a hand-built path and a
raw ``sqlite3.connect``, once per notebook, each with its own guess at where the file was.
"""

import sqlite3

import pytest

from robovast.common.analysis import (CampaignDataError, open_campaign_db,
                                      open_campaign_store)


def _campaign(tmp_path, *, with_data_db=False):
    """A campaign directory holding a campaign.db, and optionally postprocessed results."""
    root = tmp_path / "search-2026-08-25-000000"
    root.mkdir()
    con = sqlite3.connect(root / "campaign.db")
    con.execute("CREATE TABLE campaign (id INTEGER, name TEXT, mode TEXT)")
    con.execute("INSERT INTO campaign VALUES (1, 'search', 'search')")
    con.execute("CREATE TABLE batch (id INTEGER, idx INTEGER)")
    con.executemany("INSERT INTO batch VALUES (?, ?)", [(1, 0), (2, 1)])
    con.commit()
    con.close()
    if with_data_db:
        execution = root / "_execution"
        execution.mkdir()
        sqlite3.connect(execution / "data.db").close()
    return root


def test_the_store_is_readable_without_postprocessing(tmp_path):
    # The point of the function: no _execution/data.db anywhere.
    root = _campaign(tmp_path)
    assert not (root / "_execution").exists()
    con = open_campaign_store(root)
    try:
        assert con.execute("SELECT COUNT(*) FROM batch").fetchone()[0] == 2
    finally:
        con.close()


def test_the_metrics_reader_still_refuses_the_same_campaign(tmp_path):
    # The two are deliberately different: asking for measurements that were never built is an
    # error with a remedy, and this is the campaign that would otherwise fail one table at a
    # time as "no such table".
    root = _campaign(tmp_path)
    with pytest.raises(CampaignDataError) as excinfo:
        open_campaign_db(root)
    assert "data.db" in str(excinfo.value)


def test_tables_are_unqualified(tmp_path):
    # open_campaign_db attaches the same tables under a `campaign.` prefix. A notebook moving
    # between the two would otherwise have to know which one opened its connection.
    con = open_campaign_store(_campaign(tmp_path, with_data_db=True))
    try:
        assert con.execute("SELECT mode FROM campaign").fetchone()[0] == "search"
    finally:
        con.close()


def test_it_is_read_only(tmp_path):
    con = open_campaign_store(_campaign(tmp_path))
    try:
        with pytest.raises(sqlite3.OperationalError):
            con.execute("INSERT INTO batch VALUES (3, 2)")
    finally:
        con.close()


def test_it_is_found_from_a_node_inside_the_campaign(tmp_path):
    # DATA_DIR is whichever node the reader clicked, so the store has to be located the same
    # way everything else in this module locates it — by walking up.
    root = _campaign(tmp_path)
    run_dir = root / "some-config" / "0"
    run_dir.mkdir(parents=True)
    con = open_campaign_store(run_dir)
    try:
        assert con.execute("SELECT COUNT(*) FROM batch").fetchone()[0] == 2
    finally:
        con.close()


def test_a_campaign_without_a_store_says_so(tmp_path):
    # A results tree copied without campaign.db, or produced outside a controller. The message
    # has to name what is absent rather than surface as "no such table: batch".
    root = tmp_path / "copied-2026-08-25-000000"
    (root / "_execution").mkdir(parents=True)
    with pytest.raises(CampaignDataError) as excinfo:
        open_campaign_store(root)
    assert "campaign.db" in str(excinfo.value)
