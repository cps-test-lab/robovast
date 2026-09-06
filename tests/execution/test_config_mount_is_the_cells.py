# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""``/config`` is the view belonging to the cell that is running.

A configuration's own copy of a file is staged where the campaign's copy would have been,
so a path a scenario writes relative to itself -- ``get_scenario_file_directory() +
'/files/nav2_params.yaml'`` -- names the file belonging to the cell. These pin that on both
lanes, and pin the two things that make it unambiguous: one file per path, and one
file-owning configuration per job.
"""

from kubernetes import client

from robovast.common.execution import build_job_parameter_documents, scenario_env
from robovast.execution.cluster_execution import in_pod_storage, kubernetes_backend
from robovast.execution.cluster_execution.kubernetes_backend import BatchJobRunner
from robovast.execution.packer import JobSpec, WorkItem

DEPLOY_REL = "files/nav2_params.yaml"


def _item(name, run=0, files=(DEPLOY_REL,)):
    return WorkItem(
        config={"name": name,
                "config": {"params_file": DEPLOY_REL},
                "_config_files": [(rel, f"/gen/{name}/{rel}") for rel in files]},
        run_number=run)


# -- the local lane --------------------------------------------------------------------


def _plan(with_sut=False):
    from robovast.common.containers import plan_containers
    containers = {"scenario": {"image": "img:test"}}
    if with_sut:
        containers["sut"] = {"image": "sut:test"}
    return plan_containers({"containers": containers})


def _compose(tmp_path, job, run_files):
    from robovast.execution.execution_utils.execute_local import _build_packed_compose_yaml
    campaign_data = {"execution": {"containers": {"scenario": {"image": "img:test"}}, "runs": 1},
                     "scenario_file": "scenario.osc"}
    return _build_packed_compose_yaml(
        docker_image="img:test", out_path=str(tmp_path), results_dir_var="${RESULTS}",
        job=job, param_file_rel="p.yaml", run_files=run_files, env_vars={},
        pre_command=None, post_command=None, uid=1000, gid=1000,
        main_cpu=1, main_memory=None, main_gpu=False, plan=_plan(),
        use_gui_block=False, scenario_env_vars=scenario_env(campaign_data))


def _targets(compose_text, container_path):
    """Every mount line whose target is *container_path*."""
    return [line.strip() for line in compose_text.splitlines()
            if f":{container_path}:" in line]


def test_a_cells_file_is_mounted_where_the_campaigns_copy_would_have_been(tmp_path):
    text = _compose(tmp_path, JobSpec(items=[_item("cfg-a")], index=0), run_files=[])
    mounts = _targets(text, f"/config/{DEPLOY_REL}")
    assert len(mounts) == 1, text
    assert f"cfg-a/_config/{DEPLOY_REL}" in mounts[0]


def test_no_per_configuration_directory_is_mounted(tmp_path):
    """The level this replaced. Nothing may reach `/config/<config-name>/` any more, or a
    scenario written against one layout would keep working against the other."""
    text = _compose(tmp_path, JobSpec(items=[_item("cfg-a")], index=0), run_files=[])
    assert "/config/cfg-a/" not in text, text


def test_a_campaign_file_at_the_same_path_is_not_mounted_beside_it(tmp_path):
    """One file per path. Two sources for one target is a compose error, and even if it
    were not, which one the stack opened would decide whether the campaign varied
    anything."""
    text = _compose(tmp_path, JobSpec(items=[_item("cfg-a")], index=0),
                    run_files=[DEPLOY_REL, "files/depot.yaml"])
    assert len(_targets(text, f"/config/{DEPLOY_REL}")) == 1, text
    # ... and a campaign file the cell does NOT own is staged exactly as before, which is
    # what makes this an exclusion of what the configuration owns rather than of run_files.
    assert len(_targets(text, "/config/files/depot.yaml")) == 1, text


def test_a_packed_job_mounts_one_configurations_file_once(tmp_path):
    """`runs_per_job > 1` carries a configuration once per run. Compose refuses a repeated
    mount target even when both sides name the same file, so the paths are what is
    iterated, not the work items."""
    job = JobSpec(items=[_item("cfg-a", 0), _item("cfg-a", 1), _item("cfg-a", 2)], index=0)
    text = _compose(tmp_path, job, run_files=[])
    assert len(_targets(text, f"/config/{DEPLOY_REL}")) == 1, text


def test_a_sidecar_sees_the_same_cell(tmp_path):
    """A remote `ros_launch` resolves the client's scenario directory on the SUT's
    filesystem, so the two containers must agree about what `/config` holds."""
    from robovast.execution.execution_utils.execute_local import _build_packed_compose_yaml
    campaign_data = {"execution": {"containers": {"scenario": {"image": "img:test"}}, "runs": 1},
                     "scenario_file": "scenario.osc"}
    text = _build_packed_compose_yaml(
        docker_image="img:test", out_path=str(tmp_path), results_dir_var="${RESULTS}",
        job=JobSpec(items=[_item("cfg-a")], index=0), param_file_rel="p.yaml",
        run_files=[], env_vars={}, pre_command=None, post_command=None, uid=1000, gid=1000,
        main_cpu=1, main_memory=None, main_gpu=False, plan=_plan(with_sut=True),
        use_gui_block=False, scenario_env_vars=scenario_env(campaign_data))
    assert len(_targets(text, f"/config/{DEPLOY_REL}")) == 2, text


# -- what the scenario is told ---------------------------------------------------------


def test_a_file_valued_parameter_is_carried_as_the_campaign_wrote_it():
    """No rewrite: the path already names the cell's file, because that is what is at it."""
    job = JobSpec(items=[_item("cfg-a", 0)], index=0)
    document = build_job_parameter_documents(job, "nav")[0]["nav"]
    assert document["params_file"] == DEPLOY_REL
    assert document["_output_dir"] == "cfg-a/0"


