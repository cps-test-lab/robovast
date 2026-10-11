# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Fixtures for :mod:`robovast.results_processing.data_query` tests.

A campaign is a results directory, and querying it builds its tables from what is on disk.
So these fixtures build the tree the way a real campaign is shaped -- a metric CSV per run,
plus the SQLite ``campaign.db`` the driver writes, which is where the params and each run's
outcome come from.
"""

import csv
import json
import sqlite3
from pathlib import Path

import pytest

from robovast.common.store import CampaignStore

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

    ``config_name``/``run_id`` are *not* columns in the files: a table built from a run's
    file takes them from the directory the file was found in, as for a real campaign.
    """
    (root / "_execution").mkdir(parents=True, exist_ok=True)
    for config, run, error in LANDING_ERROR:
        _write_csv(root / config / str(run) / "landing_error.csv", ["error"], [[error]])
    return root


def write_campaign_db(root: Path, name: str) -> None:
    """The driver's ``campaign.db``: the campaign's record, read as the ``campaign`` schema.

    It is also where the ``runs`` table comes from -- the params, the objective and each
    run's outcome -- so the scenario params have to be written here rather than into a data
    file. The schema is the store's own, so the fixture cannot drift from what the driver
    writes.

    ``strategy_state`` is written because a real one has it: the search's pickle is what the
    query engine is expected to leave out of the ``campaign`` schema.
    """
    root.mkdir(parents=True, exist_ok=True)
    CampaignStore(str(root / "campaign.db")).close()
    conn = sqlite3.connect(root / "campaign.db")
    conn.execute(
        "INSERT INTO campaign (id, name, config_json, strategy_state) VALUES (?,?,?,?)",
        (1, name, json.dumps(CAMPAIGN_CONFIG),
         b"\x80\x04" + b"\x00" * 4096))  # stand-in for a pickled optimizer blob
    conn.execute("INSERT INTO batch (id, campaign_id, idx) VALUES (1, 1, 0)")
    units = {}
    for index, (config, objective, params) in enumerate(UNITS, start=1):
        units[config] = index
        conn.execute(
            "INSERT INTO unit (id, batch_id, paramset_id, config_name, params_json, objective,"
            " status) VALUES (?,1,?,?,?,?,'evaluated')",
            (index, f"ps-{index}", config, json.dumps(params), objective))
    for index, (config, run_id, status, passed, duration) in enumerate(RUNS, start=1):
        conn.execute(
            "INSERT INTO run (id, unit_id, run_id, status, passed, duration_s, errors,"
            " failures, tests, start_time) VALUES (?,?,?,?,?,?,0,0,1,'t')",
            (index, units[config], run_id, status, passed, duration))
    conn.commit()
    conn.close()


@pytest.fixture
def campaign_dir(tmp_path: Path) -> Path:
    """A campaign directory with runs, a metric file per run, and its record."""
    root = tmp_path / "camp-2026-08-10-07150919"
    write_results_tree(root)
    write_campaign_db(root, root.name)
    return root


@pytest.fixture
def campaign_dir_no_data(tmp_path: Path) -> Path:
    """A campaign with only its record: no run has written a data file."""
    root = tmp_path / "camp-2026-08-10-07150920"
    (root / "_execution").mkdir(parents=True)
    write_campaign_db(root, root.name)
    return root
