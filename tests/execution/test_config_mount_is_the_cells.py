# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""``/config`` is the view belonging to the cell that is running.

A configuration's own copy of a file is staged where the campaign's copy would have been,
so a path a scenario writes relative to itself -- ``get_scenario_file_directory() +
'/files/nav2_params.yaml'`` -- names the file belonging to the cell. These pin that, and
the two things that make it unambiguous: one file per path, and one
configuration per job.
"""

from kubernetes import client

from robovast.common.execution import build_job_parameter_documents
from robovast.execution.cluster_execution import kubernetes_backend
from robovast.execution.cluster_execution.kubernetes_backend import BatchJobRunner
from robovast.execution.jobs import Job

DEPLOY_REL = "files/nav2_params.yaml"


def _job(name, run=0, files=(DEPLOY_REL,)):
    return Job(
        config={"name": name,
                "config": {"params_file": DEPLOY_REL},
                "_config_files": [(rel, f"/gen/{name}/{rel}") for rel in files]},
        run_number=run)


# -- the job parameter documents -------------------------------------------------------


def _plan(with_sut=False):
    from robovast.common.containers import plan_containers
    containers = {"scenario": {"image": "img:test"}}
    if with_sut:
        containers["sut"] = {"image": "sut:test"}
    return plan_containers({"containers": containers})


def test_a_file_valued_parameter_is_carried_as_the_campaign_wrote_it():
    """No rewrite: the path already names the cell's file, because that is what is at it."""
    document = build_job_parameter_documents(_job("cfg-a", 0), "nav")[0]["nav"]
    assert document["params_file"] == DEPLOY_REL
    assert document["_output_dir"] == "cfg-a/0"


# -- the init container ----------------------------------------------------------------
#
# The init container names the cell's inputs on the request it fetches `/config` with; the
# data plane emits each one after the campaign's copy, so the cell's file lands on it
# (`tests/service/test_data_app.py`). What the init container decides is therefore WHICH
# paths are asked for.


class _FakeClusterConfig:
    def get_registry_config(self):
        import types
        return types.SimpleNamespace(pull_secret_name="")


def _init_command(monkeypatch, configs, runs=1, also_reads=()):
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
    monkeypatch.setattr(BatchJobRunner, "_resolve_digest", lambda self, ref: "")
    runner = BatchJobRunner.for_batch(
        campaign_data={"configs": configs,
                       "execution": {},
                       "scenario_file": "scenario.osc", "vast": "/tmp/x.vast"},
        campaign_id="camp-2026-07-17-120000", batch_tag="batch-0", runs=runs,
        cluster_config=_FakeClusterConfig(), namespace="ns", image="img:test",
        kube_context=None)
    job = runner._build_jobs()[0]
    manifest = runner.create_job_manifest(job, total_jobs=1, also_reads=also_reads)
    spec = manifest["spec"]["template"]["spec"]
    init = next(c for c in spec["initContainers"] if c["name"] == "fetch-inputs")
    return " ".join(init["command"] + init.get("args", []))


_CLUSTER_CONFIGS = [{"name": "cfg-a",
                     "_config_files": [(DEPLOY_REL, f"/gen/cfg-a/{DEPLOY_REL}")]}]

#: How ``config_file=<config>:<rel>`` reads once it is URL-quoted onto the query.
_ASKED_FOR = "config_file=cfg-a%3Afiles%2Fnav2_params.yaml"


def test_the_init_container_asks_for_the_cells_file(monkeypatch):
    command = _init_command(monkeypatch, _CLUSTER_CONFIGS)
    assert _ASKED_FOR in command, command


def test_it_fetches_the_whole_view_in_one_stream(monkeypatch):
    """One request carries the campaign's copies and the cell's, in that order, so the
    order the cell's file lands on the campaign's is the stream's rather than a step the
    pod could get wrong."""
    command = _init_command(monkeypatch, _CLUSTER_CONFIGS)
    assert command.count("curl -sSf") == 1, command
    assert "/campaigns/camp-2026-07-17-120000/inputs" in command, command
    assert "tar -x -C /config" in command, command


def test_the_init_container_asks_for_its_own_jobs_documents(monkeypatch):
    """The data plane sends a pod only the job documents it names, so a job names its tag
    and nothing else: every other job's parameters would grow the download with the
    campaign."""
    command = _init_command(monkeypatch, _CLUSTER_CONFIGS)
    assert "job=batch-0-job-0" in command, command
    assert command.count("job=") == 1, command
    assert "SCENARIO_PARAMETER_FILE" not in command


def test_a_manifest_a_probe_derives_from_also_asks_for_the_probes_documents(monkeypatch):
    command = _init_command(monkeypatch, _CLUSTER_CONFIGS, also_reads=["probe-n1"])
    assert "job=batch-0-job-0" in command and "job=probe-n1" in command, command


def test_it_never_asks_for_a_cells_records(monkeypatch):
    """`<config>/_config/` also holds `scenario.config`, which is the entrypoint's default
    parameter file -- which is why the deploy paths are named rather than mirrored."""
    command = _init_command(monkeypatch, _CLUSTER_CONFIGS)
    assert "scenario.config" not in command, command
    assert "sut.config" not in command, command
    assert "cfg-a" not in command.replace(_ASKED_FOR, ""), command


def test_a_campaign_staging_nothing_per_cell_names_no_file(monkeypatch):
    command = _init_command(monkeypatch, [{"name": "cfg-a"}])
    assert "config_file=" not in command, command
