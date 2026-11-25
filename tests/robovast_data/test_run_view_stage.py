# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``run_view.stage``: the stage of a search that proposed each run."""

import json
import sqlite3
from pathlib import Path

from robovast.results_processing.data_query import query_data_db

from .conftest import nav_campaign

SEARCH = {"search": {
    "objectives": [{"name": "margin", "direction": "maximize"}],
    "stages": [{"name": "obstacle", "over": ["speed"], "strategy": "boundary",
                "objectives": [{"name": "margin", "direction": "minimize"}]},
               {"name": "decision", "over": ["speed"], "strategy": "grid"}]}}


def _staged(root: Path) -> Path:
    """The fixture campaign as a search whose one batch was proposed by stage 'obstacle'."""
    nav_campaign(root, runs=(("cfg-a", 0), ("cfg-b", 0)))
    db = sqlite3.connect(root / "campaign.db")
    db.execute("UPDATE campaign SET mode = 'search', config_json = ?", (json.dumps(SEARCH),))
    db.execute("INSERT INTO stage (id, campaign_id, idx, name, objective, direction, outcome) "
               "VALUES (1, 1, 0, 'obstacle', 'margin', 'minimize', 'done')")
    db.execute("UPDATE batch SET stage_id = 1")
    db.commit()
    db.close()
    return root


def test_run_view_names_the_stage_that_proposed_each_run(tmp_path):
    root = _staged(tmp_path / "s-2026-01-01-00000000")
    got = query_data_db(root, "SELECT config_name, stage FROM run_view ORDER BY config_name")
    assert got["rows"] == [{"config_name": "cfg-a", "stage": "obstacle"},
                           {"config_name": "cfg-b", "stage": "obstacle"}]
