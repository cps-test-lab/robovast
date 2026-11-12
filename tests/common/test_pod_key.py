"""The pod key: what a configuration's pod is made of, and so who may share a job with it."""
from robovast.common.execution import (STAGES_OWN_FILE, build_job_links,
                                       build_job_parameter_documents, planned_job_count,
                                       planned_jobs, planned_pod_statements, pod_key, plan_keys,
                                       run_reset_arguments)
from robovast.common.simulators import sim_job_overlay

#: A stepped roqsim execution, as composition leaves it, packing three runs per job.
_EXECUTION = {
    "mode": "base",
    "containers": {"scenario": {"image": "img:both"},
                   "simulation": {"backend": "roqsim", "config": "w.yaml"}},
    "runs": 2, "runs_per_job": 3,
}
#: What composition adds to a roqsim configuration: the streams its recording leaves out.
_COMPOSED = {"recording": {"ros2": {"left_out": []}}}


def _config(name, world="worlds/a.yaml", files=(), **more):
    config = {"name": name, "config": {"speed": 1.0}, "sim": {"config": world}, **_COMPOSED, **more}
    if files:
        config["_config_files"] = [(rel, f"/gen/{name}/{rel}") for rel in files]
    return config


def test_configurations_differing_only_in_parameters_share_a_key():
    a = _config("a")
    b = _config("b")
    b["config"] = {"speed": 2.0}
    assert pod_key(_EXECUTION, a) == pod_key(_EXECUTION, b)
    assert pod_key(_EXECUTION, a).own_reason is None


def test_a_different_world_is_the_same_pod_in_the_stepped_shape():
    """The world is told to the simulator per run, through the document's ``_reset``, so it is
    the run's and not the pod's; the same for the overrides document."""
    with_overrides = _config("b", world="worlds/b.yaml")
    with_overrides["sim"]["overrides"] = {"components": {"floor": {"reflectance": 0.3}}}
    assert pod_key(_EXECUTION, _config("a")) == pod_key(_EXECUTION, with_overrides)


def test_the_world_stays_the_pods_where_the_backend_states_nothing_per_run():
    """In the ROS shape the simulator is another container started with one world."""
    ros = {**_EXECUTION, "mode": "ros2", "runs_per_job": 1}
    assert pod_key(ros, _config("a")).key != pod_key(ros, _config("b", world="worlds/b.yaml")).key
    assert run_reset_arguments(ros, _config("a")) is None


def test_the_backend_states_the_world_and_its_overrides_per_run():
    overlay = sim_job_overlay(_EXECUTION, {"config": "worlds/b.yaml",
                                           "overrides": {"components": {"floor": {"k": 1}}}})
    assert overlay["reset"].arguments == {"world": "/config/worlds/b.yaml",
                                          "world_overrides": {"components": {"floor": {"k": 1}}}}
    assert set(overlay["reset"].supersedes) == {"ROQSIM_WORLD", "ROQSIM_WORLD_OVERRIDES"}
    assert run_reset_arguments(_EXECUTION, _config("a")) == {"world": "/config/worlds/a.yaml",
                                                            "world_overrides": {}}


def test_a_different_recorder_environment_is_a_different_pod():
    leaves_out = _config("b", recording={"ros2": {"left_out": [{"topic": "/scan", "type": "t"}]}})
    assert pod_key(_EXECUTION, _config("a")).key != pod_key(_EXECUTION, leaves_out).key


def test_a_configuration_staging_a_file_shares_with_nobody():
    """Its copies are at ``/config/<deploy path>``, which another configuration's run would read."""
    c = _config("c", files=("files/nav2_params.yaml",))
    d = _config("d", files=("files/nav2_params.yaml",))
    assert pod_key(_EXECUTION, c).key != pod_key(_EXECUTION, d).key
    assert pod_key(_EXECUTION, c).key != pod_key(_EXECUTION, _config("a")).key
    assert pod_key(_EXECUTION, c).own_reason == STAGES_OWN_FILE


def test_the_default_asks_for_no_key():
    """At one run per job nothing shares, so no backend is resolved to say what a pod is."""
    assert plan_keys({"runs_per_job": 1}, [{"name": "a"}]) is None
    assert plan_keys({}, [{"name": "a"}]) is None
    assert planned_job_count({"runs": 3}, [{"name": "a"}, {"name": "b"}], 3) == 6