# -- the cluster lane ------------------------------------------------------------------


class _FakeClusterConfig:
    def get_s3_endpoint(self):
        return "http://s3:9000"

    def get_s3_credentials(self):
        return ("ak", "sk")

    def get_registry_config(self):
        import types
        return types.SimpleNamespace(pull_secret_name="")


def _init_command(monkeypatch, configs, runs=1, runs_per_job=1):
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
    monkeypatch.setattr(in_pod_storage, "campaign_storage_location",
                        lambda cfg, camp: ("bkt", ""))
    runner = BatchJobRunner.for_batch(
        campaign_data={"configs": configs,
                       "execution": {"runs_per_job": runs_per_job},
                       "scenario_file": "scenario.osc", "vast": "/tmp/x.vast"},
        campaign_id="camp-2026-07-17-120000", batch_tag="batch-0", runs=runs,
        cluster_config=_FakeClusterConfig(), namespace="ns", image="img:test",
        kube_context=None)
    job = runner._build_jobs()[0]
    manifest = runner.create_job_manifest(job, total_jobs=1)
    spec = manifest["spec"]["template"]["spec"]
    init = next(c for c in spec["initContainers"] if c["name"] == "s3-init")
    return " ".join(init["command"] + init.get("args", []))


_CLUSTER_CONFIGS = [{"name": "cfg-a",
                     "_config_files": [(DEPLOY_REL, f"/gen/cfg-a/{DEPLOY_REL}")]}]


def test_the_init_container_stages_a_path_once_for_a_packed_job(monkeypatch):
    """The cluster twin of the local dedupe: several runs of one cell are one copy."""
    command = _init_command(monkeypatch, _CLUSTER_CONFIGS, runs=3, runs_per_job=3)
    assert command.count(f"/config/{DEPLOY_REL}") == 1, command


def test_the_init_container_stages_the_cells_file_at_the_config_mount(monkeypatch):
    command = _init_command(monkeypatch, _CLUSTER_CONFIGS)
    assert f"cfg-a/_config/{DEPLOY_REL} /config/{DEPLOY_REL}" in command, command


def test_it_stages_after_both_campaign_mirrors(monkeypatch):
    """Order is the mechanism: the cell's copy has to land on the campaign's, not under
    it."""
    command = _init_command(monkeypatch, _CLUSTER_CONFIGS)
    campaign = command.index("_config/ /config/")
    transient = command.index("_transient/ /config/")
    cell = command.index(f"/config/{DEPLOY_REL}")
    assert campaign < cell and transient < cell, command


def test_it_never_puts_a_cells_records_at_the_config_mount(monkeypatch):
    """`<config>/_config/` also holds `scenario.config`, which is the entrypoint's default
    parameter file -- which is why the deploy paths are named rather than mirrored."""
    command = _init_command(monkeypatch, _CLUSTER_CONFIGS)
    assert "/config/scenario.config" not in command, command
    assert "/config/sut.config" not in command, command
    assert "/config/cfg-a/" not in command, command


def test_a_campaign_staging_nothing_per_cell_adds_no_step(monkeypatch):
    command = _init_command(monkeypatch, [{"name": "cfg-a"}])
    assert "mc cp" not in command, command
