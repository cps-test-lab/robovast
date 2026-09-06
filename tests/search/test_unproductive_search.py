# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""A search that cannot produce an evaluation is stopped, not run to its budget.

Dropping the draw a plugin refuses, and the cell whose every run was lost, is what keeps
one bad proposal from ending a campaign. The cost of that tolerance is a campaign that
tolerates its way through the whole budget: nothing composes, or nothing measures, every
batch, and the search reports success having scored nothing.

So the framework watches for the symptom instead of trying to tell the causes apart in
advance: a batch that measured nothing. One is luck -- a mostly-infeasible space produces
one by chance -- and two running is a campaign that cannot produce.
"""

# Tests drive the controller directly and read its private counters.
# pylint: disable=import-outside-toplevel,protected-access

import os

from robovast.common.config import SearchConfig
from robovast.common.store import STORE_FILENAME, CampaignStore
from robovast.execution.backends import ExecutionBackend, RunOptions
from robovast.execution.controller import EMPTY_BATCH_LIMIT, CampaignController
from robovast.search.evaluator import Evaluator
from robovast.search.strategy import build_strategy

_BUDGET_BATCHES = 6


def _cfg(per_batch=2):
    return SearchConfig(
        strategy="random",
        search_space={"x": {"type": "float", "low": 0, "high": 1}},
        extract={"plugin": "failure_rate"},
        objectives=[{"name": "failure_rate", "direction": "maximize"}],
        per_batch=per_batch, budget=[{"batches": _BUDGET_BATCHES}], seed=1,
    )


class _Backend(ExecutionBackend):
    """Writes a result per run, or nothing at all -- a cell whose every run was lost."""

    def __init__(self, produces=True):
        self.produces = produces
        self.batches = 0

    def run_batch(self, campaign_data, *, campaign_root, batch_tag, runs, options,
                  whole_campaign=False):
        self.batches += 1
        if not self.produces:
            return
        for cfg in campaign_data["configs"]:
            for run in range(runs):
                run_dir = os.path.join(campaign_root, cfg["name"], str(run))
                os.makedirs(run_dir, exist_ok=True)
                with open(os.path.join(run_dir, "test.xml"), "w") as f:
                    f.write('<testsuite errors="0" failures="1" tests="1">'
                            '<testcase name="t" time="1.0"/></testsuite>')


class _Compose:
    """Composes nothing at all, or nothing until *composes_from* -- the shape
    ``Compose._resolve_names`` leaves when every draw was refused or unrealizable."""

    def __init__(self, composes_from=None):
        self.composes_from = composes_from
        self.calls = 0

    def compose(self, param_sets, output_dir):
        self.calls += 1
        composing = (self.composes_from is not None
                     and self.calls >= self.composes_from)
        name_by_id = {ps.id: f"c{ps.id}" for ps in param_sets} if composing else {}
        campaign_data = {
            "execution": {"containers": {"scenario": {"image": "img"}}, "runs": 1},
            "configs": [{"name": n} for n in name_by_id.values()]}
        return campaign_data, name_by_id


def _controller(cfg, tmp_path, backend, compose):
    from robovast.search.stopping import build_stop_conditions
    store = CampaignStore(tmp_path / "camp" / STORE_FILENAME)
    controller = CampaignController(
        campaign_id="camp", results_dir=str(tmp_path), runs=1, backend=backend,
        options=RunOptions(), store=store, campaign_config_dump={"version": 1},
        vast_dir=str(tmp_path), strategy=build_strategy(cfg),
        evaluator=Evaluator(cfg, str(tmp_path)), compose=compose,
        per_batch=cfg.per_batch, stop_conditions=build_stop_conditions(cfg))
    return controller, store


def test_a_search_that_composes_nothing_stops_instead_of_spending_its_budget(tmp_path):
    """Every draw refused, batch after batch. The campaign has a six-batch budget and must
    not spend it to learn the same thing six times."""
    cfg = _cfg()
    compose = _Compose()
    controller, store = _controller(cfg, tmp_path, _Backend(), compose)

    report = controller.run()

    assert report.extra["stop"]["kind"] == "unproductive"
    assert report.extra["stop"]["batches"] == EMPTY_BATCH_LIMIT
    assert compose.calls == EMPTY_BATCH_LIMIT
    assert not report.evaluations
    store.close()


def test_the_reason_says_which_end_of_the_campaign_produced_nothing(tmp_path):
    """Nothing composed and nothing measured send a reader to different files, so the
    stop names which of the two happened rather than that the batch was empty."""
    cfg = _cfg()
    controller, store = _controller(cfg, tmp_path, _Backend(), _Compose())
    composed_nothing = controller.run().extra["stop"]["reason"]
    store.close()

    # The other end: the cells compose and run, and no run produces a result.
    cfg = _cfg()
    controller, store = _controller(
        cfg, tmp_path / "b", _Backend(produces=False), _Compose(composes_from=1))
    measured_nothing = controller.run().extra["stop"]["reason"]
    store.close()

    assert "no parameter set could be composed" in composed_nothing
    assert "search_space" in composed_nothing
    assert "no measurable sample" in measured_nothing
    assert "extractor" in measured_nothing


def test_one_empty_batch_is_luck_and_does_not_stop_the_search(tmp_path):
    """A space that is mostly -- not entirely -- unrealizable produces an empty batch by
    chance. Stopping on the first would end a search that was working."""
    cfg = _cfg()
    compose = _Compose(composes_from=2)   # the first batch composes nothing
    controller, store = _controller(cfg, tmp_path, _Backend(), compose)

    report = controller.run()

    assert report.extra["stop"]["kind"] == "batches"
    assert report.extra["stop"]["batches"] == _BUDGET_BATCHES
    assert len(report.evaluations) == (_BUDGET_BATCHES - 1) * cfg.per_batch
    store.close()
