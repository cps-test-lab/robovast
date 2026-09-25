# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Campaign directories with a store and recordings, as a query engine meets them."""

import json
import sqlite3
from pathlib import Path

import pytest

from robovast.common.store import _SCHEMA
from tests.robovast_decode.conftest import make_campaign

SYSINFO = {"instance_type": "n2-standard-8", "node_label": "9f2c1a", "cpu_name": "Xeon",
           "available_cpus": 2, "available_mem": "16Gi"}


def write_store(root: Path, configs, config_json=None, failures=()):
    """``campaign.db`` with the store's own schema: one unit per config, one run per run.

    *configs* is ``{config_name: {"params": {...}, "runs": {run_id: status}}}``.
    """
    db = sqlite3.connect(root / "campaign.db")
    db.executescript(_SCHEMA)
    db.execute("INSERT INTO campaign (id, name, config_json) VALUES (1, ?, ?)",
               (root.name, json.dumps(config_json or {"execution": {"containers": {
                   "scenario": {"cpu": 2}}}, "flag": True})))
    db.execute("INSERT INTO batch (id, campaign_id, idx) VALUES (1, 1, 0)")
    db.execute("INSERT INTO job (id, campaign_id, job_dir, sysinfo_json) VALUES "
               "(1, 1, '_jobs/job-0', ?)", (json.dumps(SYSINFO),))
    run_row = 1
    for unit_id, (config_name, spec) in enumerate(configs.items(), 1):
        db.execute("INSERT INTO unit (id, batch_id, paramset_id, config_name, params_json, "
                   "status) VALUES (?, 1, ?, ?, ?, 'ok')",
                   (unit_id, config_name, config_name, json.dumps(spec.get("params", {}))))
        for run_id, status in spec.get("runs", {}).items():
            db.execute("INSERT INTO run (id, unit_id, run_id, status, passed, errors, "
                       "failures, tests, duration_s, start_time, job_id) VALUES "
                       "(?, ?, ?, ?, ?, 0, 0, 1, 10.0, '2026-01-01T00:00:00', 1)",
                       (run_row, unit_id, run_id, status, int(status == "passed")))
            run_row += 1
    for detail in failures:
        db.execute("INSERT INTO container_failure (campaign_id, job_dir, runs_json, container, "
                   "restart_count) VALUES (1, '_jobs/job-0', ?, 'scenario', 1)",
                   (json.dumps(detail),))
    db.commit()
    db.close()


def nav_campaign(root: Path, runs=(("cfg", 0),), params=None) -> Path:
    """The decoder's fixture recording per run, and a store recording those runs."""
    make_campaign(root, runs=runs)
    configs = {}
    for config, run_id in runs:
        entry = configs.setdefault(config, {"params": (params or {}).get(config, {}),
                                            "runs": {}})
        entry["runs"][run_id] = "passed"
    write_store(root, configs)
    return root


@pytest.fixture
def campaign(tmp_path) -> Path:
    return nav_campaign(tmp_path / "nav-2026-01-01-00000000",
                        runs=(("cfg-a", 0), ("cfg-a", 1), ("cfg-b", 0)),
                        params={"cfg-a": {"speed": 0.5}, "cfg-b": {"speed": 1.0}})
