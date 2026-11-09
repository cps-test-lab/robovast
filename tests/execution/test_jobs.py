"""A campaign's jobs: one per run by default, runs of equal-pod configurations packed on request."""
import pytest

from robovast.execution.jobs import Job, RunItem, build_jobs, declared_runs_per_job, job_count, pod_statements


def _layout(jobs):
    return [[(item.config_name, item.run) for item in j.items] for j in jobs]


def test_one_job_per_run_by_config_then_by_run():
    jobs = build_jobs([{"name": "b"}, {"name": "a"}], 2)
    assert _layout(jobs) == [[("b", 0)], [("b", 1)], [("a", 0)], [("a", 1)]]
    assert [j.index for j in jobs] == [0, 1, 2, 3]


def test_one_run_per_job_asks_for_no_key():
    """Nothing shares at the default, whatever the keys would have said."""
    assert _layout(build_jobs([{"name": "a"}, {"name": "b"}], 2)) == \
        _layout(build_jobs([{"name": "a"}, {"name": "b"}], 2, 1, keys={"a": 1, "b": 1}))


def test_the_jobs_are_the_same_on_every_call():
    """The parameter files and the manifests are built by separate calls, for the same jobs."""
    configs = [{"name": "b"}, {"name": "a"}]
    keys = {"a": "k", "b": "k"}
    assert build_jobs(configs, 2) == build_jobs(configs, 2)
    assert build_jobs(configs, 5, 2, keys) == build_jobs(configs, 5, 2, keys)


def test_configurations_of_distinct_keys_never_share_a_job():
    jobs = build_jobs([{"name": "a"}, {"name": "b"}], 5, runs_per_job=2, keys={"a": 1, "b": 2})
    assert _layout(jobs) == [
        [("a", 0), ("a", 1)], [("a", 2), ("a", 3)], [("a", 4)],
        [("b", 0), ("b", 1)], [("b", 2), ("b", 3)], [("b", 4)]]
    assert [j.index for j in jobs] == list(range(6))
    assert jobs[0].run_keys == ("a/0", "a/1") and jobs[0].first_run == 0


def test_configurations_of_equal_key_pack_config_major_in_first_seen_order():
    """A group's runs are laid out configuration after configuration and cut into jobs, so a
    cut falls inside the group and a job changes configuration as rarely as the cut allows."""
    configs = [{"name": "c"}, {"name": "a"}, {"name": "b"}]
    jobs = build_jobs(configs, 2, runs_per_job=3, keys={"a": "k", "b": "k", "c": "k"})
    assert _layout(jobs) == [[("c", 0), ("c", 1), ("a", 0)], [("a", 1), ("b", 0), ("b", 1)]]
    assert jobs[0].config_names == ("c", "a") and jobs[0].run_keys == ("c/0", "c/1", "a/0")


def test_a_cut_never_crosses_a_group():
    configs = [{"name": "a"}, {"name": "b"}, {"name": "c"}]
    jobs = build_jobs(configs, 1, runs_per_job=4, keys={"a": "k", "b": "own", "c": "k"})
    assert _layout(jobs) == [[("a", 0), ("c", 0)], [("b", 0)]]


def test_packing_wider_than_the_runs_is_one_job_per_group():
    jobs = build_jobs([{"name": "a"}], 3, runs_per_job=50, keys={"a": 1})
    assert _layout(jobs) == [[("a", 0), ("a", 1), ("a", 2)]]


def test_packing_below_one_is_refused():
    with pytest.raises(ValueError, match="at least 1"):
        build_jobs([{"name": "a"}], 3, runs_per_job=0)


def test_packing_without_keys_is_refused():
    with pytest.raises(ValueError, match="pod key"):
        build_jobs([{"name": "a"}], 3, runs_per_job=2)
    with pytest.raises(ValueError, match="no pod key for configuration 'b'"):
        build_jobs([{"name": "a"}, {"name": "b"}], 3, runs_per_job=2, keys={"a": 1})


def test_the_declared_packing_defaults_to_one():
    assert declared_runs_per_job({}) == 1
    assert declared_runs_per_job({"runs_per_job": None}) == 1
    assert declared_runs_per_job({"runs_per_job": 4}) == 4


def test_the_reported_count_is_the_plans():
    """What validation and the preview report is what the backend creates."""
    configs = [{"name": "a"}, {"name": "b"}]
    for runs, packing, keys in ((1, 1, None), (5, 2, {"a": 1, "b": 2}), (3, 50, {"a": 1, "b": 1}),
                                (4, 4, {"a": 1, "b": 1})):
        assert job_count(configs, runs, packing, keys) == len(build_jobs(configs, runs, packing, keys))
    assert job_count(configs, 5, 2, {"a": 1, "b": 2}) == 6
    assert job_count(configs, 5, 2, {"a": 1, "b": 1}) == 5
    assert job_count(configs, 4) == 8


def test_a_job_derives_everything_from_its_items():
    a, b = {"name": "a"}, {"name": "b"}
    job = Job(items=[RunItem(a, 3), RunItem(a, 4), RunItem(b, 0)], index=1)
    assert job.run_keys == ("a/3", "a/4", "b/0") and job.runs == [3, 4, 0]
    assert job.first == RunItem(a, 3) and job.first_run == 3
    assert job.configs == [a, b] and job.config_names == ("a", "b")
    assert job.shared_config_name is None


def test_the_configuration_of_a_mixed_job_is_no_one_configuration():
    """A reader asking a mixed job for *the* configuration is asking a question with no
    answer, and is told so rather than handed the first."""
    job = Job(items=[RunItem({"name": "a"}, 0), RunItem({"name": "b"}, 0)], index=2)
    with pytest.raises(ValueError, match="job 2 holds runs of 2 configurations \\(a, b\\)"):
        _ = job.config
    one = Job(items=[RunItem({"name": "a"}, 0), RunItem({"name": "a"}, 1)])
    assert one.config == {"name": "a"} and one.config_name == "a" and one.shared_config_name == "a"


def test_the_statements_name_who_shares_and_why_one_does_not():
    configs = [{"name": "a"}, {"name": "b"}, {"name": "c"}, {"name": "d"}]
    keys = {"a": "k", "b": "k", "c": "own-c", "d": "other"}
    assert pod_statements(configs, 5, 2, keys, own_reasons={"c": "stages a file of its own"}) == [
        "a, b share a pod (5 job(s) of up to 2 runs)",
        "c keeps its own pod: stages a file of its own",
        "d keeps its own pod: its simulator overlay is its own"]


def test_there_is_nothing_to_state_at_one_run_per_job():
    assert pod_statements([{"name": "a"}, {"name": "b"}], 5) == []
