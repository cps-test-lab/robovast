# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The Results Explorer's own query, run through the data engine on a store that has stages and
on one recorded before them."""

import re
import sqlite3
from pathlib import Path

from robovast.results_processing.data_query import query_data_db

from .test_run_view_stage import _staged

RESULTS_TREE = (Path(__file__).resolve().parents[2] / "frontend" / "ui" / "src" / "lib"
                / "resultsTree.ts")


def _explorer_sql() -> str:
    """``CAMPAIGN_RUNS_SQL`` as the web UI sends it: its string literals, concatenated."""
    text = RESULTS_TREE.read_text()
    body = text[text.index("export const CAMPAIGN_RUNS_SQL ="):]
    body = body[:body.index("\n\n")]
    return "".join(a or b for a, b in re.findall(r"'((?:[^'\\]|\\.)*)'|\"((?:[^\"\\]|\\.)*)\"",
                                                   body.split("=", 1)[1]))


def _before_stages(root: Path) -> Path:
    """The same campaign as a store of schema 16 keeps it until something opens it read-write."""
    _staged(root)
    db = sqlite3.connect(root / "campaign.db")
    db.execute("ALTER TABLE batch DROP COLUMN stage_id")
    db.execute("DROP TABLE stage")
    db.execute("PRAGMA user_version = 16")
    db.commit()
    db.close()
    return root


def test_the_explorer_s_query_reads_each_round_s_stage_and_its_direction(tmp_path):
    root = _staged(tmp_path / "s-2026-01-01-00000000")
    got = query_data_db(root, _explorer_sql(), max_rows=5000)
    rows = got["rows"]
    assert {(r["stage"], r["stage_direction"], r["objective_direction"]) for r in rows} == {
        ("obstacle", "minimize", "maximize")}


def test_the_explorer_s_query_reads_a_store_recorded_before_stages(tmp_path):
    root = _before_stages(tmp_path / "s-2026-01-01-00000000")
    got = query_data_db(root, _explorer_sql(), max_rows=5000)
    rows = got["rows"]
    assert [r["config_name"] for r in rows] == ["cfg-a", "cfg-b"]
    assert {(r["stage"], r["stage_direction"]) for r in rows} == {(None, None)}
