"""RUN_OUTPUT_DIR names this run's own result directory, for a process the scenario only launched."""
from robovast.execution.cluster_execution.kubernetes_backend import (_run_output_dir_env,
                                                                   run_of_output_dir)
from robovast.execution.jobs import Job


def test_named_for_the_jobs_run():
    env = dict(_run_output_dir_env(Job(config={"name": "goal-1"}, runs=[0], index=0)))
    assert env == {"RUN_OUTPUT_DIR": "/out/goal-1/0"}


def test_read_back_as_the_run_it_names():
    env = dict(_run_output_dir_env(Job(config={"name": "goal-1"}, runs=[12], index=4)))
    assert run_of_output_dir(env["RUN_OUTPUT_DIR"]) == ("goal-1", 12)


def test_a_job_of_several_runs_names_none():
    """One variable cannot name several directories, so it is left out rather than made wrong;
    the in-process simulator of such a job is told each run's directory on reset()."""
    assert _run_output_dir_env(Job(config={"name": "goal-1"}, runs=[0, 1, 2], index=0)) == ()


def test_a_value_of_another_shape_names_no_run():
    for value in ("/out/_jobs/batch-0/job-3", "/out/goal-1", "/elsewhere/goal-1/0", "", None):
        assert run_of_output_dir(value) is None
