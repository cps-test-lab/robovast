# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""The nested minimax reads the direction the campaign declared, at both of its ends.

A minimax has two orderings and they point opposite ways: the inner adversary keeps the
WORST environment it can find for a tuning, and the outer search keeps the tuning whose
worst case is LEAST bad. Both were written as raw comparisons -- `min()` for the adversary,
`sorted(reverse=True)` for the outer -- which is correct for a minimized objective and
inverted for a maximized one. `nav_search_minimax.vast` declares `direction: minimize`, so
the shipped example was right and the strategy was right by coincidence.

Under `maximize` -- the natural direction for an objective like a failure rate, and what
anyone copying this template for their own experiment is likely to declare -- the adversary
kept the mildest environment it found and `report` crowned the LEAST robust tuning, both
silently and both presented as the answer. This is the same defect the `qd` strategy's
report already had and fixed ("`max` alone returned the archive's WORST cell for every
minimizing search, and returned it as the answer").

Both ends now compare `SearchStrategy.objective_value`, which orients so higher is always
better, and the report converts back to the objective's own units -- a report in negated
units is one nobody can check against the campaign's data.
"""

import importlib.util
import pathlib

import pytest

from robovast.common.config import SearchConfig
from robovast.search.types import Evaluation

EXAMPLE = pathlib.Path(__file__).resolve().parents[2] / "configs" / "examples" / "nav_search"

OUTER, INNER = "speed_limit", "walker_dwell"
SPACE = {OUTER: {"type": "float", "low": 0.0, "high": 1.0},
         INNER: {"type": "float", "low": 0.0, "high": 8.0}}


def _minimax_cls():
    if not (EXAMPLE / "search" / "minimax.py").is_file():
        pytest.skip("nav_search example not present")
    spec = importlib.util.spec_from_file_location(
        "minimax_under_test", EXAMPLE / "search" / "minimax.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Minimax


def _strategy(direction, *, inner_budget=2, outer_candidates=2):
    cls = _minimax_cls()
    cfg = SearchConfig(
        strategy="./search/minimax.py:Minimax", search_space=SPACE,
        objectives=[{"name": "score", "direction": direction}],
        per_batch=4, extract={"plugin": "failure_rate"}, budget=[{"batches": 2}], seed=1,
        strategy_parameters={"outer": [OUTER], "inner": [INNER],
                             "inner_budget": inner_budget,
                             "outer_candidates": outer_candidates})
    return cls(cfg, cls.PARAMS_MODEL(**cfg.strategy_parameters))


def _tell(strategy, param_sets, values):
    strategy.tell([Evaluation(params=ps, objectives={"score": v}, n_samples=1)
                   for ps, v in zip(param_sets, values)])


def _drive(direction, values):
    """One batch of four: two draws against tuning A, then two against tuning B."""
    strategy = _strategy(direction)
    param_sets = strategy.ask(4)
    _tell(strategy, param_sets, values)
    return strategy.report().extra, param_sets


# Tuning A sees 0.9 and 0.1; tuning B sees 0.6 and 0.5.
VALUES = [0.9, 0.1, 0.6, 0.5]


def test_a_minimized_objective_keeps_the_low_end_and_prefers_the_higher_worst_case():
    """The direction this example declares. A's worst is 0.1, B's is 0.5; the tuning that
    holds up best under attack is B."""
    extra, param_sets = _drive("minimize", VALUES)

    assert extra["outer_best"] == pytest.approx(0.5)
    assert extra["robust_tuning"][OUTER] == param_sets[2].values[OUTER]
    assert [t["worst_robustness"] for t in extra["worst_case_by_tuning"]] == \
        pytest.approx([0.5, 0.1])


def test_a_maximized_objective_flips_both_ends():
    """A failure rate, say: the campaign declares `maximize`, so the inner search IS hunting
    the high end and a tuning's worst case is the highest value provoked -- A 0.9, B 0.6 --
    and the tuning that holds up best is the one whose worst case is LOWEST, B at 0.6.

    Before, both ends ran the other way: the adversary kept the mildest environment it had
    found (A 0.1, B 0.5) and ranked on that, so every published worst case understated the
    severity it was there to report."""
    extra, param_sets = _drive("maximize", VALUES)

    assert extra["outer_best"] == pytest.approx(0.6)
    assert extra["robust_tuning"][OUTER] == param_sets[2].values[OUTER]
    assert [t["worst_robustness"] for t in extra["worst_case_by_tuning"]] == \
        pytest.approx([0.6, 0.9])


def test_a_maximized_objective_can_crown_the_wrong_tuning_outright():
    """Understated numbers are the mild half. Where the two orderings disagree, the winner
    itself inverts: A is provoked to 0.9 and B only to 0.6, so B is the robust one -- but
    ranking on the mildest value seen (A 0.5, B 0.1) puts A first and reports the tuning
    that fails HARDER as the one to ship."""
    extra, param_sets = _drive("maximize", [0.9, 0.5, 0.6, 0.1])

    assert extra["outer_best"] == pytest.approx(0.6)
    assert extra["robust_tuning"][OUTER] == param_sets[2].values[OUTER]   # B, not A


def test_the_report_is_in_the_objectives_own_units():
    """Not the negated ones the ranking uses internally: every number here has to be
    comparable with what the campaign recorded."""
    extra, _ = _drive("minimize", VALUES)

    assert extra["outer_best"] > 0                     # not -0.5
    assert all(t["worst_robustness"] > 0 for t in extra["worst_case_by_tuning"])


def test_an_untested_tuning_is_never_reported_as_the_winner():
    """A tuning whose adversary never ran has no worst case, and reporting one as unbeaten
    would make an untested candidate the answer."""
    strategy = _strategy("maximize", inner_budget=2, outer_candidates=3)
    param_sets = strategy.ask(6)
    _tell(strategy, param_sets[:2], [0.4, 0.3])        # only the first tuning was evaluated

    extra = strategy.report().extra
    assert extra["tunings_scored"] == 1
    assert extra["tunings_total"] == 3
    assert extra["robust_tuning"][OUTER] == param_sets[0].values[OUTER]


def test_no_evaluations_at_all_reports_no_winner():
    strategy = _strategy("maximize")
    strategy.ask(4)
    strategy.tell([])

    extra = strategy.report().extra
    assert extra["outer_best"] is None
    assert extra["robust_tuning"] is None


def test_a_dimension_cannot_be_both_chosen_and_imposed():
    cls = _minimax_cls()
    cfg = SearchConfig(
        strategy="./search/minimax.py:Minimax", search_space=SPACE,
        objectives=[{"name": "score", "direction": "minimize"}],
        per_batch=2, extract={"plugin": "failure_rate"}, budget=[{"batches": 1}],
        strategy_parameters={"outer": [OUTER], "inner": [OUTER, INNER]})
    with pytest.raises(ValueError, match="both outer and inner"):
        cls(cfg, cls.PARAMS_MODEL(**cfg.strategy_parameters))
