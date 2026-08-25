# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Progress has to be able to *move*, and its clock has to move only when it does.

Together these are what make a hanging run visible: a campaign whose progress never
advances reported ``running, progress: 0`` indefinitely and looked exactly like a slow
one. Two separate defects had to hold for that: the local backend published no run
progress at all, and nothing recorded when progress last changed.
"""

from pathlib import Path

from robovast.common.config import (DEFAULT_RUN_DEADLINE_SECONDS, declared_job_seconds,
                                    declared_per_run_seconds, job_deadline_seconds)
from robovast.execution.backends import DockerBackend, ExecutionBackend
from robovast.execution.control_server import ControllerState


def _finish_run(campaign_root: Path, config: str, run: str) -> None:
    """Write the per-run JUnit report that marks a run as finished."""
    run_dir = campaign_root / config / run
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "test.xml").write_text("<testsuite/>")


# -- the local lane publishes run progress -----------------------------------


def test_the_local_backend_counts_finished_runs(tmp_path):
    """It used to return ``None`` ("results are already on disk"), which switched the
    controller's progress poller off entirely — so a live local campaign reported
    ``0/0`` and a progress that could never move."""
    backend = DockerBackend()
    assert backend.count_run_artifacts("camp", str(tmp_path)) == 0
    _finish_run(tmp_path, "cfg-a", "0")
    _finish_run(tmp_path, "cfg-a", "1")
    _finish_run(tmp_path, "cfg-b", "0")
    assert backend.count_run_artifacts("camp", str(tmp_path)) == 3


def test_reserved_campaign_dirs_cannot_inflate_the_count(tmp_path):
    """`_jobs`/`_config`/`_transient` sit at the same depth as configuration dirs, so
    an artifact under them must not read as a finished run."""
    _finish_run(tmp_path, "cfg-a", "0")
    for reserved in ("_jobs", "_config", "_transient"):
        stray = tmp_path / reserved / "batch-0"
        stray.mkdir(parents=True)
        (stray / "test.xml").write_text("<testsuite/>")
    assert DockerBackend().count_run_artifacts("camp", str(tmp_path)) == 1


def test_a_missing_campaign_dir_counts_zero_rather_than_raising(tmp_path):
    """The poller probes before the first batch has created anything."""
    missing = tmp_path / "not-yet"
    assert DockerBackend().count_run_artifacts("camp", str(missing)) == 0


def test_the_base_backend_still_allows_a_backend_that_cannot_count():
    """``None`` stays a legal answer for a third-party backend — the controller logs
    the consequence rather than the contract forbidding it."""
    assert ExecutionBackend.count_run_artifacts(
        object(), "camp", "/tmp/whatever") is None


# -- the progress clock ------------------------------------------------------


def test_rewriting_the_same_counters_does_not_reset_the_clock():
    """The poller republishes identical counters every few seconds. Any write-based
    clock would tick forever on a wedged run and report it as healthy."""
    state = ControllerState(phase="running")
    state.update_runs(completed=0, total=10)
    first = state.snapshot().progress_since
    state.update_runs(completed=0, total=10)
    assert state.snapshot().progress_since == first


def test_a_completed_run_advances_the_clock():
    state = ControllerState(phase="running")
    state.update_runs(completed=0, total=10)
    first = state.snapshot().progress_since
    state.update_runs(completed=1, total=10)
    assert state.snapshot().progress_since > first


def test_a_search_advances_on_its_budget_not_its_per_batch_runs():
    """A search's overall progress is its stopping criteria, so that is what counts
    as an advance — mirroring how ``progress`` itself is derived."""
    state = ControllerState(phase="running", mode="search")
    state.update(budget=[{"label": "batches", "current": 1, "limit": 10}])
    first = state.snapshot().progress_since
    state.update(budget=[{"label": "batches", "current": 1, "limit": 10}])
    assert state.snapshot().progress_since == first
    state.update(budget=[{"label": "batches", "current": 2, "limit": 10}])
    assert state.snapshot().progress_since > first


def test_entering_a_new_phase_restarts_the_progress_clock():
    """Reaching a phase is forward movement. Without this a campaign that spent ten
    minutes in `variation` would enter `running` already carrying a ten-minute-old
    clock and be called stalled before its first run could finish."""
    state = ControllerState(phase="variation")
    first = state.snapshot().progress_since
    state.set_phase("running")
    assert state.snapshot().progress_since > first


def test_resetting_the_same_phase_does_not_restart_the_clock():
    state = ControllerState(phase="running")
    state.set_phase("running")
    first = state.snapshot().progress_since
    state.set_phase("running")
    assert state.snapshot().progress_since == first


# -- what the controller publishes -------------------------------------------


def test_the_controller_publishes_a_progress_deadline_scaled_by_packing(tmp_path):
    """Packed runs can publish results in one burst per job, so the unpacked per-run
    figure would accuse a healthy packed campaign of stalling."""
    from robovast.common.store import STORE_FILENAME, CampaignStore
    from robovast.execution.backends import RunOptions
    from robovast.execution.controller import CampaignController

    def _controller(execution):
        return CampaignController(
            campaign_id="camp", results_dir=str(tmp_path), runs=1,
            backend=DockerBackend(), options=RunOptions(),
            store=CampaignStore(tmp_path / "camp" / STORE_FILENAME),
            campaign_config_dump={"execution": execution}, vast_dir=str(tmp_path))

    assert _controller({"timeout": 120})._progress_deadline() == 120
    # The declared budget is the JOB's, so packing does not stretch it: 120s covers the
    # whole job whether it holds one run or four.
    assert _controller({"timeout": 120, "runs_per_job": 4})._progress_deadline() == 120
    # No declared timeout: publish nothing rather than the enforcement backstop, so
    # readers return "cannot judge" instead of a false clean bill of health.
    assert _controller({})._progress_deadline() is None


def test_the_job_budget_has_exactly_one_definition():
    """Enforcement and reporting read the same declared value; only the fallback
    differs. A second copy of the value would eventually drift, and then a Job could be
    force-killed by Kubernetes while the status still called the run healthy."""
    from robovast.execution.cluster_execution import kubernetes_backend

    # The backend's own former constant is gone, so there is nothing to drift from.
    assert not hasattr(kubernetes_backend.BatchJobRunner,
                       "DEFAULT_RUN_DEADLINE_SECONDS")
    assert kubernetes_backend.job_deadline_seconds is job_deadline_seconds
    assert job_deadline_seconds({"timeout": 45}) == declared_job_seconds({"timeout": 45}) == 45


def test_a_declared_budget_is_the_jobs_and_is_not_scaled():
    """v3 semantics. Both lanes enforce at job granularity -- a Job's
    activeDeadlineSeconds, a compose step -- so the declared number is used as written."""
    packed = {"timeout": 600, "runs_per_job": 100}
    assert declared_job_seconds(packed) == job_deadline_seconds(packed) == 600


def test_the_backstop_is_per_run_and_therefore_scales():
    """Undeclared, the fallback is an hour PER RUN, which has to grow with packing.

    Asymmetric with a declaration on purpose: the backstop is a number chosen in ignorance
    of the campaign, so a job of 100 runs must not be killed after the first few. A
    declaration is a statement about the job and is taken at face value.
    """
    assert job_deadline_seconds({}) == DEFAULT_RUN_DEADLINE_SECONDS
    assert job_deadline_seconds({"timeout": None}) == DEFAULT_RUN_DEADLINE_SECONDS
    assert job_deadline_seconds({"runs_per_job": 100}) == DEFAULT_RUN_DEADLINE_SECONDS * 100
    assert declared_job_seconds({}) is None
    assert declared_job_seconds({"timeout": None}) is None


def test_the_per_run_share_is_derived_for_reporting_only():
    """`stalled` is a verdict about a run, so reporting still needs a per-run figure --
    now derived as the job's budget divided by what is packed into it."""
    assert declared_per_run_seconds({"timeout": 600, "runs_per_job": 100}) == 6
    assert declared_per_run_seconds({"timeout": 300}) == 300
    assert declared_per_run_seconds({}) is None
