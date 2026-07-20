# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Fixtures for :mod:`robovast.results_processing.data_query` tests.

Builds a minimal on-disk campaign directory (``<dir>/_execution/data.db`` plus an
optional ``<dir>/campaign.db``) so the directory-based query helpers can be
exercised without running the full postprocessing pipeline.
"""

import json
import sqlite3
from pathlib import Path

import pytest


def _write_data_db(campaign_dir: Path) -> None:
    """Create ``_execution/data.db`` with a ``runs`` dim table and one metric table.

    Mirrors the real layout: scalar scenario params become ``param_*`` columns and
    non-scalar params are JSON-encoded strings (see ``_build_runs_table``).
    """
    exec_dir = campaign_dir / "_execution"
    exec_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(exec_dir / "data.db")
    conn.execute(
        "CREATE TABLE runs (config_name TEXT, run_id INTEGER, status TEXT, "
        "passed INTEGER, duration_s REAL, objective REAL, "
        "param_wind REAL, param_waypoints TEXT)"
    )
    rows = [
        ("cfg-a", 0, "passed", 1, 10.0, 1.5, 0.0,
         json.dumps([{"x": 1.0, "y": 2.0}, {"x": 3.0, "y": 4.0}])),
        ("cfg-a", 1, "failed", 0, 12.0, 2.5, 0.0,
         json.dumps([{"x": 1.0, "y": 2.0}])),
        ("cfg-b", 0, "passed", 1, 20.0, 0.5, 5.0,
         json.dumps([{"x": 9.0, "y": 9.0}])),
        ("cfg-b", 1, "passed", 1, 22.0, 0.7, 5.0,
         json.dumps([{"x": 9.0, "y": 9.0}])),
    ]
    conn.executemany("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?)", rows)
    # A per-run metric table joinable on (config_name, run_id).
    conn.execute("CREATE TABLE landing_error (config_name TEXT, run_id INTEGER, error REAL)")
    conn.executemany(
        "INSERT INTO landing_error VALUES (?,?,?)",
        [("cfg-a", 0, 0.10), ("cfg-a", 1, 0.90),
         ("cfg-b", 0, 0.20), ("cfg-b", 1, 0.30)],
    )
    conn.commit()
    conn.close()


def _write_campaign_db(campaign_dir: Path) -> None:
    """Create ``campaign.db`` with a one-row ``campaign`` table (attached as schema)."""
    conn = sqlite3.connect(campaign_dir / "campaign.db")
    conn.execute("CREATE TABLE campaign (id INTEGER PRIMARY KEY, name TEXT, "
                 "config_json TEXT, strategy_state BLOB)")
    conn.execute(
        "INSERT INTO campaign (id, name, config_json, strategy_state) VALUES (?,?,?,?)",
        (1, "demo", json.dumps({"evaluation": {"plots": []}, "execution": {"runs": 2}}),
         b"\x80\x04" + b"\x00" * 4096),  # stand-in for a pickled optimizer blob
    )
    conn.commit()
    conn.close()


@pytest.fixture
def campaign_dir(tmp_path: Path) -> Path:
    """A campaign directory with both ``data.db`` and ``campaign.db`` populated."""
    _write_data_db(tmp_path)
    _write_campaign_db(tmp_path)
    return tmp_path


@pytest.fixture
def campaign_dir_no_data(tmp_path: Path) -> Path:
    """A campaign directory with only ``campaign.db`` (postprocessing not yet run)."""
    _write_campaign_db(tmp_path)
    return tmp_path
