# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""A resumed search recounts the runs it already spent, from what each cell was allocated.

``search.repetitions`` gives each cell its own ``n_reps``, and the live loop counts a
batch's cost from exactly that -- executions attempted, summed per cell, which is what a
``runs`` budget caps. Nothing recorded it. So a service restart re-derived every cell's
cost as ``execution.runs``, and a campaign whose whole point was to spend unevenly had its
budget recounted as if it had spent evenly: under-counted where the policy had spent above
the default, over-counted where it had spent below. A `runs` budget then stopped the search
in the wrong place, and the further the policy was from the default the further out it was.

The allocation is now recorded per unit and read back, so the resumed count is the count
the live loop made.
"""

import sqlite3

from robovast.common.config import RepetitionsConfig, SearchConfig
from robovast.common.store import STORE_FILENAME, CampaignStore
from robovast.search.history import recorded_batches
from robovast.search.repetitions import build_repetition_policy
from robovast.search.types import ParamSet

from .test_loop_and_store import FakeCompose, _search_controller

SPACE = {"x": {"type": "float", "low": 0.0, "high": 1.0}}


def _cfg(batches=2, per_batch=2):
    return SearchConfig(
        strategy="random", search_space=SPACE, extract={"plugin": "failure_rate"},
        objectives=[{"name": "failure_rate", "direction": "maximize"}],
        per_batch=per_batch, budget=[{"batches": batches}], seed=1)


class FixedRepsStrategy:
    """Proposes cells that carry their own ``n_reps`` -- the channel a noise-aware
    strategy speaks through, and the one the repetition policy leaves alone."""

    RESUMABLE = True

    def __init__(self, cfg, reps):
        self.objectives = cfg.objectives
        self.reps = reps
        self._n = 0

    def ask(self, n):
        out = [ParamSet(values={"x": float(self._n * 10 + i)}, n_reps=self.reps[i % len(self.reps)])
               for i in range(n)]
        self._n += 1
        return out

    def tell(self, evaluations):
        pass

    def resume(self, batches):
        for batch in batches:
            self.ask(batch.asked)
            self.tell(batch.evaluations)

    def report(self):
        from robovast.search.types import SearchReport
        return SearchReport(extra={})


def _rehydrate(controller, store, campaign_id=1):
    """Seed the counters `_run_search` owns, then replay -- the resume path in isolation."""
    controller.store = store
    controller._batches_done = 0                     # noqa: SLF001
    controller._evaluations_done = 0                 # noqa: SLF001
    controller._runs_done = 0                        # noqa: SLF001
    controller._rehydrate_search(campaign_id)        # noqa: SLF001
    return controller._runs_done                     # noqa: SLF001


def _run(tmp_path, reps, default_runs=2):
    cfg = _cfg()
    controller, store, backend = _search_controller(
        cfg, tmp_path, strategy=FixedRepsStrategy(cfg, reps), runs=default_runs,
        compose=FakeCompose())
    controller.run()
    return controller, store, backend


def test_the_allocation_is_recorded_per_cell(tmp_path):
    _, store, backend = _run(tmp_path, reps=[5, 1])

    # Two cells per batch, one at 5 reps and one at 1 -- so two run_batch calls per batch.
    assert backend.batch_runs == [1, 5, 1, 5]
    conn = sqlite3.connect(store.db_path)
    assert sorted(r[0] for r in conn.execute("SELECT n_reps FROM unit")) == [1, 1, 5, 5]


def test_a_resumed_search_recounts_what_the_live_loop_counted(tmp_path):
    """The number under test: 12 runs (two batches of 5 + 1), not 8 (four cells x the
    campaign default of 2)."""
    live, store, _ = _run(tmp_path, reps=[5, 1])
    assert live._runs_done == 12                     # noqa: SLF001 - the count under test

    cfg = _cfg()
    resumed, _, _ = _search_controller(
        cfg, tmp_path, strategy=FixedRepsStrategy(cfg, [5, 1]), runs=2,
        compose=FakeCompose())
    assert _rehydrate(resumed, store) == 12


def test_a_cell_the_policy_sized_is_recounted_at_its_own_size(tmp_path):
    """The same, through `search.repetitions` rather than a strategy that speaks for
    itself -- the policy is what makes a campaign's cells cost different amounts."""
    cfg = _cfg(per_batch=3)
    policy = build_repetition_policy(
        RepetitionsConfig(policy="fixed", min=4, max=4), SPACE, default_runs=4)
    controller, store, _ = _search_controller(
        cfg, tmp_path, runs=2, compose=FakeCompose())
    controller.repetition_policy = policy
    controller.run()

    # The policy spends 4 per cell; the campaign default is 2. Six cells, 24 runs.
    assert controller._runs_done == 24               # noqa: SLF001

    resumed, _, _ = _search_controller(cfg, tmp_path, runs=2, compose=FakeCompose())
    assert _rehydrate(resumed, store) == 24


def test_a_store_that_recorded_no_allocation_falls_back_to_the_campaign_default(tmp_path):
    """A store written before the column: every cell cost `execution.runs`, which is what
    it did, because a per-cell allocation could not be recorded and was not used."""
    store = CampaignStore(tmp_path / STORE_FILENAME)
    campaign_id = store.create_campaign(name="c", mode="search", config_dir=str(tmp_path),
                                        config={})
    batch_id = store.open_batch(campaign_id, 0, ".", asked=2)
    for i in range(2):
        store.record_unit(batch_id=batch_id, paramset_id=f"p{i}", config_name=f"c{i}",
                          params={"x": i}, objectives={"failure_rate": 1.0}, measures={},
                          n_samples=1, status="evaluated", result_dir=f"c{i}")

    assert [b.reps for b in recorded_batches(store, campaign_id)] == [[None, None]]

    cfg = _cfg()
    controller, _, _ = _search_controller(cfg, tmp_path, runs=3, compose=FakeCompose())
    assert _rehydrate(controller, store, campaign_id) == 6
