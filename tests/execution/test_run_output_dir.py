"""RUN_OUTPUT_DIR names this run's own result directory, for a process the scenario only launched."""
from robovast.execution.cluster_execution.kubernetes_backend import _run_output_dir_env
from robovast.execution.jobs import Job


def test_named_for_the_jobs_run():
    env = dict(_run_output_dir_env(Job(config={"name": "goal-1"}, run_number=0, index=0)))
    assert env == {"RUN_OUTPUT_DIR": "/out/goal-1/0"}
