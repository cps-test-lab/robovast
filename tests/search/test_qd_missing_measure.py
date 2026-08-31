# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""An archive axis the extractor never reported is refused by name.

`Evaluator` refuses a missing OBJECTIVE with a message naming it and what the extractor did
return. A missing MEASURE had no such check: it reached `QDStrategy.tell` and came out as a
bare ``KeyError: 'clearance'`` from inside a list comprehension -- after the batch had run,
on a campaign the controller then aborted, because only `NoSampleError` is survivable there.
The exception named the axis and nothing else: not the cell, not what the extractor did
return, and not which of the two declarations -- the archive's or the extractor's -- was
the one to correct.

It is still fatal, and should be. The archive places a cell by all of its axes at once, so a
missing one leaves the cell nowhere to go; a substituted coordinate would file it somewhere
it was never measured, and dropping it quietly would empty the archive the campaign exists
to fill. The disagreement also recurs on every cell, so carrying on would spend the whole
budget producing nothing. What changes is that the campaign is now told what happened.
"""

import pytest

from robovast.common.config import SearchConfig
from robovast.search.strategy import build_strategy
from robovast.search.types import Evaluation

pytest.importorskip("ribs")

ARCHIVE = {"archive": {"measures": {"speed": {"low": 0.0, "high": 1.0},
                                    "clearance": {"low": 0.0, "high": 1.0}}}}


def _strategy(per_batch=2):
    cfg = SearchConfig(
        strategy="qd", search_space={"x": {"type": "float", "low": 0.0, "high": 1.0}},
        objectives=[{"name": "f", "direction": "maximize"}], per_batch=per_batch,
        extract={"plugin": "failure_rate"}, budget=[{"batches": 2}], seed=1,
        strategy_parameters=ARCHIVE)
    return build_strategy(cfg, "")


def _evals(param_sets, measures):
    return [Evaluation(params=ps, objectives={"f": 0.5}, measures=dict(measures), n_samples=1)
            for ps in param_sets]


def test_a_missing_axis_names_itself_the_cell_and_what_arrived():
    strategy = _strategy()
    param_sets = strategy.ask(2)

    with pytest.raises(ValueError) as excinfo:
        strategy.tell(_evals(param_sets, {"speed": 0.5}))

    message = str(excinfo.value)
    assert "clearance" in message                     # the axis that is missing
    assert "'speed'" in message                       # what the extractor did return
    assert param_sets[0].id in message                # which cell
    assert "archive.measures" in message              # and which declaration to correct


def test_a_missing_axis_is_refused_on_the_short_generation_path_too():
    """`_tell_incomplete` reads the same coordinates for the draws that did come back, so
    it has to refuse the same way -- otherwise the diagnosis depended on whether some
    unrelated draw in the batch happened to be unrealizable."""
    strategy = _strategy(per_batch=3)
    param_sets = strategy.ask(3)

    with pytest.raises(ValueError) as excinfo:
        strategy.tell(_evals(param_sets[:2], {"speed": 0.5}))   # short AND missing an axis

    assert "clearance" in str(excinfo.value)


def test_an_evaluation_reporting_every_axis_is_unaffected():
    strategy = _strategy()
    param_sets = strategy.ask(2)

    strategy.tell(_evals(param_sets, {"speed": 0.5, "clearance": 0.25}))

    assert strategy.report().extra["num_elites"] == 1     # both cells share one archive cell


def test_an_extra_measure_beside_the_declared_axes_is_ignored():
    """The archive reads the axes it declares. An extractor reporting more than that is
    explicitly allowed -- a nav extractor returns diagnostics beside the behaviour axes."""
    strategy = _strategy()
    param_sets = strategy.ask(2)

    strategy.tell(_evals(param_sets, {"speed": 0.5, "clearance": 0.25, "time_to_goal": 9.0}))

    assert strategy.report().extra["measure_names"] == ["speed", "clearance"]
