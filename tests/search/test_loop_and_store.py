# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""SearchLoop orchestration (fake compose+launcher) and CampaignStore."""

import json
import os
import sqlite3

from robovast.common.config import SearchConfig
from robovast.common.store import STORE_FILENAME, CampaignStore
from robovast.search.evaluator import Evaluator
from robovast.search.launcher import Launcher
from robovast.search.loop import SearchLoop
from robovast.search.strategy import SearchStrategy, build_strategy
from robovast.search.types import ParamSet, SearchReport


def _cfg(generations=2, per_step=3):
    return SearchConfig(
        strategy="random",
        search_space={"x": {"type": "float", "low": 0, "high": 1}},
        extract={"plugin": "failure_rate"},
        objectives=[{"name": "failure_rate", "direction": "maximize"}],
        per_step=per_step, budget={"generations": generations}, seed=1,
    )


class FakeCompose:
    def __init__(self):
        self.calls = 0

    def compose(self, param_sets, output_dir):
        self.calls += 1
        name_by_id = {ps.id: f"c{ps.id}" for ps in param_sets}
        campaign_data = {"execution": {"image": "img", "runs": 1},
                         "configs": [{"name": n} for n in name_by_id.values()]}
        return campaign_data, name_by_id


class FakeLauncher(Launcher):
    def __init__(self):
        self.launched_runs = []

    def launch(self, campaign_data, gen_dir, runs):
        self.launched_runs.append(runs)
        result_dir = os.path.join(gen_dir, "results")
        for i, cfg in enumerate(campaign_data["configs"]):
            failures = i % 2     # alternate failing / passing configs
            for run in range(runs):
                run_dir = os.path.join(result_dir, cfg["name"], str(run))
                os.makedirs(run_dir, exist_ok=True)
                with open(os.path.join(run_dir, "test.xml"), "w") as f:
                    f.write(f'<testsuite errors="0" failures="{failures}" tests="1">'
                            f'<testcase name="t" time="1.0"/></testsuite>')
        return result_dir


def _loop(cfg, tmp_path, strategy=None, runs=2):
    store = CampaignStore(tmp_path / STORE_FILENAME)
    launcher = FakeLauncher()
    loop = SearchLoop(
        vast_file="x.vast", output_dir=str(tmp_path / "out"), runs=runs, store=store,
        strategy=strategy or build_strategy(cfg), evaluator=Evaluator(cfg),
        compose=FakeCompose(), launcher=launcher, per_step=cfg.per_step,
    )
    return loop, store, launcher


def test_loop_runs_generations_and_records(tmp_path):
    cfg = _cfg(generations=2, per_step=3)
    loop, store, launcher = _loop(cfg, tmp_path)
    report = loop.run("camp", {"version": 1})

    assert launcher.launched_runs == [2, 2]
    assert len(report.evaluations) == 6
    assert {next(iter(e.objectives.values())) for e in report.evaluations} <= {0.0, 1.0}
    assert report.best.objectives["failure_rate"] == 1.0
    assert all(e.n_samples == 2 for e in report.evaluations)

    conn = sqlite3.connect(store.db_path)
    assert conn.execute("SELECT mode FROM campaign").fetchone()[0] == "search"
    assert conn.execute("SELECT COUNT(*) FROM unit").fetchone()[0] == 6
    assert conn.execute("SELECT COUNT(*) FROM generation").fetchone()[0] == 2
    assert conn.execute("SELECT DISTINCT n_samples FROM unit").fetchall() == [(2,)]
    row = conn.execute("SELECT objectives_json, measures_json FROM unit LIMIT 1").fetchone()
    assert "failure_rate" in json.loads(row[0]) and json.loads(row[1]) == {}
    conn.close()
    store.close()


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


def test_n_reps_override_groups_launches(tmp_path):
    cfg = _cfg(generations=1, per_step=3)
    param_sets = [ParamSet(values={"x": 0.1}, n_reps=5),
                  ParamSet(values={"x": 0.2}, n_reps=5),
                  ParamSet(values={"x": 0.3})]
    loop, store, launcher = _loop(cfg, tmp_path, strategy=_Fixed(cfg, param_sets), runs=2)
    report = loop.run("camp", {"version": 1})
    assert sorted(launcher.launched_runs) == [2, 5]
    by_x = {ev.params.values["x"]: ev.n_samples for ev in report.evaluations}
    assert by_x[0.1] == 5 and by_x[0.2] == 5 and by_x[0.3] == 2
    store.close()


def test_store_strategy_state_roundtrip(tmp_path):
    store = CampaignStore(tmp_path / "s.sqlite")
    cid = store.create_campaign("c", {"version": 1})
    store.save_strategy_state(cid, b"opaque-blob")
    assert store.load_strategy_state(cid) == b"opaque-blob"
    store.close()
