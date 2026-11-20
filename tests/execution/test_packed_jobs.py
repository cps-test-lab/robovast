# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""A job holds runs of several configurations when their pod would be the same.

The manifest of such a job is read off its first item, which is only right because every item
resolves to the same pod -- asserted where the manifest is built. A configuration that stages a
file of its own keeps a job of its own, so ``/config`` never shows one cell another's file.
"""
import types

import pytest
from kubernetes import client

from robovast.common.execution import build_job_parameter_documents
from robovast.execution.backends import CampaignConfigError
from robovast.execution.cluster_execution import kubernetes_backend
from robovast.execution.cluster_execution.kubernetes_backend import BatchJobRunner
from robovast.execution.jobs import Job, RunItem

_COMPOSED = {"recording": {"ros2": {"left_out": []}}}
_STEPPED = {"mode": "base",
            "containers": {"scenario": {"image": "img:both"},
                           "simulation": {"backend": "roqsim", "config": "w.yaml"}}}


def _config(name, world="worlds/a.yaml", files=()):
    config = {"name": name, "config": {"speed": 1.0}, "sim": {"config": world}, **_COMPOSED}
    if files:
        config["_config_files"] = [(rel, f"/gen/{name}/{rel}") for rel in files]
    return config


class _FakeClusterConfig:
    def get_registry_config(self):
        return types.SimpleNamespace(pull_secret_name="")


def _runner(monkeypatch, configs, runs=2, runs_per_job=4):
    monkeypatch.setattr(kubernetes_backend, "resolve_resources",
                        lambda res, ctx: dict(res) if isinstance(res, dict) else {})

    def _fake_discover(self):
        self._gpu_capacity = 0
        self._gpu_runtime_class = None

    monkeypatch.setattr(BatchJobRunner, "_discover_gpu_support", _fake_discover)

    def _no_such_secret(self, *args, **kwargs):
        raise client.exceptions.ApiException(status=404, reason="Not Found")

    monkeypatch.setattr(kubernetes_backend.client.CoreV1Api, "read_namespaced_secret",
                        _no_such_secret)
    monkeypatch.setattr(BatchJobRunner, "_resolve_digest",
                        lambda self, ref: ref.rsplit(":", 1)[0] + "@sha256:" + "0" * 64)
    return BatchJobRunner.for_batch(
        campaign_data={"configs": configs,
                       "execution": {**_STEPPED, "runs_per_job": runs_per_job, "timeout": 30},
                       "scenario_file": "scenario.osc", "vast": "/tmp/x.vast"},
        campaign_id="camp-2026-07-17-120000", batch_tag="batch-0", runs=runs,
        cluster_config=_FakeClusterConfig(), namespace="ns", image="img:test",
        kube_context=None)


def _main_env(manifest):
    main = manifest["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e.get("value") for e in main["env"]}


def _init_command(manifest):
    spec = manifest["spec"]["template"]["spec"]
    init = next(c for c in spec["initContainers"] if c["name"] == "fetch-inputs")
    return " ".join(init["command"] + init.get("args", []))


def test_configurations_of_one_pod_share_a_job(monkeypatch):
    r = _runner(monkeypatch, [_config("a"), _config("b")])
    jobs = r._build_jobs()
    assert [j.run_keys for j in jobs] == [("a/0", "a/1", "b/0", "b/1")]
    manifest = r.create_job_manifest(jobs[0], total_jobs=1)
    env = _main_env(manifest)
    assert "RUN_OUTPUT_DIR" not in env, "a job of several items has no one run directory"
    assert env["ROQSIM_WORLD"] == "/config/worlds/a.yaml"
    assert manifest["spec"]["activeDeadlineSeconds"] == 4 * 30
    assert "config_file=" not in _init_command(manifest)
    documents = build_job_parameter_documents(jobs[0], "trial")
    assert [d["trial"]["_output_dir"] for d in documents] == ["a/0", "a/1", "b/0", "b/1"]


def test_a_configuration_staging_a_file_keeps_a_job_of_its_own(monkeypatch):
    r = _runner(monkeypatch, [_config("a"), _config("c", files=("files/nav2_params.yaml",)),
                              _config("b")])
    jobs = r._build_jobs()
    assert [j.run_keys for j in jobs] == [("a/0", "a/1", "b/0", "b/1"), ("c/0", "c/1")]
    own = r.create_job_manifest(jobs[1], total_jobs=2)
    assert "config_file=c%3Afiles%2Fnav2_params.yaml" in _init_command(own)
    shared = r.create_job_manifest(jobs[0], total_jobs=2)
    assert "config_file=" not in _init_command(shared)


def _leaves_out(config):
    return {**config, "recording": {"ros2": {"left_out": [{"topic": "/scan", "type": "t"}]}}}


def test_configurations_of_different_pods_never_share_a_job(monkeypatch):
    r = _runner(monkeypatch, [_config("a"), _leaves_out(_config("b"))])
    jobs = r._build_jobs()
    assert [j.run_keys for j in jobs] == [("a/0", "a/1"), ("b/0", "b/1")]
    left_out = [_main_env(r.create_job_manifest(j, total_jobs=2))["RECORD_LEFT_OUT"] for j in jobs]
    assert left_out == ["", "/scan"]


def test_a_job_whose_items_would_not_share_a_pod_is_refused(monkeypatch):
    """The manifest is read off the first item; a job that packed another pod's run would run
    it in a pod made for the first, so it is refused rather than rendered."""
    r = _runner(monkeypatch, [_config("a"), _leaves_out(_config("b"))])
    job = Job(items=[RunItem(_config("a"), 0), RunItem(_leaves_out(_config("b")), 0)])
    with pytest.raises(CampaignConfigError, match="packs runs of b with a"):
        r.create_job_manifest(job, total_jobs=1)


def test_runs_of_different_worlds_share_a_job_and_each_document_names_its_world(monkeypatch, tmp_path):
    """The stepped simulator is told each run's world by its document, so two configurations
    differing in their world get one pod; the process starts with the first item's world, and
    the job-level document and environment say so."""
    import yaml

    a, b = _config("a"), _config("b", world="worlds/b.yaml")
    b["sim"]["overrides"] = {"components": {"floor": {"k": 1}}}
    r = _runner(monkeypatch, [a, b])
    (jobs,) = [r._build_jobs()]
    assert [j.run_keys for j in jobs] == [("a/0", "a/1", "b/0", "b/1")]
    env = _main_env(r.create_job_manifest(jobs[0], total_jobs=1))
    assert env["ROQSIM_WORLD"] == "/config/worlds/a.yaml"
    assert "ROQSIM_WORLD_OVERRIDES" not in env, "the first item has no overrides document"
    scenario = tmp_path / "scenario.osc"
    scenario.write_text("scenario trial:\n    do serial:\n        wait elapsed(1s)\n")
    r.campaign_data["scenario_file"] = str(scenario)
    r._write_job_param_files(str(tmp_path / "out"))
    (params,) = (tmp_path / "out" / "_transient").glob("*.params.yaml")
    documents = list(yaml.safe_load_all(params.read_text()))
    assert [d["trial"]["_reset"]["world"] for d in documents] == [
        "/config/worlds/a.yaml", "/config/worlds/a.yaml", "/config/worlds/b.yaml", "/config/worlds/b.yaml"]
    assert documents[3]["trial"]["_reset"]["world_overrides"] == {"components": {"floor": {"k": 1}}}
    assert documents[0]["trial"]["_reset"]["world_overrides"] == {}
    assert not list((tmp_path / "out" / "_transient").glob("*.sim.yaml")), "the first item has no document"


def test_a_mixed_job_is_done_only_with_every_items_verdict(tmp_path):
    job = Job(items=[RunItem({"name": "a"}, 0), RunItem({"name": "b"}, 0)], index=0)
    (tmp_path / "a" / "0").mkdir(parents=True)
    (tmp_path / "a" / "0" / "test.xml").write_text("<testsuite/>")
    assert BatchJobRunner._jobs_already_done([job], str(tmp_path)) == set()
    (tmp_path / "b" / "0").mkdir(parents=True)
    (tmp_path / "b" / "0" / "test.xml").write_text("<testsuite/>")
    assert BatchJobRunner._jobs_already_done([job], str(tmp_path)) == {0}


def test_the_default_is_one_job_per_run(monkeypatch):
    r = _runner(monkeypatch, [_config("a"), _config("b")], runs=2, runs_per_job=1)
    assert [j.run_keys for j in r._build_jobs()] == [("a/0",), ("a/1",), ("b/0",), ("b/1",)]
