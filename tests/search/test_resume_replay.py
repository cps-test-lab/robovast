# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""A resumed search must propose what the uninterrupted one would have.

This is the test the whole search-resume design rests on. Nothing about a strategy's
internal state is serialized; a fresh strategy is re-driven through the ask/tell sequence
the campaign recorded, and from there it has to carry on identically. If the replay is
merely *plausible* rather than exact, a resumed campaign quietly becomes a different
experiment from the one it started as — and nothing downstream would ever say so.

It runs in-process against every shipped strategy: no cluster, no jobs, no store.
"""

# pylint: disable=import-outside-toplevel

import pytest

from robovast.common.config import SearchConfig
from robovast.search.history import RecordedBatch
from robovast.search.strategy import build_strategy
from robovast.search.types import Evaluation

#: Every strategy that ships here, with whatever parameters it insists on. `qd` and
#: `optuna` are exercised in their own modules; their samplers come from third-party
#: libraries whose determinism is that library's contract, not this one's.
STRATEGIES = {"random": {}, "halton": {}, "boundary": {"level": 0.5}}

PER_BATCH = 3
BATCHES = 6
RESUME_AFTER = 3


def _cfg(strategy):
    return SearchConfig(
        strategy=strategy, strategy_parameters=STRATEGIES[strategy],
        search_space={"x": {"type": "float", "low": 0.0, "high": 1.0},
                      "y": {"type": "float", "low": -2.0, "high": 2.0}},
        extract={"plugin": "failure_rate"},
        objectives=[{"name": "failure_rate", "direction": "maximize"}],
        per_batch=PER_BATCH, budget=[{"batches": BATCHES}], seed=7,
    )


def _score(param_sets):
    """A deterministic stand-in for running the batch."""
    return [Evaluation(params=ps, objectives={"failure_rate": float(ps.values["x"])},
                       n_samples=1)
            for ps in param_sets]


def _run(strategy, batches, drop_last_of_batch=()):
    """Drive *strategy* for *batches* rounds; return the proposals it made, per round.

    ``drop_last_of_batch`` names rounds where the final draw produced no evaluation — the
    shape a composition failure or a batch whose runs were all lost leaves behind.
    """
    proposed, recorded = [], []
    for i in range(batches):
        param_sets = strategy.ask(PER_BATCH)
        proposed.append([ps.values for ps in param_sets])
        told = _score(param_sets[:-1] if i in drop_last_of_batch else param_sets)
        strategy.tell(told)
        recorded.append(RecordedBatch(asked=len(param_sets), evaluations=told))
    return proposed, recorded


@pytest.mark.parametrize("name", STRATEGIES)
def test_a_resumed_search_proposes_what_it_would_have(name):
    """Six batches straight, against three + resume + three."""
    straight, recorded = _run(build_strategy(_cfg(name)), BATCHES)

    resumed = build_strategy(_cfg(name))
    resumed.resume(recorded[:RESUME_AFTER])
    after, _ = _run(resumed, BATCHES - RESUME_AFTER)

    assert after == straight[RESUME_AFTER:]


@pytest.mark.parametrize("name", STRATEGIES)
def test_a_draw_that_produced_no_evaluation_still_advanced_the_sequence(name):
    """The number PROPOSED is replayed, not the number told back.

    A draw the variation pipeline could not realize, or one whose every run was lost, costs
    a proposal and produces no evaluation. Replaying only the evaluations would leave the
    strategy's stream rewound by one, and every parameter set after it would differ.
    """
    dropped = {0, 2}
    straight, recorded = _run(build_strategy(_cfg(name)), BATCHES,
                              drop_last_of_batch=dropped)

    resumed = build_strategy(_cfg(name))
    resumed.resume(recorded[:RESUME_AFTER])
    after, _ = _run(resumed, BATCHES - RESUME_AFTER,
                    drop_last_of_batch={i - RESUME_AFTER for i in dropped
                                        if i >= RESUME_AFTER})

    assert after == straight[RESUME_AFTER:]


@pytest.mark.parametrize("name", STRATEGIES)
def test_replaying_only_the_evaluations_would_not_have_worked(name):
    """The negative control for the test above — the bug it exists to catch.

    Told without being asked, a strategy resumes with its sequence rewound. This asserts
    the difference is observable, so the test above is not passing by accident.
    """
    straight, recorded = _run(build_strategy(_cfg(name)), BATCHES)

    naive = build_strategy(_cfg(name))
    for batch in recorded[:RESUME_AFTER]:
        naive.tell(batch.evaluations)
    after, _ = _run(naive, BATCHES - RESUME_AFTER)

    assert after != straight[RESUME_AFTER:]


@pytest.mark.parametrize("name", STRATEGIES)
def test_resuming_nothing_is_a_search_that_never_ran(name):
    """A campaign interrupted before its first batch closed resumes from the start."""
    straight, _ = _run(build_strategy(_cfg(name)), 2)

    resumed = build_strategy(_cfg(name))
    resumed.resume([])
    after, _ = _run(resumed, 2)

    assert after == straight


def test_every_shipped_strategy_declares_itself_resumable():
    """The default is correct for a strategy that is a function of seed and evaluations.

    A strategy that is nondeterministic for a reason a seed cannot fix opts out; this
    guards against one of the shipped ones silently acquiring such a dependency.
    """
    for name in STRATEGIES:
        assert build_strategy(_cfg(name)).RESUMABLE is True


# -- through the controller -------------------------------------------------------------

def test_a_re_entered_controller_continues_where_the_store_left_off(tmp_path):
    """The loop already began at ``self._batches_done``; the resume only seeds it.

    A second controller over the same store and campaign root must finish the campaign's
    budget rather than start it again — four batches in total, not four more.
    """
    from tests.search.test_loop_and_store import _cfg as _loop_cfg
    from tests.search.test_loop_and_store import _search_controller

    cfg = _loop_cfg(batches=2, per_batch=2)
    first, store, backend = _search_controller(cfg, tmp_path)
    first.run()
    assert first._batches_done == 2
    ran_first = len(backend.batch_runs)

    # A fresh process: new controller, new strategy, the same store and campaign root.
    cfg4 = _loop_cfg(batches=4, per_batch=2)
    second, _, backend2 = _search_controller(cfg4, tmp_path)
    second.store = store
    second.run()

    assert second._batches_done == 4                  # finished the budget
    assert len(backend2.batch_runs) == 4 - ran_first  # only the batches still owed
    assert second._evaluations_done >= 4              # the earlier ones were counted


def test_a_campaign_with_no_recorded_batches_starts_from_zero(tmp_path):
    """The no-op that keeps the rehydration from being a mode."""
    from tests.search.test_loop_and_store import _cfg as _loop_cfg
    from tests.search.test_loop_and_store import _search_controller

    controller, _, backend = _search_controller(_loop_cfg(batches=2, per_batch=2), tmp_path)
    controller.run()

    assert controller._batches_done == 2
    assert len(backend.batch_runs) == 2
