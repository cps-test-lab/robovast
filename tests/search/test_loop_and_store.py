# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""CampaignController orchestration (fake backend) + CampaignStore.

Covers both modes: a strategy-driven *search* campaign and a strategy-less
*batch* campaign, plus the live store records (one batch per ask/tell round /
one batch for batch mode).
"""

# Tests exercise store internals and import schema helpers lazily.
# pylint: disable=import-outside-toplevel,protected-access

import json
import os
import sqlite3

from robovast.common.config import SearchConfig
from robovast.common.store import STORE_FILENAME, CampaignStore
from robovast.execution.backends import ExecutionBackend, RunOptions
from robovast.execution.controller import CampaignController
from robovast.search.evaluator import Evaluator
from robovast.search.strategy import SearchStrategy, build_strategy
from robovast.search.types import ParamSet, SearchReport


def _cfg(batches=2, per_batch=3, stopping=None):
    budget = [{"batches": batches}]
    return SearchConfig(
        strategy="random",
        search_space={"x": {"type": "float", "low": 0, "high": 1}},
        extract={"plugin": "failure_rate"},
        objectives=[{"name": "failure_rate", "direction": "maximize"}],
        per_batch=per_batch, budget=budget, seed=1, stopping=stopping,
    )


def _write_test_xml(run_dir, failures):
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "test.xml"), "w") as f:
        f.write(f'<testsuite errors="0" failures="{failures}" tests="1">'
                f'<testcase name="t" time="1.0"/></testsuite>')


class FakeBackend(ExecutionBackend):
    """Writes per-config test.xml under the campaign root (alternating pass/fail)."""

    def __init__(self):
        self.batch_runs = []  # reps requested per run_batch call

    def run_batch(self, campaign_data, *, campaign_root, batch_tag, runs, options):
        self.batch_runs.append(runs)
        for i, cfg in enumerate(campaign_data["configs"]):
            failures = i % 2  # alternate failing / passing configs
            for run in range(runs):
                _write_test_xml(os.path.join(campaign_root, cfg["name"], str(run)), failures)


class FakeCompose:
    def compose(self, param_sets, output_dir):
        name_by_id = {ps.id: f"c{ps.id}" for ps in param_sets}
        campaign_data = {"execution": {"image": "img", "runs": 1},
                         "configs": [{"name": n} for n in name_by_id.values()]}
        return campaign_data, name_by_id


def _search_controller(cfg, tmp_path, strategy=None, runs=2):
    from robovast.search.stopping import build_stop_conditions
    store = CampaignStore(tmp_path / "camp" / STORE_FILENAME)
    backend = FakeBackend()
    controller = CampaignController(
        campaign_id="camp", results_dir=str(tmp_path), runs=runs, backend=backend,
        options=RunOptions(), store=store, campaign_config_dump={"version": 1},
        vast_dir=str(tmp_path), strategy=strategy or build_strategy(cfg),
        evaluator=Evaluator(cfg, str(tmp_path)), compose=FakeCompose(),
        per_batch=cfg.per_batch, stop_conditions=build_stop_conditions(cfg))
    return controller, store, backend


def test_search_runs_batches_and_records(tmp_path):
    cfg = _cfg(batches=2, per_batch=3)
    controller, store, backend = _search_controller(cfg, tmp_path)
    report = controller.run()

    assert backend.batch_runs == [2, 2]
    assert len(report.evaluations) == 6
    assert {next(iter(e.objectives.values())) for e in report.evaluations} <= {0.0, 1.0}
    assert report.best.objectives["failure_rate"] == 1.0
    assert all(e.n_samples == 2 for e in report.evaluations)

    conn = sqlite3.connect(store.db_path)
    assert conn.execute("SELECT mode FROM campaign").fetchone()[0] == "search"
    assert conn.execute("SELECT COUNT(*) FROM unit").fetchone()[0] == 6
    assert conn.execute("SELECT COUNT(*) FROM batch").fetchone()[0] == 2
    assert conn.execute("SELECT DISTINCT n_samples FROM unit").fetchall() == [(2,)]
    row = conn.execute("SELECT objectives_json, measures_json FROM unit LIMIT 1").fetchone()
    assert "failure_rate" in json.loads(row[0]) and json.loads(row[1]) == {}
    conn.close()
    store.close()

    # All config dirs are flat under the one campaign root (no per-batch nesting).
    campaign_root = tmp_path / "camp"
    config_dirs = [d for d in campaign_root.iterdir() if d.is_dir() and d.name.startswith("c")]
    assert len(config_dirs) == 6


class _Fixed(SearchStrategy):
    PARAMS_MODEL = None

    def __init__(self, cfg, param_sets):
        super().__init__(cfg, {})
        self._param_sets = param_sets
        self._done = False
        self.told = []

    def ask(self, n):
        return self._param_sets

    def tell(self, evaluations):
        self.told = evaluations
        self._done = True

    def report(self):
        return SearchReport(evaluations=self.told)


def test_stopping_target_objective_halts_early(tmp_path):
    # FakeBackend makes config #1 fail -> failure_rate 1.0 in batch 0, so a
    # target of 1.0 stops after the first batch despite the batches budget of 5.
    cfg = _cfg(batches=5, per_batch=3,
               stopping=[{"target_objective": 1.0}])
    controller, store, backend = _search_controller(cfg, tmp_path)
    report = controller.run()
    assert len(backend.batch_runs) == 1            # stopped after batch 0
    # Outcome persisted (parseable) on the campaign row + in the report.
    conn = sqlite3.connect(store.db_path)
    assert conn.execute("SELECT COUNT(*) FROM batch").fetchone()[0] == 1
    row = conn.execute("SELECT stop_kind, batches FROM campaign").fetchone()
    assert row == ("target_objective", 1)
    conn.close()
    store.close()
    assert report.extra["stop"]["kind"] == "target_objective"


def test_no_stopping_runs_full_budget(tmp_path):
    cfg = _cfg(batches=3, per_batch=2)             # only the batches budget
    controller, store, backend = _search_controller(cfg, tmp_path)
    report = controller.run()
    assert len(backend.batch_runs) == 3            # full batches budget
    assert report.extra["stop"]["kind"] == "batches"
    conn = sqlite3.connect(store.db_path)
    assert conn.execute("SELECT stop_kind FROM campaign").fetchone()[0] == "batches"
    conn.close()
    store.close()


def test_n_reps_override_groups_runs(tmp_path):
    cfg = _cfg(batches=1, per_batch=3)
    param_sets = [ParamSet(values={"x": 0.1}, n_reps=5),
                  ParamSet(values={"x": 0.2}, n_reps=5),
                  ParamSet(values={"x": 0.3})]
    controller, store, backend = _search_controller(
        cfg, tmp_path, strategy=_Fixed(cfg, param_sets), runs=2)
    report = controller.run()
    assert sorted(backend.batch_runs) == [2, 5]
    by_x = {ev.params.values["x"]: ev.n_samples for ev in report.evaluations}
    assert by_x[0.1] == 5 and by_x[0.2] == 5 and by_x[0.3] == 2
    store.close()


def test_batch_mode_records_one_batch(tmp_path):
    store = CampaignStore(tmp_path / "camp" / STORE_FILENAME)
    backend = FakeBackend()
    campaign_data = {"execution": {"image": "img", "runs": 2},
                     "configs": [{"name": "ca", "config": {"speed": 1.0}},
                                 {"name": "cb", "config": {"speed": 2.0}}]}
    controller = CampaignController(
        campaign_id="camp", results_dir=str(tmp_path), runs=2, backend=backend,
        options=RunOptions(), store=store, campaign_config_dump={"version": 1},
        vast_dir=str(tmp_path), batch_campaign_data=campaign_data)
    report = controller.run()
    assert report == {"mode": "batch", "configs": 2,
                      "campaign_root": str(tmp_path / "camp")}
    assert backend.batch_runs == [2]

    conn = sqlite3.connect(store.db_path)
    assert conn.execute("SELECT mode FROM campaign").fetchone()[0] == "batch"
    assert conn.execute("SELECT COUNT(*) FROM batch").fetchone()[0] == 1
    rows = dict(conn.execute("SELECT config_name, status FROM unit").fetchall())
    assert rows == {"ca": "passed", "cb": "failed"}  # FakeBackend alternates
    assert conn.execute("SELECT DISTINCT n_samples FROM unit").fetchall() == [(2,)]
    conn.close()
    store.close()


def test_store_strategy_state_roundtrip(tmp_path):
    store = CampaignStore(tmp_path / "s.db")
    cid = store.create_campaign("c", {"version": 1})
    store.save_strategy_state(cid, b"opaque-blob")
    assert store.load_strategy_state(cid) == b"opaque-blob"
    store.close()


def test_fresh_store_stamps_schema_version(tmp_path):
    from robovast.common.store import SCHEMA_VERSION
    store = CampaignStore(tmp_path / "camp.db")
    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    store.close()


def test_pre_versioning_store_migrates_forward(tmp_path):
    """A store created before schema versioning (tables present, user_version 0)
    is adopted at the current version and stays readable."""
    from robovast.common.store import SCHEMA_VERSION, _SCHEMA
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)  # pre-versioning: tables but no user_version bump
    conn.execute(
        "INSERT INTO campaign (id, name, mode, created_at) VALUES (1, 'old', 'search', 0)")
    conn.commit()
    conn.close()
    assert sqlite3.connect(db).execute("PRAGMA user_version").fetchone()[0] == 0

    with CampaignStore(db) as store:
        assert store._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert [c["name"] for c in store.list_campaigns()] == ["old"]


def test_newer_store_is_read_best_effort(tmp_path):
    """A store written by a newer robovast (higher user_version, extra column) is
    not downgraded and remains readable through existing columns."""
    from robovast.common.store import SCHEMA_VERSION, _SCHEMA
    db = tmp_path / "future.db"
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    conn.execute("ALTER TABLE campaign ADD COLUMN future_col TEXT")
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.execute(
        "INSERT INTO campaign (id, name, mode, created_at) VALUES (1, 'newer', 'search', 0)")
    conn.commit()
    conn.close()

    with CampaignStore(db) as store:
        # Untouched: still at the newer version, not migrated down.
        assert store._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION + 1
        assert [c["name"] for c in store.list_campaigns()] == ["newer"]
