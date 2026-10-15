"""A campaign's jobs: one per run by default, a configuration's runs packed on request."""
import pytest

from robovast.execution.jobs import Job, build_jobs, declared_runs_per_job


def test_one_job_per_run_by_config_then_by_run():
    jobs = build_jobs([{"name": "b"}, {"name": "a"}], 2)
    assert [(j.config_name, j.runs) for j in jobs] == [
        ("b", [0]), ("b", [1]), ("a", [0]), ("a", [1])]
    assert [j.index for j in jobs] == [0, 1, 2, 3]


def test_the_jobs_are_the_same_on_every_call():
    """The parameter files and the manifests are built by separate calls, for the same jobs."""
    configs = [{"name": "b"}, {"name": "a"}]
    assert build_jobs(configs, 2) == build_jobs(configs, 2)
    assert build_jobs(configs, 5, 2) == build_jobs(configs, 5, 2)


def test_a_configurations_runs_are_packed_and_never_another_configurations():
    """``runs_per_job`` is an upper bound within one configuration: a job holds one world and
    one configuration's files, so what it holds several of is that configuration's runs."""
    jobs = build_jobs([{"name": "a"}, {"name": "b"}], 5, runs_per_job=2)
    assert [(j.config_name, j.runs) for j in jobs] == [
        ("a", [0, 1]), ("a", [2, 3]), ("a", [4]), ("b", [0, 1]), ("b", [2, 3]), ("b", [4])]
    assert [j.index for j in jobs] == list(range(6))
    assert jobs[0].run_keys == ("a/0", "a/1") and jobs[0].first_run == 0


def test_packing_wider_than_the_runs_is_one_job_per_configuration():
    jobs = build_jobs([{"name": "a"}], 3, runs_per_job=50)
    assert [(j.config_name, j.runs) for j in jobs] == [("a", [0, 1, 2])]


def test_packing_below_one_is_refused():
    with pytest.raises(ValueError, match="at least 1"):
        build_jobs([{"name": "a"}], 3, runs_per_job=0)


def test_the_declared_packing_defaults_to_one():
    assert declared_runs_per_job({}) == 1
    assert declared_runs_per_job({"runs_per_job": None}) == 1
    assert declared_runs_per_job({"runs_per_job": 4}) == 4


def test_the_reported_count_is_the_plans():
    """What validation and the preview report is what the backend creates."""
    from robovast.execution.jobs import job_count
    configs = [{"name": "a"}, {"name": "b"}]
    for runs, packing in ((1, 1), (5, 2), (3, 50), (4, 4)):
        assert job_count(len(configs), runs, packing) == len(build_jobs(configs, runs, packing))
    assert job_count(2, 5, 2) == 6 and job_count(3, 4, 1) == 12


def test_a_job_knows_its_runs_keys():
    assert Job(config={"name": "c"}, runs=[3, 4], index=1).run_keys == ("c/3", "c/4")
