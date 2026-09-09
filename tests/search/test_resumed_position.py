# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""A search re-entered after a restart continues from the position it actually reached.

The position lives in the campaign's store; the loop carries it in counters. Rebuilding those
one attribute at a time is what these tests exist to prevent, because a counter left behind
reads as zero and zero is a legal value. Two of them decide when the search STOPS rather than
what it displays: without the best-so-far a resumed ``target_objective`` cannot know it is
already met and spends another whole batch, and without the per-batch best history
``no_improvement`` cannot know how long the search has been flat -- while the staleness a
reader is shown, computed separately from the store, is not the one being acted on.

So the whole record folds into one ``SearchPosition``, and a fresh campaign folds to the zero
position by the same path. These tests hold that, including a field-completeness guard so a
field added to the position later cannot be quietly left out of the resume.
"""

import dataclasses

from robovast.execution.control_server import ControllerState
from robovast.search.history import SearchPosition, position_from, recorded_batches
from robovast.common.config import NoImprovementStop, SearchConfig
from robovast.search.stopping import StopConditions, StopSnapshot

from .test_loop_and_store import FakeCompose, _search_controller

SPACE = {"x": {"type": "float", "low": 0.0, "high": 1.0}}


def _cfg(batches=3, per_batch=2, stopping=None):
    return SearchConfig(
        strategy="random", search_space=SPACE, extract={"plugin": "failure_rate"},
        objectives=[{"name": "failure_rate", "direction": "maximize"}],
        per_batch=per_batch, budget=[{"batches": batches}],
        stopping=stopping or [], seed=1)


def _live_run(tmp_path, cfg=None):
    """Run a search to completion and hand back the controller and its store."""
    cfg = cfg or _cfg()
    controller, store, _ = _search_controller(cfg, tmp_path, compose=FakeCompose())
    controller.run()
    return controller, store, cfg


def _resumed(tmp_path, cfg, store):
    """A second controller over the same store -- the restart, in isolation."""
    controller, _, _ = _search_controller(cfg, tmp_path, compose=FakeCompose())
    controller.store = store
    obj = cfg.objectives[0].name
    position = controller._rehydrate_search(                       # noqa: SLF001
        1, lambda best, evs: controller._update_best(best, evs, obj))  # noqa: SLF001
    return controller, position


def test_every_position_field_is_accounted_for():
    """The guard that makes the rest of this file keep working.

    A field added to :class:`SearchPosition` is a field the resume has to carry, and the
    way that goes wrong is silently -- nobody notices a counter that reads zero. Naming the
    set here fails the moment one appears, so the tests below have to be extended rather
    than merely still passing.
    """
    assert {f.name for f in dataclasses.fields(SearchPosition)} == {
        "batches", "evaluations", "runs", "history",
        "best_objective", "best_per_batch", "age_s"}


def test_an_empty_record_folds_to_the_zero_position():
    """A campaign starting now takes the same path as one being re-entered, which is why
    the fold has no resume branch to get wrong."""
    assert position_from([], default_runs=2, fold_best=lambda b, e: b) == SearchPosition()


def test_a_resumed_controller_reaches_the_position_the_live_one_left(tmp_path):
    controller, store, cfg = _live_run(tmp_path)
    live = (controller._batches_done, controller._evaluations_done,   # noqa: SLF001
            controller._runs_done, len(controller._history))          # noqa: SLF001

    _, position = _resumed(tmp_path, cfg, store)

    assert (position.batches, position.evaluations, position.runs,
            len(position.history)) == live
    assert position.batches == 3


def test_the_best_so_far_survives_the_restart(tmp_path):
    """Without this a resumed ``target_objective`` reports ``—`` for a batch, and a search
    that had already met its target spends another one before noticing."""
    controller, store, cfg = _live_run(tmp_path)
    live_best = max(next(iter(e.objectives.values()))
                    for e in controller._history)                     # noqa: SLF001

    _, position = _resumed(tmp_path, cfg, store)

    assert position.best_objective == live_best
    assert position.best_objective is not None


def test_the_per_batch_best_history_survives_the_restart(tmp_path):
    """One entry per batch that had a best, which is the shape ``_stale_batches`` walks."""
    _, store, cfg = _live_run(tmp_path)
    _, position = _resumed(tmp_path, cfg, store)

    assert len(position.best_per_batch) == position.batches
    # Best-so-far is monotone, so the list never goes backwards.
    assert position.best_per_batch == sorted(position.best_per_batch)
    assert position.best_per_batch[-1] == position.best_objective


def test_the_budget_is_published_from_the_position_not_from_zero(tmp_path):
    """The reported symptom: a resumed search showed ``0 / 30`` batches until its next
    batch closed, because the pre-loop publish wrote a hard-coded zero over the counters
    the resume had just restored."""
    _, store, cfg = _live_run(tmp_path)
    controller, position = _resumed(tmp_path, cfg, store)
    controller.state = ControllerState()
    controller._batches_done = position.batches                       # noqa: SLF001
    controller._publish_budget(controller.stop_conditions, StopSnapshot(  # noqa: SLF001
        batch=position.batches, elapsed=0.0, best_objective=position.best_objective,
        evaluations=position.evaluations, runs=position.runs))

    def batches_row():
        return next(b for b in controller.state.snapshot().budget if b.label == "batches")

    assert batches_row().current == position.batches == 3

    # The contrast, kept here rather than in a commit message: the snapshot this replaced
    # was a literal zero, and publishing one over a restored position is the whole of the
    # reported symptom. A future edit that reintroduces it fails on this line.
    controller._publish_budget(controller.stop_conditions,            # noqa: SLF001
                               StopSnapshot(batch=0, elapsed=0.0))
    assert batches_row().current == 0


def test_no_improvement_fires_on_schedule_across_a_restart():
    """The bug with results in it rather than pixels.

    A stopping set is built from configuration alone, so a re-entered search used to start
    believing it had just improved and ran ``patience`` batches past its own convergence
    stop. Seeded, the flat stretch behind the restart still counts.
    """
    flat = [1.0, 1.0, 1.0]      # three batches behind the restart, all at the same best

    def conditions():
        return StopConditions(
            [], [NoImprovementStop(type="no_improvement", patience=2, min_delta=0.0)],
            "o", "maximize")

    seeded = conditions()
    seeded.seed_history(flat)
    fired = seeded.should_stop(StopSnapshot(batch=4, elapsed=0.0, best_objective=1.0))
    assert fired is not None and fired.kind == "no_improvement"

    # The same search without its history behind it: one batch in, nothing to be stale
    # against, and the search runs on past the point it should have converged.
    assert conditions().should_stop(
        StopSnapshot(batch=4, elapsed=0.0, best_objective=1.0)) is None


def test_a_resumed_search_replays_its_strategy_through_the_record(tmp_path):
    """The position is folded from the same batches the strategy is re-driven through, so
    the counters and the replay cannot describe different campaigns."""
    _, store, cfg = _live_run(tmp_path)
    batches = recorded_batches(store, 1)
    _, position = _resumed(tmp_path, cfg, store)

    assert position.batches == len(batches)
    assert position.evaluations == sum(len(b.evaluations) for b in batches)
