# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""QD (pyribs) and Optuna strategies, and the dimension/source-agnostic goal.

The QD/Optuna tests are skipped when the optional extra is not installed.
"""

# pylint: disable=import-outside-toplevel

import random

import pytest

from robovast.common.config import SearchConfig
from robovast.search.evaluator import Evaluator
from robovast.search.strategy import build_strategy
from robovast.search.types import Evaluation


def _cfg(strategy, search_space, objectives, strategy_parameters=None,
         batches=4, per_batch=8):
    return SearchConfig(
        strategy=strategy, search_space=search_space,
        extract={"plugin": "failure_rate"}, objectives=objectives,
        per_batch=per_batch, budget=[{"batches": batches}], seed=0,
        strategy_parameters=strategy_parameters or {},
    )


QUAD_SPACE = {
    "thrust": {"type": "float", "low": 0.3, "high": 3.0},
    "mass": {"type": "int", "low": 1, "high": 3},
    "mode": {"type": "choice", "values": ["a", "b", "c"]},
}


def _drive(strategy, gens, value_fn, measure_fn=None):
    rng = random.Random(0)
    for _ in range(gens):
        ps = strategy.ask(strategy.cfg.per_batch)
        evs = []
        for p in ps:
            measures = measure_fn(p, rng) if measure_fn else {}
            evs.append(Evaluation(params=p, objectives={"obj": value_fn(p, rng)},
                                  measures=measures))
        strategy.tell(evs)


# ---- QD ----

def test_qd_fills_archive():
    pytest.importorskip("ribs")
    cfg = _cfg("qd", QUAD_SPACE, [{"name": "obj", "direction": "maximize"}],
               strategy_parameters={"archive": {"type": "cvt", "cells": 64,
                   "measures": {"m1": {"low": 0, "high": 1}, "m2": {"low": 0, "high": 4}}}})
    s = build_strategy(cfg)
    _drive(s, 4, lambda p, r: r.random(),
           lambda p, r: {"m1": r.random(), "m2": r.random() * 4})
    rep = s.report()
    assert rep.extra["num_elites"] > 0
    assert 0.0 <= rep.extra["coverage"] <= 1.0
    assert len(rep.extra["elites"]) == rep.extra["num_elites"]


def test_qd_dimension_agnostic_different_shape():
    """A differently-shaped SUT (2 params + 3 measures) works with no code change."""
    pytest.importorskip("ribs")
    cfg = _cfg("qd",
               {"a": {"type": "float", "low": 0, "high": 10},
                "b": {"type": "int", "low": 0, "high": 5}},
               [{"name": "obj", "direction": "maximize"}],
               strategy_parameters={"archive": {"type": "cvt", "cells": 32,
                   "measures": {"p": {"low": 0, "high": 1}, "q": {"low": 0, "high": 1},
                                "r": {"low": 0, "high": 1}}}})
    s = build_strategy(cfg)
    _drive(s, 4, lambda p, r: r.random(),
           lambda p, r: {"p": r.random(), "q": r.random(), "r": r.random()})
    assert s.report().extra["num_elites"] > 0


def test_qd_grid_archive():
    pytest.importorskip("ribs")
    cfg = _cfg("qd", QUAD_SPACE, [{"name": "obj", "direction": "maximize"}],
               strategy_parameters={"archive": {"type": "grid",
                   "measures": {"m1": {"low": 0, "high": 1, "bins": 8},
                                "m2": {"low": 0, "high": 4, "bins": 8}}}})
    s = build_strategy(cfg)
    _drive(s, 3, lambda p, r: r.random(),
           lambda p, r: {"m1": r.random(), "m2": r.random() * 4})
    assert s.report().extra["num_elites"] > 0


@pytest.mark.parametrize("direction,better", [("minimize", min), ("maximize", max)])
def test_the_best_elite_is_best_by_the_campaign_direction(direction, better):
    """The elites carry RAW objective values, so which end is best is the direction again.

    The archive is always maximized internally -- a minimize objective is negated on the way
    in and un-negated on the way out -- and picking the best with a bare ``max`` after that
    second flip returned the archive's WORST cell for every minimizing search, labelled as
    the answer.
    """
    pytest.importorskip("ribs")
    cfg = _cfg("qd", QUAD_SPACE, [{"name": "obj", "direction": direction}],
               strategy_parameters={"archive": {"type": "grid",
                   "measures": {"m1": {"low": 0, "high": 1, "bins": 8}}}})
    s = build_strategy(cfg)
    _drive(s, 3, lambda p, r: r.random(), lambda p, r: {"m1": r.random()})
    extra = s.report().extra
    assert extra["elites"], "the archive must hold something for this to mean anything"
    assert extra["best_elite"]["objective"] == better(
        e["objective"] for e in extra["elites"])


# ---- Optuna ----

def test_optuna_tpe_improves_toward_optimum():
    pytest.importorskip("optuna")
    cfg = _cfg("optuna", QUAD_SPACE, [{"name": "obj", "direction": "maximize"}],
               strategy_parameters={"sampler": "tpe"}, batches=5)
    s = build_strategy(cfg)
    # objective rewards high thrust; TPE should push the best thrust up.
    _drive(s, 5, lambda p, r: p.values["thrust"] / 3.0 + r.random() * 0.02)
    rep = s.report()
    assert rep.best.params.values["thrust"] > 1.5      # better than midpoint
    assert rep.extra["n_trials"] == 40


def test_optuna_rejects_bad_sampler():
    pytest.importorskip("optuna")
    from pydantic import ValidationError
    cfg = _cfg("optuna", QUAD_SPACE, [{"name": "obj", "direction": "maximize"}],
               strategy_parameters={"sampler": "nope"})
    with pytest.raises(ValidationError):
        build_strategy(cfg)


# ---- source-agnostic: file-loaded extractor returns measures ----

def test_file_extractor_via_evaluator(tmp_path):
    """A local-file extractor (not metrics.csv) feeds objectives + measures."""
    ext = tmp_path / "myext.py"
    ext.write_text(
        "from robovast.search.extractor import Extractor, ExtractResult\n"
        "class E(Extractor):\n"
        "    def extract(self, config_dir):\n"
        "        return ExtractResult(objectives={'obj': 0.7}, measures={'m': 0.4})\n")
    cfg = SearchConfig(
        strategy="random", search_space={"x": {"type": "float", "low": 0, "high": 1}},
        extract={"plugin": "myext.py:E"},
        objectives=[{"name": "obj", "direction": "maximize"}],
        per_batch=1, budget=[{"batches": 1}], seed=0)
    from robovast.search.types import ParamSet
    config_dir = tmp_path / "c0"
    (config_dir / "0").mkdir(parents=True)
    (config_dir / "0" / "test.xml").write_text(
        '<testsuite errors="0" failures="1" tests="1"><testcase name="t" time="1"/></testsuite>')
    ev = Evaluator(cfg, vast_dir=str(tmp_path)).evaluate(config_dir, ParamSet(values={"x": 0.5}))
    assert ev.objectives == {"obj": 0.7} and ev.measures == {"m": 0.4} and ev.n_samples == 1


# ---- QD: a generation that comes back short ----

def _drive_qd(strategy, gens, *, drop=()):
    """Run *gens* generations, withholding the evaluation of the ask-order indices in
    *drop* on every generation — what the batch loop hands back when a draw could not be
    composed into a config, or when every run of a cell was lost."""
    rng = random.Random(0)
    for _ in range(gens):
        ps = strategy.ask(strategy.cfg.per_batch)
        evs = [Evaluation(params=p, objectives={"obj": rng.random()},
                          measures={"m1": rng.random(), "m2": rng.random() * 4})
               for i, p in enumerate(ps) if i not in drop]
        strategy.tell(evs)


def _qd(per_batch=8):
    return build_strategy(_cfg(
        "qd", QUAD_SPACE, [{"name": "obj", "direction": "maximize"}], per_batch=per_batch,
        strategy_parameters={"archive": {"type": "grid",
            "measures": {"m1": {"low": 0, "high": 1, "bins": 8},
                         "m2": {"low": 0, "high": 4, "bins": 8}}}}))


def test_qd_survives_a_draw_that_produced_no_evaluation():
    """One unrealizable draw must not end the search.

    pyribs needs one row per solution it emitted, so a short generation raises KeyError
    out of the batch loop and fails the campaign — a 50-batch search dying on batch 33
    with eight hours of finished work behind it.
    """
    pytest.importorskip("ribs")
    s = _qd()
    _drive_qd(s, 4, drop={3})
    assert s.report().extra["batches"] == 4


def test_qd_keeps_the_measured_cells_of_a_short_generation():
    """Skipping the emitter update must not throw away the runs that DID happen: the
    seven cells that ran are seven cells of archive, and they cost a batch of compute."""
    pytest.importorskip("ribs")
    s = _qd()
    _drive_qd(s, 1, drop={0})
    assert s.report().extra["num_elites"] > 0


def test_qd_invents_nothing_for_the_missing_draw():
    """Every elite must trace back to an evaluation. A worst-case objective standing in
    for the unevaluated draw would need invented measures too, and those land the fake in
    a real cell where the search then chases it."""
    pytest.importorskip("ribs")
    s = _qd(per_batch=4)
    dropped = []
    rng = random.Random(0)
    for _ in range(3):
        ps = s.ask(s.cfg.per_batch)
        dropped.append(ps[1].values)
        s.tell([Evaluation(params=p, objectives={"obj": rng.random()},
                           measures={"m1": rng.random(), "m2": rng.random() * 4})
                for i, p in enumerate(ps) if i != 1])
    elites = [e["params"] for e in s.report().extra["elites"]]
    for values in dropped:
        assert values not in elites


def test_qd_recovers_a_full_generation_after_a_short_one():
    """The abandoned generation costs one round of adaptation, not the search: the next
    ask/tell pair must go through the scheduler normally."""
    pytest.importorskip("ribs")
    s = _qd()
    _drive_qd(s, 1, drop={2})
    _drive_qd(s, 2)                      # full generations, straight through pyribs
    assert s.report().extra["batches"] == 3
    assert s.report().extra["num_elites"] > 0


def test_qd_survives_a_generation_with_no_evaluations_at_all():
    """The degenerate end of the same case — every draw in the batch unrealizable."""
    pytest.importorskip("ribs")
    s = _qd(per_batch=4)
    _drive_qd(s, 1, drop={0, 1, 2, 3})
    _drive_qd(s, 1)
    assert s.report().extra["num_elites"] > 0
