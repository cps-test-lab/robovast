# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A batch says whether its failure counts have been counted, because 0 cannot.

``batch_runs_failed`` is written once per batch, when that batch's per-run verdicts are
tallied. Until then it reads 0 -- and 0 is also what a batch that lost nothing reads. A
poll partway through a batch that had already lost runs therefore reported a clean sweep,
and was believed: three separate readers acted on it.

The number cannot carry the distinction, so a flag beside it does. The rule underneath is
unchanged -- a run's own JUnit verdict is the authority on that run -- and this says when
the convenient aggregate is worth reading.
"""

# Exercises controller internals the way the sibling controller suites do.
# pylint: disable=import-outside-toplevel,protected-access

import os

from robovast.common.config import SearchConfig
from robovast.common.store import STORE_FILENAME, CampaignStore
from robovast.execution.backends import ExecutionBackend, RunOptions
from robovast.execution.control_server import ControllerState
from robovast.execution.controller import CampaignController
from robovast.search.evaluator import Evaluator
from robovast.search.stopping import build_stop_conditions
from robovast.search.strategy import build_strategy


def _write_test_xml(run_dir, failures):
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "test.xml"), "w") as handle:
        handle.write(f'<testsuite errors="0" failures="{failures}" tests="1">'
                     f'<testcase name="t" time="1.0"/></testsuite>')


class _Backend(ExecutionBackend):
    """Writes a per-config ``test.xml``; every config fails when *failures* is 1."""

    def __init__(self, failures=1, watch=None):
        self.failures = failures
        self._watch = watch

    def run_batch(self, campaign_data, *, campaign_root, batch_tag, runs, options,
                  whole_campaign=False):
        for cfg in campaign_data["configs"]:
            for run in range(runs):
                _write_test_xml(os.path.join(campaign_root, cfg["name"], str(run)),
                                self.failures)
        # Sampled from inside the batch, after every run has delivered a verdict and
        # before anything has tallied them: the exact moment a mid-batch poll reads.
        if self._watch is not None:
            self._watch.append(self._watch[0].snapshot().runs.model_copy())


class _Compose:
    def compose(self, param_sets, output_dir):
        names = {ps.id: f"c{ps.id}" for ps in param_sets}
        return ({"execution": {"containers": {"scenario": {"image": "img"}}, "runs": 1},
                 "configs": [{"name": n} for n in names.values()]}, names)


def _cfg(batches=1, per_batch=2):
    return SearchConfig(
        strategy="random",
        search_space={"x": {"type": "float", "low": 0, "high": 1}},
        extract={"plugin": "failure_rate"},
        objectives=[{"name": "failure_rate", "direction": "maximize"}],
        per_batch=per_batch, budget=[{"batches": batches}], seed=1,
    )


# -- the default is the honest one -------------------------------------------


def test_a_fresh_run_progress_does_not_claim_to_have_counted():
    """Nobody has tallied anything, so it must not read as "counted, and clean"."""
    from robovast.client.status import RunProgress

    assert RunProgress().outcomes_counted is False


def test_a_new_batch_goes_back_to_not_counted(tmp_path):
    """Otherwise the previous batch's "counted" would vouch for this batch's zeros."""
    state = ControllerState()
    store = CampaignStore(tmp_path / "camp" / STORE_FILENAME)
    controller = CampaignController(
        campaign_id="camp", results_dir=str(tmp_path), runs=1, backend=_Backend(),
        options=RunOptions(), store=store, campaign_config_dump={"version": 1},
        vast_dir=str(tmp_path), batch_campaign_data={"configs": []}, state=state)
    controller._poller = object()  # the progress path, without a live thread

    state.update_runs(failed=3, outcomes_counted=True)
    controller._begin_batch_progress(4)

    runs = state.snapshot().runs
    assert (runs.failed, runs.outcomes_counted) == (0, False)
    store.close()


# -- a search batch ----------------------------------------------------------


def test_a_search_batch_that_lost_runs_reports_them_as_counted(tmp_path):
    state = ControllerState()
    store = CampaignStore(tmp_path / "camp" / STORE_FILENAME)
    cfg = _cfg()
    controller = CampaignController(
        campaign_id="camp", results_dir=str(tmp_path), runs=2, backend=_Backend(failures=1),
        options=RunOptions(), store=store, campaign_config_dump={"version": 1},
        vast_dir=str(tmp_path), strategy=build_strategy(cfg),
        evaluator=Evaluator(cfg, str(tmp_path)), compose=_Compose(),
        per_batch=cfg.per_batch, stop_conditions=build_stop_conditions(cfg),
        state=state)
    controller.run()

    runs = state.snapshot().runs
    assert runs.failed > 0, "the fixture must lose runs"
    assert runs.outcomes_counted is True
    store.close()


def test_a_poll_inside_a_batch_that_has_already_lost_runs_says_it_has_not_counted(
        tmp_path):
    """**The symptom, at the moment it was read.**

    Every run of this batch has written a failing verdict, and nothing has tallied them.
    ``batch_runs_failed`` genuinely reads 0 here and there is no bug in the count -- it
    answers "as of the last sync point". What was missing is anything saying so, and this
    is the read that misled an operator and two agents into treating the batch as healthy.
    """
    state = ControllerState()
    store = CampaignStore(tmp_path / "camp" / STORE_FILENAME)
    cfg = _cfg()
    watch = [state]
    controller = CampaignController(
        campaign_id="camp", results_dir=str(tmp_path), runs=2,
        backend=_Backend(failures=1, watch=watch),
        options=RunOptions(), store=store, campaign_config_dump={"version": 1},
        vast_dir=str(tmp_path), strategy=build_strategy(cfg),
        evaluator=Evaluator(cfg, str(tmp_path)), compose=_Compose(),
        per_batch=cfg.per_batch, stop_conditions=build_stop_conditions(cfg),
        state=state)
    controller.run()

    mid_batch = watch[1]
    assert mid_batch.failed == 0, "the fixture must catch the read before the tally"
    assert mid_batch.outcomes_counted is False, (
        "a poll mid-batch reported 0 failures with nothing to say it had not counted")
    # And by the end the same batch's real losses are both counted and reported.
    final = state.snapshot().runs
    assert final.failed > 0 and final.outcomes_counted is True
    store.close()


def test_a_search_batch_that_lost_nothing_still_reports_that_it_counted(tmp_path):
    """**The regression.** A clean batch used to skip the write altogether.

    So its ``failed: 0`` was indistinguishable from the 0 a batch reads before anyone has
    looked -- and a clean batch is the common case, which is what made the misreading
    reliable rather than occasional.
    """
    state = ControllerState()
    store = CampaignStore(tmp_path / "camp" / STORE_FILENAME)
    cfg = _cfg()
    controller = CampaignController(
        campaign_id="camp", results_dir=str(tmp_path), runs=2, backend=_Backend(failures=0),
        options=RunOptions(), store=store, campaign_config_dump={"version": 1},
        vast_dir=str(tmp_path), strategy=build_strategy(cfg),
        evaluator=Evaluator(cfg, str(tmp_path)), compose=_Compose(),
        per_batch=cfg.per_batch, stop_conditions=build_stop_conditions(cfg),
        state=state)
    controller.run()

    runs = state.snapshot().runs
    assert runs.failed == 0, "the fixture must lose nothing"
    assert runs.outcomes_counted is True
    store.close()


# -- a batch-mode campaign ---------------------------------------------------


def test_a_batch_mode_campaign_reports_its_tally_as_counted(tmp_path):
    state = ControllerState()
    store = CampaignStore(tmp_path / "camp" / STORE_FILENAME)
    campaign_data = {"execution": {"containers": {"scenario": {"image": "img"}}, "runs": 2},
                     "configs": [{"name": "ca", "config": {}}]}
    controller = CampaignController(
        campaign_id="camp", results_dir=str(tmp_path), runs=2, backend=_Backend(failures=0),
        options=RunOptions(), store=store, campaign_config_dump={"version": 1},
        vast_dir=str(tmp_path), batch_campaign_data=campaign_data, state=state)
    controller.run()

    assert state.snapshot().runs.outcomes_counted is True
    store.close()


# -- recovered from disk -----------------------------------------------------


def test_a_status_recovered_from_disk_is_counted_by_construction():
    """Its counters come from verdicts already written, so there is no later moment."""
    from robovast.execution.status_recovery import _runs_from_verdicts

    payload = _runs_from_verdicts({"num_runs": 4, "num_passed": 3, "num_failed": 1}, 4)
    assert payload["outcomes_counted"] is True


# -- what a caller of the status tool sees -----------------------------------


def test_the_status_tool_reports_the_flag_beside_the_count():
    """A caller that has not read the docstring is the caller that misreads the number."""
    from robovast.client.status import RunProgress, Status
    from robovast.mcp_server.plugins.execution import _status_to_dict

    mid_batch = Status(phase="running", mode="search",
                       runs=RunProgress(completed=1, total=4))
    result = _status_to_dict("camp", "service", mid_batch)
    assert result["batch_runs_failed"] == 0
    assert result["batch_outcomes_counted"] is False

    tallied = Status(phase="running", mode="search",
                     runs=RunProgress(completed=4, total=4, outcomes_counted=True))
    assert _status_to_dict("camp", "service", tallied)["batch_outcomes_counted"] is True


def test_a_status_with_no_run_progress_at_all_is_not_counted():
    """``initializing`` has no runs sub-model; absent must not read as "counted"."""
    from robovast.client.status import Status
    from robovast.mcp_server.plugins.execution import _status_to_dict

    result = _status_to_dict("camp", "service", Status(phase="initializing"))
    assert result["batch_outcomes_counted"] is False
