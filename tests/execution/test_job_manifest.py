# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Characterization tests for BatchJobRunner's job-manifest construction.

This is the previously-untested critical path (create_job_manifest /
_build_job_manifest / get_job_manifest). These tests pin the manifest shape a
scenario Job is submitted with — the container env, volumes, init container, the
per-job S3 wiring and deadline — so the manifest builder can be refactored/split
behind a safety net instead of blind.
"""

import pytest

from robovast.execution.backends import RunOptions  # noqa: F401  (import parity)
from robovast.execution.cluster_execution import in_pod_storage, kubernetes_backend
from robovast.execution.cluster_execution.kubernetes_backend import BatchJobRunner


class _FakeClusterConfig:
    def get_s3_endpoint(self):
        return "http://s3:9000"

    def get_s3_credentials(self):
        return ("ak", "sk")

    def get_registry_config(self):
        import types
        return types.SimpleNamespace(pull_secret_name="")


def _runner(monkeypatch, *, execution=None, configs=None, tmp_vast="/tmp/x.vast"):
    """Build a BatchJobRunner via for_batch with external calls stubbed."""
    monkeypatch.setattr(kubernetes_backend, "resolve_resources",
                        lambda res, ctx: dict(res) if isinstance(res, dict) else {})
    monkeypatch.setattr(in_pod_storage, "campaign_storage_location",
                        lambda cfg, camp: ("bkt", ""))
    campaign_data = {
        "configs": configs or [{"name": "cfgA"}],
        "execution": execution or {},
        "scenario_file": "scenario.osc",
        "vast": tmp_vast,
    }
    return BatchJobRunner.for_batch(
        campaign_data=campaign_data, campaign_id="camp-2026-07-17-120000",
        batch_tag="batch-0", runs=1, cluster_config=_FakeClusterConfig(),
        namespace="ns", image="img:test", kube_context=None)


def _env_dict(container):
    # Skip valueFrom entries (e.g. AVAILABLE_CPUS via fieldRef) — we assert on
    # the plain value-carrying env vars.
    return {e["name"]: e["value"] for e in container.get("env", []) if "value" in e}


def test_base_manifest_has_kueue_label_and_deadline(monkeypatch):
    r = _runner(monkeypatch, execution={"timeout": 30})
    # Kueue admits off the queue-name *label* (an annotation is ignored by Kueue).
    assert r.manifest["metadata"]["labels"]["kueue.x-k8s.io/queue-name"]
    # Every Job is wall-clock capped so a stuck scenario is force-killed.
    assert r.manifest["spec"]["activeDeadlineSeconds"] == 30  # per-run * runs_per_job(1)


def test_create_job_manifest_shape(monkeypatch):
    r = _runner(monkeypatch)
    job = r._build_jobs()[0]
    m = r.create_job_manifest(job, total_jobs=1)

    spec = m["spec"]["template"]["spec"]
    assert m["metadata"]["name"]  # a concrete, K8s-safe job name

    # Volumes the run relies on.
    assert {v["name"] for v in spec["volumes"]} == {
        "config", "out", "dshm", "ipc", "tmp"}

    # The s3-init init container mirrors the config tree in before the run.
    init = spec["initContainers"][0]
    assert init["name"] == "s3-init"
    assert _env_dict(init)["S3_ENDPOINT"] == "http://s3:9000"

    # Main container per-job wiring.
    main_env = _env_dict(spec["containers"][0])
    assert main_env["SCENARIO_FILE"] == "scenario.osc"
    assert main_env["OUTPUT_RESULT_PER_SCENARIO"] == "true"
    assert main_env["SCENARIO_PARAMETER_FILE"] == f"/config/{r._job_tag(job.index)}.params.yaml"
    assert main_env["OUTPUT_DIR"] == f"/out/_jobs/{r._job_artifact_path(job.index)}"
    assert main_env["S3_PREFIX"] == ""  # embedded per-campaign bucket → empty prefix
    # The behaviour tree is recorded unless a campaign opts out, so a cluster run is
    # explainable afterwards without anyone having remembered to ask for it.
    assert main_env["BT_LOG"] == "true"


def test_bt_log_can_be_turned_off(monkeypatch):
    """Stated as false rather than omitted: the pod spec says what the run did."""
    r = _runner(monkeypatch, execution={"bt_log": False})
    job = r._build_jobs()[0]
    m = r.create_job_manifest(job, total_jobs=1)
    main_env = _env_dict(m["spec"]["template"]["spec"]["containers"][0])
    assert main_env["BT_LOG"] == "false"


def _sidecar(manifest, name):
    """Sidecars are NATIVE sidecars: init containers with restartPolicy Always.

    That is what makes the pod's lifetime track the scenario. As ordinary containers a
    simulator sidecar never exits, so the Job stayed at 0/1 after the scenario had
    finished and uploaded its results. Asserting the location, not just the content, is
    the point -- put one back in spec.containers and the campaign hangs at the end.
    """
    spec = manifest["spec"]["template"]["spec"]
    assert name not in [c["name"] for c in spec["containers"]], \
        f"{name} must be a native sidecar, not a regular container"
    sc = next(c for c in spec["initContainers"] if c["name"] == name)
    assert sc["restartPolicy"] == "Always"
    return sc


def test_a_sidecar_is_appended_with_its_own_image(monkeypatch):
    """A sidecar no longer inherits the main container's image: it states one, which is
    what lets the system under test be a vanilla vendor image."""
    r = _runner(monkeypatch, execution={"containers": {
        "scenario": {"image": "img:test"},
        "sut": {"image": "nav2:humble", "resources": {"cpu": 2, "memory": "1Gi"}}}})
    job = r._build_jobs()[0]
    m = r.create_job_manifest(job, total_jobs=1)

    sut = _sidecar(m, "sut")
    assert sut["image"] == "nav2:humble"
    # No command declared -> the scenario-execution server, so a scenario can drive it
    # with remote("ipc:///ipc/sut").
    assert sut["command"][-1].endswith("secondary_entrypoint.sh")
    assert sut["resources"]["requests"]["cpu"] == "2"


def test_a_sidecar_with_a_command_runs_it_through_the_entrypoint(monkeypatch):
    """How a simulator, or any stack RoboVAST does not drive, is started.

    Through secondary_entrypoint.sh, not as the container's argv. The entrypoint sources
    the ROS overlay, tees stdout into the job's log dir and starts the resource monitor;
    a command exec'd directly as the entrypoint got none of them. That is not cosmetic --
    a colcon-built plugin only reaches PYTHONPATH once /opt/ros and /ws/install are
    sourced, so `rst sim --ros` died on an unregistered `ros2_bridge` while the scenario
    sat out its /scan timeout with no log anywhere explaining why.

    The command travels by env so ONE entrypoint serves both kinds of sidecar, and under
    a name of its own: `SECONDARY_COMMAND` is already a host-shell variable that compose
    substitutes into the service's `command:`, and one name meaning two things either
    side of that boundary is a trap.
    """
    r = _runner(monkeypatch, execution={"containers": {
        "scenario": {"image": "img:test"},
        "simulation": {"image": "rst-ros:jazzy",
                       "command": ["rst", "sim", "w.yaml", "--ros", "--headless"]}}})
    m = r.create_job_manifest(r._build_jobs()[0], total_jobs=1)
    sim = _sidecar(m, "simulation")
    assert sim["command"] == ["/usr/bin/tini", "--", "/bin/bash",
                              "/config/secondary_entrypoint.sh"]
    env = {e["name"]: e["value"] for e in sim["env"]}
    assert env["ROBOVAST_CONTAINER_COMMAND"] == "rst sim w.yaml --ros --headless"


def test_a_sidecar_without_a_command_still_runs_the_server(monkeypatch):
    """The remote() case: no command, no ROBOVAST_CONTAINER_COMMAND, same entrypoint."""
    r = _runner(monkeypatch, execution={"containers": {
        "scenario": {"image": "img:test"},
        "sut": {"image": "nav2:jazzy"}}})
    m = r.create_job_manifest(r._build_jobs()[0], total_jobs=1)
    sut = _sidecar(m, "sut")
    assert sut["command"] == ["/usr/bin/tini", "--", "/bin/bash",
                              "/config/secondary_entrypoint.sh"]
    assert "ROBOVAST_CONTAINER_COMMAND" not in {e["name"] for e in sut["env"]}


def test_job_tag_and_artifact_path_are_batch_namespaced(monkeypatch):
    r = _runner(monkeypatch)
    # Flat, slash-free job tag (K8s name / param file) vs nested artifact path.
    assert r._job_tag(3) == "batch-0-job-3"
    assert r._job_artifact_path(3) == "batch-0/job-3"
    # Unbatched runner (prepare-run): flat everywhere.
    r._batch_tag = None
    assert r._job_tag(3) == "job-3"
    assert r._job_artifact_path(3) == "job-3"
