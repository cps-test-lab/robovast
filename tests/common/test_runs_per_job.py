"""``execution.runs_per_job``: a configuration's runs in one job, stepped shape only."""
import pytest
from pydantic import ValidationError

from robovast.common.config import (DEFAULT_RUN_DEADLINE_SECONDS, ExecutionConfig,
                                    declared_job_seconds, job_deadline_seconds)


def _execution(**kwargs):
    return ExecutionConfig(containers={"scenario": {"image": "x:1"}}, runs=1, **kwargs)


def test_a_job_is_one_run_unless_asked():
    assert _execution().runs_per_job == 1
    assert _execution(mode="base", runs_per_job=20).runs_per_job == 20


def test_several_runs_per_job_are_refused_in_the_ros_shape():
    """The simulator is another container there, started once and told nothing per run."""
    with pytest.raises(ValidationError, match="runs_per_job is 4.*stepped shape.*mode 'ros2'"):
        _execution(mode="ros2", runs_per_job=4)


def test_less_than_one_run_per_job_is_refused():
    with pytest.raises(ValidationError, match="at least 1"):
        _execution(mode="base", runs_per_job=0)


def test_the_deadline_is_the_runs_budget_times_the_jobs_runs():
    """``timeout`` stays the budget of one run; the Job holding several is bounded by all."""
    assert declared_job_seconds({"timeout": 30}) == 30
    assert job_deadline_seconds({"timeout": 30}) == 30
    assert job_deadline_seconds({"timeout": 30}, runs=4) == 120
    assert job_deadline_seconds({}, runs=3) == 3 * DEFAULT_RUN_DEADLINE_SECONDS
