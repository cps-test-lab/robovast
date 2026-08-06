"""RUN_OUTPUT_DIR names this run's own result directory, for a process the scenario only launched."""
from robovast.execution.cluster_execution.kubernetes_backend import _run_output_dir_env
from robovast.execution.packer import JobSpec, WorkItem


def _item(name, run):
    return WorkItem(config={"name": name}, run_number=run)


def test_named_for_a_single_run_job():
    env = dict(_run_output_dir_env(JobSpec(items=[_item("goal-1", 0)], index=0)))
    assert env == {"RUN_OUTPUT_DIR": "/out/goal-1/0"}


def test_omitted_for_a_packed_job():
    """Several work items run sequentially in one job, so one variable cannot serve them all --
    omitted rather than made wrong, leaving a consumer to fall back to the per-job OUTPUT_DIR."""
    job = JobSpec(items=[_item("goal-1", 0), _item("goal-1", 1)], index=0)
    assert _run_output_dir_env(job) == ()