def test_the_plan_packs_equal_pods_and_states_who_shares():
    configs = [_config("a"), _config("b"), _config("c", files=("files/x.yaml",)),
               _config("d", recording={"ros2": {"left_out": [{"topic": "/scan", "type": "t"}]}})]
    jobs = planned_jobs(_EXECUTION, configs, 2)
    assert [j.run_keys for j in jobs] == [
        ("a/0", "a/1", "b/0"), ("b/1",), ("c/0", "c/1"), ("d/0", "d/1")]
    assert planned_job_count(_EXECUTION, configs, 2) == 4
    assert planned_pod_statements(_EXECUTION, configs, 2) == [
        "a, b share a pod (2 job(s) of up to 3 runs)",
        f"c keeps its own pod: {STAGES_OWN_FILE}",
        "d keeps its own pod: its simulator overlay is its own"]
    assert planned_pod_statements({"runs_per_job": 1}, configs, 2) == []


def test_the_documents_of_a_mixed_job_are_each_items_own():
    a, b = _config("a"), _config("b")
    b["config"] = {"speed": 2.0}
    job = planned_jobs(_EXECUTION, [a, b], 2)[0]
    documents = build_job_parameter_documents(job, "trial")
    assert [d["trial"]["_output_dir"] for d in documents] == ["a/0", "a/1", "b/0"]
    assert [d["trial"]["speed"] for d in documents] == [1.0, 1.0, 2.0]


def _reset_of(execution):
    return lambda config: run_reset_arguments(execution, config)


def test_the_documents_of_a_mixed_world_job_carry_each_runs_world():
    """Each run's document tells the simulator its world; the first's says what the process was
    started with, so the first configuration's runs rebuild nothing."""
    a, b = _config("a"), _config("b", world="worlds/b.yaml")
    b["sim"]["overrides"] = {"components": {"floor": {"k": 1}}}
    job = planned_jobs(_EXECUTION, [a, b], 2)[0]
    documents = build_job_parameter_documents(job, "trial", run_reset=_reset_of(_EXECUTION))
    assert [d["trial"]["_reset"] for d in documents] == [
        {"world": "/config/worlds/a.yaml", "world_overrides": {}},
        {"world": "/config/worlds/a.yaml", "world_overrides": {}},
        {"world": "/config/worlds/b.yaml", "world_overrides": {"components": {"floor": {"k": 1}}}}]


def test_a_job_of_one_configuration_carries_no_reset():
    """So an unpacked or same-configuration campaign runs unchanged on an older runner."""
    one = {**_EXECUTION, "runs_per_job": 1}
    for execution in (one, _EXECUTION):
        for job in planned_jobs(execution, [_config("a")], 2):
            documents = build_job_parameter_documents(job, "trial", run_reset=_reset_of(execution))
            assert all("_reset" not in d["trial"] for d in documents)


def test_a_job_of_one_world_carries_no_reset():
    """Configurations differing only in their parameters share a job without ``_reset``, so a
    campaign's own adapter, whose ``reset()`` need not know the key, runs them as before."""
    a, b = _config("a"), _config("b")
    b["config"] = {"speed": 2.0}
    job = planned_jobs(_EXECUTION, [a, b], 2)[0]
    assert job.config_names == ("a", "b")
    documents = build_job_parameter_documents(job, "trial", run_reset=_reset_of(_EXECUTION))
    assert all("_reset" not in d["trial"] for d in documents)


def test_a_campaigns_own_adapter_keeps_the_world_the_pods():
    """Only roqsim's own adapter is known to take the world at ``reset()``: under another one
    the world stays the process's, so two worlds never share a job."""
    own = {"adapter": "my_experiment.adapter:Sim"}
    a = _config("a")
    b = _config("b", world="worlds/b.yaml")
    a["sim"].update(own)
    b["sim"].update(own)
    assert run_reset_arguments(_EXECUTION, a) is None
    assert pod_key(_EXECUTION, a).key != pod_key(_EXECUTION, b).key
    assert [j.config_names for j in planned_jobs(_EXECUTION, [a, b], 2)] == [("a",), ("b",)]


def test_the_links_of_a_mixed_job_point_at_its_directory():
    job = planned_jobs(_EXECUTION, [_config("a"), _config("b")], 2)[0]
    assert build_job_links([job], "batch-0") == {
        "a/0/job": "../../_jobs/batch-0/job-0", "a/1/job": "../../_jobs/batch-0/job-0",
        "b/0/job": "../../_jobs/batch-0/job-0"}
