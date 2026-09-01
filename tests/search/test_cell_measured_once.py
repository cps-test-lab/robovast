# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""A cell is measured once per campaign, however often a strategy proposes it.

Strategies revisit: TPE re-proposes a category it likes, and on a discrete space every
strategy here eventually lands twice on the same cell. Within one batch that is already
collapsed. ACROSS batches it was not, and it is not merely wasteful there but
unrecordable -- the result directory is addressed by ``ParamSet.id``, so a second
evaluation of one cell writes into the first's directory, over its runs, and the campaign
dies on the job link that guards exactly that:

    conflicting job_links.yaml entry for 'c81eda2e11597-1-1/0/job':
    already '../../_jobs/batch-0/job-0', now '../../_jobs/batch-1/job-2'

Measured on a four-cell space with per_batch 8: batch 0 scored three cells, batch 1
re-proposed one of them, and the campaign ended there on batch 1 of 2.

A re-proposed cell is therefore not run again -- there is one place to put its results and
it already holds the answer -- and the strategy is told what that cell scored. That is a
real answer to a real proposal, so the generation is not short; what it costs is nothing.
"""

import sqlite3

from robovast.common.config import SearchConfig
from robovast.search.types import ParamSet, SearchReport

from .test_loop_and_store import FakeCompose, _search_controller


def _cfg(batches=3, per_batch=2):
    return SearchConfig(
        strategy="random", search_space={"x": {"type": "float", "low": 0, "high": 1}},
        extract={"plugin": "failure_rate"},
        objectives=[{"name": "failure_rate", "direction": "maximize"}],
        per_batch=per_batch, budget=[{"batches": batches}], seed=1)


class ScriptedStrategy:
    """Proposes exactly the cells it is told to, batch by batch."""

    RESUMABLE = True

    def __init__(self, cfg, script):
        self.objectives = cfg.objectives
        self.script = list(script)
        self.told = []
        self._batch = 0

    def ask(self, n):
        values = self.script[min(self._batch, len(self.script) - 1)]
        self._batch += 1
        return [ParamSet(values={"x": float(v)}) for v in values]

    def tell(self, evaluations):
        self.told.append(sorted(ev.params.values["x"] for ev in evaluations))

    def resume(self, batches):
        for batch in batches:
            self.ask(batch.asked)
            self.tell(batch.evaluations)

    def report(self):
        return SearchReport(extra={})


def _run(tmp_path, script, batches=None):
    cfg = _cfg(batches=batches or len(script), per_batch=len(script[0]))
    strategy = ScriptedStrategy(cfg, script)
    controller, store, backend = _search_controller(
        cfg, tmp_path, strategy=strategy, runs=2, compose=FakeCompose())
    controller.run()
    return controller, store, backend, strategy


def _units(store):
    conn = sqlite3.connect(store.db_path)
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT b.idx AS batch, u.paramset_id, u.status FROM unit u "
        "JOIN batch b ON u.batch_id = b.id ORDER BY u.id").fetchall()


def test_a_cell_from_an_earlier_batch_is_not_run_again(tmp_path):
    """Batch 1 re-proposes one of batch 0's cells and adds one of its own."""
    controller, store, backend, _ = _run(tmp_path, [[1.0, 2.0], [1.0, 3.0]])

    units = _units(store)
    assert [(u["batch"], u["paramset_id"] in {units[0]["paramset_id"],
                                              units[1]["paramset_id"]})
            for u in units] == [(0, True), (0, True), (1, False)]
    # Three cells measured, not four: batch 1 ran only the cell it had not seen.
    assert len(units) == 3
    # And only that cell's runs were executed -- 2 configs, then 1.
    assert backend.batch_runs == [2, 2]
    assert controller._runs_done == 6            # noqa: SLF001 - 2+2 then 2, not 8


def test_the_strategy_is_still_told_about_the_cell_it_re_proposed(tmp_path):
    """Not a short generation: the recalled cell is what that cell measured."""
    _, _, _, strategy = _run(tmp_path, [[1.0, 2.0], [1.0, 3.0]])

    assert strategy.told == [[1.0, 2.0], [1.0, 3.0]]


def test_a_batch_that_proposes_only_known_cells_runs_nothing(tmp_path):
    controller, store, backend, strategy = _run(tmp_path, [[1.0, 2.0], [2.0, 1.0]])

    assert len(_units(store)) == 2                # batch 1 recorded no new cell
    assert backend.batch_runs == [2]              # and executed no runs at all
    assert strategy.told == [[1.0, 2.0], [1.0, 2.0]]
    assert controller._runs_done == 4             # noqa: SLF001


def test_a_recalled_cell_is_not_counted_as_a_new_evaluation(tmp_path):
    """An `evaluations` budget counts cells SCORED. Counting a recalled one again would
    let a search exhaust that budget without measuring anything."""
    controller, _, _, _ = _run(tmp_path, [[1.0, 2.0], [2.0, 1.0]])

    assert controller._evaluations_done == 2      # noqa: SLF001 - the two real cells


def test_a_run_budget_is_spent_only_on_what_is_measured(tmp_path):
    cfg = _cfg(batches=3, per_batch=2)
    strategy = ScriptedStrategy(cfg, [[1.0, 2.0], [1.0, 2.0], [1.0, 2.0]])
    controller, _, backend = _search_controller(
        cfg, tmp_path, strategy=strategy, runs=2, compose=FakeCompose())
    controller.run()

    # Three batches proposing the same two cells cost one batch's compute.
    assert backend.batch_runs == [2]
    assert controller._runs_done == 4             # noqa: SLF001
    assert controller._batches_done == 3          # noqa: SLF001 - the loop still ran
