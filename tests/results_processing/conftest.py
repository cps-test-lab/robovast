# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Fixtures for :mod:`robovast.results_processing.data_query` tests.

There is no per-campaign ``data.db`` any more: a campaign is a results directory on
disk whose rows are ingested into the central index, and querying it is a
``WHERE campaign_id = ...`` clause. So these fixtures build the tree the way a real
campaign is shaped -- a metric CSV per run, plus the SQLite ``campaign.db`` the driver
writes, which is where the params and each run's outcome come from -- and hand it to
:func:`robovast.results_processing.campaign_ingest.ingest_campaign`.

Everything here needs Postgres, so the fixtures skip when ``ROBOVAST_TEST_PG_DSN`` is
unset; tests that need no database must not take them.
"""

import csv
import json
import os
import sqlite3
from pathlib import Path

import pytest

DSN = os.environ.get("ROBOVAST_TEST_PG_DSN")

#: Namespaced so a shared test database can hold several suites at once.
SCHEMA = "dq_test"

#: One unit per configuration -- (config_name, objective, params). Scalar scenario params
#: become ``param_*`` columns of the ``runs`` table and non-scalar ones are JSON-encoded
#: there, which is what makes a list-valued param a thing SQL can unnest.
UNITS = [
    ("cfg-a", 1.5, {"wind": 0.0, "waypoints": [{"x": 1.0, "y": 2.0}, {"x": 3.0, "y": 4.0}]}),
    ("cfg-b", 0.5, {"wind": 5.0, "waypoints": [{"x": 9.0, "y": 9.0}]}),
]

#: (config_name, run_id, status, passed, duration_s) -- one row per run directory.
RUNS = [
    ("cfg-a", 0, "passed", 1, 10.0),
    ("cfg-a", 1, "failed", 0, 12.0),
    ("cfg-b", 0, "passed", 1, 20.0),
    ("cfg-b", 1, "passed", 1, 22.0),
]

#: (config_name, run_id, error) -- a per-run metric table, joinable on the run keys.
LANDING_ERROR = [("cfg-a", 0, 0.10), ("cfg-a", 1, 0.90),
                 ("cfg-b", 0, 0.20), ("cfg-b", 1, 0.30)]

CAMPAIGN_CONFIG = {"evaluation": {"plots": []}, "execution": {"runs": 2}}


def _write_csv(path: Path, header, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def write_results_tree(root: Path) -> Path:
    """The per-run data files a postprocessed campaign leaves on disk.

    ``config_name``/``run_id`` are *not* columns in the files: the ingest derives them
    from the directory the file was found in, exactly as it does for a real campaign.
    """
    (root / "_execution").mkdir(parents=True, exist_ok=True)
    for config, run, error in LANDING_ERROR:
        _write_csv(root / config / str(run) / "landing_error.csv", ["error"], [[error]])
    return root


def write_campaign_db(root: Path, name: str) -> None:
    """The driver's ``campaign.db``: still SQLite, mirrored into the index on ingest.

    It is also where the ``runs`` dimension table comes from -- the params, the objective
    and each run's outcome -- so the scenario params have to be written here rather than
    into a data file.

    ``strategy_state`` is written because a real one has it: the search's pickle is what
    the ingest is expected to leave behind (``dimension_ingest._EXCLUDED_COLUMNS``).
    """
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / "campaign.db")
    conn.executescript(
        "CREATE TABLE campaign (id INTEGER PRIMARY KEY, name TEXT, config_json TEXT,"
        "                       strategy_state BLOB);"
        "CREATE TABLE unit (id INTEGER PRIMARY KEY, batch_id INTEGER, config_name TEXT,"
        "                   paramset_id TEXT, params_json TEXT, objective REAL,"
        "                   status TEXT);"
        "CREATE TABLE run (id INTEGER PRIMARY KEY, unit_id INTEGER, run_id INTEGER,"
        "                  status TEXT, passed INTEGER, duration_s REAL, errors INTEGER,"
        "                  failures INTEGER, tests INTEGER, start_time TEXT,"
        "                  failure_message TEXT, job_id INTEGER);")
    conn.execute(
        "INSERT INTO campaign (id, name, config_json, strategy_state) VALUES (?,?,?,?)",
        (1, name, json.dumps(CAMPAIGN_CONFIG),
         b"\x80\x04" + b"\x00" * 4096))  # stand-in for a pickled optimizer blob
    units = {}
    for index, (config, objective, params) in enumerate(UNITS, start=1):
        units[config] = index
        conn.execute("INSERT INTO unit VALUES (?,1,?,?,?,?,'evaluated')",
                     (index, config, f"ps-{index}", json.dumps(params), objective))
    for index, (config, run_id, status, passed, duration) in enumerate(RUNS, start=1):
        conn.execute("INSERT INTO run VALUES (?,?,?,?,?,?,0,0,1,'t',NULL,NULL)",
                     (index, units[config], run_id, status, passed, duration))
    conn.commit()
    conn.close()


def reset_schema(psycopg) -> None:
    with psycopg.connect(DSN, autocommit=True) as setup:
        for statement in (f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE",
                          "DROP SCHEMA IF EXISTS campaign CASCADE",
                          f"CREATE SCHEMA {SCHEMA}"):
            setup.execute(statement)


def drop_schema(psycopg) -> None:
    with psycopg.connect(DSN, autocommit=True) as teardown:
        teardown.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        teardown.execute("DROP SCHEMA IF EXISTS campaign CASCADE")


@pytest.fixture(name="index")
def _index(monkeypatch):
    """An empty index, with the env pointing at it the way a deployment would."""
    if not DSN:
        pytest.skip("ROBOVAST_TEST_PG_DSN is not set")
    psycopg = pytest.importorskip("psycopg")
    from robovast.common import index_db

    reset_schema(psycopg)
    monkeypatch.setenv(index_db.DSN_ENV, f"{DSN} options=-csearch_path={SCHEMA}")
    yield
    drop_schema(psycopg)


def ingest(root: Path, campaign_id: str) -> None:
    from robovast.results_processing import campaign_ingest, index_query, index_views

    with index_query.open_index(readonly=False) as conn:
        campaign_ingest.ingest_campaign(conn, str(root), campaign_id)
        index_views.create_views(conn)


@pytest.fixture
def campaign_dir(index, tmp_path: Path) -> Path:  # pylint: disable=unused-argument
    """A campaign directory whose runs and whose record are both in the index."""
    root = tmp_path / "camp-2026-08-10-07150919"
    write_results_tree(root)
    write_campaign_db(root, root.name)
    ingest(root, root.name)
    return root


@pytest.fixture
def campaign_dir_no_data(index, tmp_path: Path) -> Path:  # pylint: disable=unused-argument
    """A campaign with only its record ingested (postprocessing has not run)."""
    root = tmp_path / "camp-2026-08-10-07150920"
    (root / "_execution").mkdir(parents=True)
    write_campaign_db(root, root.name)
    ingest(root, root.name)
    return root
