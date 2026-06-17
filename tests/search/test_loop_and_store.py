# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""CampaignController orchestration (fake backend) + CampaignStore.

Covers both modes: a strategy-driven *search* campaign and a strategy-less
*batch* campaign, plus the live store records (one batch per ask/tell round /
one batch for batch mode).
"""

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


def _cfg(batches=2, per_batch=3):
    return SearchConfig(
        strategy="random",
        search_space={"x": {"type": "float", "low": 0, "high": 1}},
        extract={"plugin": "failure_rate"},
        objectives=[{"name": "failure_rate", "direction": "maximize"}],
        per_batch=per_batch, budget={"batches": batches}, seed=1,
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
    store = CampaignStore(tmp_path / "camp" / STORE_FILENAME)
    backend = FakeBackend()
    controller = CampaignController(
        campaign_id="camp", results_dir=str(tmp_path), runs=runs, backend=backend,
        options=RunOptions(), store=store, campaign_config_dump={"version": 1},
        vast_dir=str(tmp_path), strategy=strategy or build_strategy(cfg),
        evaluator=Evaluator(cfg, str(tmp_path)), compose=FakeCompose(),
        per_batch=cfg.per_batch)
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

    def is_done(self):
        return self._done

    def report(self):
        return SearchReport(evaluations=self.told)


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
    store = CampaignStore(tmp_path / "s.sqlite")
    cid = store.create_campaign("c", {"version": 1})
    store.save_strategy_state(cid, b"opaque-blob")
    assert store.load_strategy_state(cid) == b"opaque-blob"
    store.close()
