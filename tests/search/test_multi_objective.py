# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""A search with more than one objective — end-to-end through the controller.

The schema has always accepted a list of objectives and ``SearchReport`` has always had a
``front``, but the loop folded a single scalar best and raised on anything else. So a
two-objective ``.vast`` validated and then died at run time.

What multi-objective means here: the deliverable is the non-dominated set, not a winner.
Nothing picks between "close but fast" and "slow but safe" without a weighting nobody has.
"""

import sqlite3

import pytest

from robovast.common.config import SearchConfig
from robovast.common.store import STORE_FILENAME, CampaignStore
from robovast.execution.backends import ExecutionBackend, RunOptions
from robovast.execution.controller import CampaignController
from robovast.search.strategy import SearchStrategy
from robovast.search.types import Evaluation, ParamSet, SearchReport

from .test_loop_and_store import FakeCompose


def _cfg(objectives, batches=2, per_batch=3):
    return SearchConfig(
        strategy='random',
        search_space={'x': {'type': 'float', 'low': 0, 'high': 1}},
        extract={'plugin': 'failure_rate'},
        objectives=objectives, per_batch=per_batch,
        budget=[{'batches': batches}], seed=1,
    )


TWO = [{'name': 'clearance', 'direction': 'maximize'},
       {'name': 'time', 'direction': 'minimize'}]


class _TwoObjective(SearchStrategy):
    """Proposes points and scores each on both objectives; never asks for a scalar best."""

    PARAMS_MODEL = None

    def __init__(self, cfg):
        super().__init__(cfg, {})
        self._history: list[Evaluation] = []
        self._n = 0

    def ask(self, n):
        out = [ParamSet(values={'x': (self._n + i) / 10.0}) for i in range(n)]
        self._n += n
        return out

    def tell(self, evaluations):
        self._history.extend(evaluations)

    def report(self):
        return SearchReport(evaluations=list(self._history))


class _Backend(ExecutionBackend):
    def __init__(self):
        self.batches = 0

    def run_batch(self, campaign_data, *, campaign_root, batch_tag, runs, options):
        self.batches += 1


class _TwoObjectiveEvaluator:
    """Scores a cell on both objectives: clearance rises with x, and costs time to get."""

    def evaluate(self, config_dir, param_set):
        x = float(param_set.values['x'])
        return Evaluation(params=param_set,
                          objectives={'clearance': x, 'time': x},
                          n_samples=1)


def _controller(cfg, tmp_path, strategy):
    from robovast.search.stopping import build_stop_conditions
    store = CampaignStore(tmp_path / 'camp' / STORE_FILENAME)
    backend = _Backend()
    controller = CampaignController(
        campaign_id='camp', results_dir=str(tmp_path), runs=1, backend=backend,
        options=RunOptions(), store=store, campaign_config_dump={'version': 1},
        vast_dir=str(tmp_path), strategy=strategy,
        evaluator=_TwoObjectiveEvaluator(), compose=FakeCompose(),
        per_batch=cfg.per_batch, stop_conditions=build_stop_conditions(cfg))
    return controller, store


# -- the loop ---------------------------------------------------------------

def test_a_two_objective_search_runs_to_completion(tmp_path):
    """The regression: this used to raise before the first batch."""
    cfg = _cfg(TWO)
    controller, store = _controller(cfg, tmp_path, _TwoObjective(cfg))
    report = controller.run()
    assert len(report.evaluations) == 6
    assert report.extra['stop']['kind'] == 'batches'
    store.close()


def test_the_report_carries_a_front(tmp_path):
    """Clearance and time rise together, so every point trades one for the other and the
    whole set is non-dominated -- the answer is the curve, not a winner."""
    cfg = _cfg(TWO)
    controller, store = _controller(cfg, tmp_path, _TwoObjective(cfg))
    report = controller.run()
    assert len(report.front) == len(report.evaluations)
    store.close()


def test_the_front_excludes_a_dominated_cell(tmp_path):
    """A cell beaten on both objectives is not a trade-off and must not be offered as one."""
    cfg = _cfg(TWO, batches=1, per_batch=3)

    class _Dominated(_TwoObjectiveEvaluator):
        def evaluate(self, config_dir, param_set):
            # x=0.0 -> (0.5, 0.5) beats x=0.1 -> (0.4, 0.6) on both.
            x = float(param_set.values['x'])
            table = {0.0: (0.5, 0.5), 0.1: (0.4, 0.6), 0.2: (0.9, 0.9)}
            clearance, time = table.get(round(x, 1), (x, x))
            return Evaluation(params=param_set,
                              objectives={'clearance': clearance, 'time': time},
                              n_samples=1)

    controller, store = _controller(cfg, tmp_path, _TwoObjective(cfg))
    controller.evaluator = _Dominated()
    report = controller.run()
    fronted = {round(float(ev.objectives['clearance']), 1) for ev in report.front}
    assert 0.4 not in fronted           # dominated by (0.5, 0.5)
    assert {0.5, 0.9} <= fronted
    store.close()


def test_a_single_objective_search_still_reports_a_best(tmp_path):
    """The existing path must not regress: one objective still folds a scalar best."""
    cfg = _cfg([{'name': 'clearance', 'direction': 'maximize'}], batches=1, per_batch=2)
    controller, store = _controller(cfg, tmp_path, _TwoObjective(cfg))
    controller.run()
    conn = sqlite3.connect(store.db_path)
    assert conn.execute('SELECT stop_kind FROM campaign').fetchone()[0] == 'batches'
    conn.close()
    store.close()


# -- what a single-objective strategy does with two -------------------------

def test_single_objective_helper_still_refuses_two():
    """`single_objective` is how a strategy says it cannot do this; keep it loud."""
    cfg = _cfg(TWO)
    strategy = _TwoObjective(cfg)
    with pytest.raises(ValueError, match='single-objective'):
        _ = strategy.single_objective


# -- optuna -----------------------------------------------------------------

def test_optuna_nsga2_optimises_two_objectives():
    """NSGA-II is the sampler that makes optuna multi-objective; without it the study is
    scalar and `tell` with two values is a type error."""
    pytest.importorskip('optuna')
    from robovast.search.strategies.optuna import OptunaStrategy, OptunaParams

    cfg = _cfg(TWO, per_batch=4)
    strategy = OptunaStrategy(cfg, OptunaParams(sampler='nsga2'))
    for _ in range(3):
        proposals = strategy.ask(4)
        strategy.tell([
            Evaluation(params=ps,
                       objectives={'clearance': float(ps.values['x']),
                                   'time': float(ps.values['x'])},
                       n_samples=1)
            for ps in proposals])
    report = strategy.report()
    assert len(report.evaluations) == 12
    assert report.front, 'a multi-objective study must report a front'


def test_optuna_refuses_a_scalar_sampler_for_two_objectives():
    """Picking tpe with two objectives is a mistake worth naming, not a silent scalarisation."""
    pytest.importorskip('optuna')
    from robovast.search.strategies.optuna import OptunaStrategy, OptunaParams

    cfg = _cfg(TWO)
    with pytest.raises(ValueError, match='nsga2'):
        OptunaStrategy(cfg, OptunaParams(sampler='tpe'))
