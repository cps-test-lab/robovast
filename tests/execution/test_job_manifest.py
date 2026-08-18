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


def _runner(monkeypatch, *, execution=None, configs=None, tmp_vast="/tmp/x.vast",
            cluster_gpus=0, runtime_class=None):
    """Build a BatchJobRunner via for_batch with external calls stubbed.

    ``cluster_gpus``/``runtime_class`` stand in for the live cluster probe, which is the
    only thing that decides whether an unstated ``resources.gpu`` becomes a request. Stubbed
    rather than left real because otherwise every test here dials whatever cluster the
    developer's kubeconfig happens to point at.
    """
    monkeypatch.setattr(kubernetes_backend, "resolve_resources",
                        lambda res, ctx: dict(res) if isinstance(res, dict) else {})

    def _fake_discover(self):
        self._gpu_capacity = cluster_gpus
        self._gpu_runtime_class = runtime_class

    monkeypatch.setattr(BatchJobRunner, "_discover_gpu_support", _fake_discover)
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
    sourced, so `roqsim sim --ros` died on an unregistered `ros2_bridge` while the scenario
    sat out its /scan timeout with no log anywhere explaining why.

    The command travels by env so ONE entrypoint serves both kinds of sidecar, and under
    a name of its own: `SECONDARY_COMMAND` is already a host-shell variable that compose
    substitutes into the service's `command:`, and one name meaning two things either
    side of that boundary is a trap.
    """
    r = _runner(monkeypatch, execution={"containers": {
        "scenario": {"image": "img:test"},
        "simulation": {"image": "roqsim-ros:jazzy",
                       "command": ["roqsim", "sim", "w.yaml", "--ros", "--headless"]}}})
    m = r.create_job_manifest(r._build_jobs()[0], total_jobs=1)
    sim = _sidecar(m, "simulation")
    assert sim["command"] == ["/usr/bin/tini", "--", "/bin/bash",
                              "/config/secondary_entrypoint.sh"]
    env = {e["name"]: e["value"] for e in sim["env"]}
    assert env["ROBOVAST_CONTAINER_COMMAND"] == "roqsim sim w.yaml --ros --headless"


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
    # Unbatched runner: flat everywhere.
    r._batch_tag = None
    assert r._job_tag(3) == "job-3"
    assert r._job_artifact_path(3) == "job-3"


def test_a_sidecar_can_upload_what_it_writes_after_the_scenario_ends(monkeypatch):
    """A sidecar carries the S3 credentials, because it runs the upload script itself.

    /out is an emptyDir that dies with the pod, and the only thing that copies it out is
    the main container's --post-run upload -- which runs while the scenario finishes,
    BEFORE kubelet stops a sidecar. So everything a sidecar wrote after that was lost:
    the simulator's run.npz and capture/ (an .npz writes its index at close, so it exists
    only at shutdown), and the tail of every sidecar log -- the simulator's was truncated
    to nine lines of start-up for a 99-second run. secondary_entrypoint.sh now runs
    /tmp/s3_upload.sh once its workload exits, which needs these.
    """
    r = _runner(monkeypatch, execution={"containers": {
        "scenario": {"image": "img:test"},
        "simulation": {"image": "roqsim-ros:jazzy", "command": ["roqsim", "sim", "w.yaml"]}}})
    m = r.create_job_manifest(r._build_jobs()[0], total_jobs=1)
    env = _env_dict(_sidecar(m, "simulation"))
    assert env["S3_ENDPOINT"] == "http://s3:9000"
    assert env["S3_ACCESS_KEY"] == "ak"
    assert env["S3_SECRET_KEY"] == "sk"
    # And the anchor a relative artifact path resolves against, so a per-run file lands
    # in the run's own directory rather than at the campaign root.
    assert env["RUN_OUTPUT_DIR"] == "/out/cfgA/0"


# -- GPUs ---------------------------------------------------------------------------
#
# The requirement is "if the cluster has a GPU, the simulator uses it; if not, nothing
# changes". Both halves are asserted, and the second one harder: a CPU-only cluster must
# produce the manifest it produced before any of this existed.

_ROS_SHAPE = {
    "containers": {
        "scenario": {"image": "img:scenario"},
        "simulation": {"image": "img:sim", "command": ["roqsim", "sim", "w.yaml"]},
    },
}
_STEPPED_SHAPE = {
    "containers": {
        "scenario": {"image": "img:both"},
        "simulation": {"backend": "roqsim", "config": "w.yaml"},
    },
}


def _job_manifest(runner):
    job = runner._build_jobs()[0]
    return runner.create_job_manifest(job, total_jobs=1)


def _main_of(manifest):
    return manifest["spec"]["template"]["spec"]["containers"][0]


def test_a_cpu_only_cluster_produces_an_unchanged_manifest(monkeypatch):
    """The invariant. No GPU limit, no runtimeClassName, no driver-capability env -- the
    shape a campaign had before GPUs were supported at all."""
    r = _runner(monkeypatch, execution=_ROS_SHAPE, cluster_gpus=0)
    m = _job_manifest(r)
    assert "runtimeClassName" not in m["spec"]["template"]["spec"]
    sim = _sidecar(m, "simulation")
    assert "nvidia.com/gpu" not in sim["resources"].get("limits", {})
    assert "NVIDIA_DRIVER_CAPABILITIES" not in _env_dict(sim)
    assert "NVIDIA_DRIVER_CAPABILITIES" not in _env_dict(_main_of(m))


def test_a_gpu_cluster_gives_the_simulation_sidecar_a_gpu(monkeypatch):
    """The ROS shape: the simulator is a sidecar, so that is where the request must land.
    Putting it on the scenario container instead would consume a replica and charge quota
    while the process that actually renders still saw no device."""
    r = _runner(monkeypatch, execution=_ROS_SHAPE, cluster_gpus=16, runtime_class="nvidia")
    m = _job_manifest(r)
    sim = _sidecar(m, "simulation")
    assert sim["resources"]["limits"]["nvidia.com/gpu"] == "1"
    # Both, because Kueue reads the pod TEMPLATE -- no pod exists yet for Kubernetes to
    # default one from the other, so an empty request is accounted as zero GPUs.
    assert sim["resources"]["requests"]["nvidia.com/gpu"] == "1"
    assert _env_dict(sim)["NVIDIA_DRIVER_CAPABILITIES"] == "all"
    assert "nvidia.com/gpu" not in _main_of(m)["resources"].get("limits", {})


def test_a_gpu_on_a_sidecar_still_sets_the_pod_runtime_class(monkeypatch):
    """runtimeClassName is a pod field with no per-container form, so a sidecar's request
    has to reach it -- and this is the case a main-container-only implementation loses."""
    r = _runner(monkeypatch, execution=_ROS_SHAPE, cluster_gpus=16, runtime_class="nvidia")
    assert _job_manifest(r)["spec"]["template"]["spec"]["runtimeClassName"] == "nvidia"


def test_the_stepped_shape_puts_the_gpu_on_the_main_container(monkeypatch):
    """A simulator stepped in-process IS the scenario container, so the request belongs on
    the main container and there is no sidecar to carry it.

    Asserted on the base manifest rather than a per-job one: building a job for a stepped
    backend runs the config expansion that feeds the backend its per-job ``sim`` block, and
    that machinery is beside the point here -- the GPU decision is made once, where the main
    container is described.
    """
    r = _runner(monkeypatch, execution=_STEPPED_SHAPE, cluster_gpus=16,
                runtime_class="nvidia")
    main = _main_of(r.manifest)
    assert main["resources"]["limits"]["nvidia.com/gpu"] == "1"
    assert main["resources"]["requests"]["nvidia.com/gpu"] == "1"
    assert _env_dict(main)["NVIDIA_DRIVER_CAPABILITIES"] == "all"
    assert r.manifest["spec"]["template"]["spec"]["runtimeClassName"] == "nvidia"


def test_gpu_zero_opts_a_campaign_out(monkeypatch):
    """How a campaign runs wider than the advertised replica count on a GPU cluster."""
    execution = {"containers": {
        "scenario": {"image": "img:scenario"},
        "simulation": {"image": "img:sim", "command": ["roqsim"], "resources": {"gpu": 0}},
    }}
    r = _runner(monkeypatch, execution=execution, cluster_gpus=16, runtime_class="nvidia")
    m = _job_manifest(r)
    assert "nvidia.com/gpu" not in _sidecar(m, "simulation")["resources"].get("limits", {})
    assert "runtimeClassName" not in m["spec"]["template"]["spec"]


def test_an_explicit_count_wins_over_the_default(monkeypatch):
    execution = {"containers": {
        "scenario": {"image": "img:scenario"},
        "simulation": {"image": "img:sim", "command": ["roqsim"], "resources": {"gpu": 2}},
    }}
    r = _runner(monkeypatch, execution=execution, cluster_gpus=16, runtime_class="nvidia")
    sim = _sidecar(_job_manifest(r), "simulation")
    assert sim["resources"]["limits"]["nvidia.com/gpu"] == "2"


def test_nvidia_visible_devices_is_never_set(monkeypatch):
    """The device plugin injects the UUID it allocated into exactly the requesting
    container. Setting this to `all` ourselves -- as the Compose lane correctly does, where
    nothing allocates -- would hand every container every GPU regardless of quota."""
    r = _runner(monkeypatch, execution=_ROS_SHAPE, cluster_gpus=16, runtime_class="nvidia")
    m = _job_manifest(r)
    for container in (_main_of(m), _sidecar(m, "simulation")):
        assert "NVIDIA_VISIBLE_DEVICES" not in _env_dict(container)


def test_mujoco_gl_is_never_set(monkeypatch):
    """roqsim's own selector decides the backend from the machine it runs on; forcing a
    value here would override the one component that owns that decision."""
    r = _runner(monkeypatch, execution=_ROS_SHAPE, cluster_gpus=16, runtime_class="nvidia")
    m = _job_manifest(r)
    for container in (_main_of(m), _sidecar(m, "simulation")):
        assert "MUJOCO_GL" not in _env_dict(container)


def test_a_cluster_without_the_runtime_class_gets_no_runtime_class(monkeypatch):
    """A managed GPU node pool advertises the resource but registers no such RuntimeClass,
    and naming one that does not exist makes the API server reject the pod -- trading a slow
    campaign for one that cannot start at all."""
    r = _runner(monkeypatch, execution=_ROS_SHAPE, cluster_gpus=8, runtime_class=None)
    m = _job_manifest(r)
    assert "runtimeClassName" not in m["spec"]["template"]["spec"]
    # The request itself still stands: the resource is advertised, so it is schedulable.
    assert _sidecar(m, "simulation")["resources"]["limits"]["nvidia.com/gpu"] == "1"


def test_the_preflight_is_told_about_the_gpu_requirement(monkeypatch):
    """An uncovered request is suspended forever rather than rejected, so the ClusterQueue
    has to be checked before any job is created."""
    r = _runner(monkeypatch, execution=_ROS_SHAPE, cluster_gpus=16, runtime_class="nvidia")
    assert r.gpu_resources_requested() is True
    assert _runner(monkeypatch, execution=_ROS_SHAPE,
                   cluster_gpus=0).gpu_resources_requested() is False


def test_a_non_simulator_container_gets_a_gpu_when_it_asks_for_one(monkeypatch):
    """A system under test can be a legitimate GPU consumer -- a perception or inference stack
    -- and asking is how it says so. Only the *auto*-request is tied to the simulation role,
    because that is the container RoboVAST knows renders; nothing here can infer that someone
    else's stack wants a device, so an explicit request is honoured on any container."""
    execution = {"containers": {
        "scenario": {"image": "img:scenario"},
        "simulation": {"image": "img:sim", "command": ["roqsim"]},
        "sut": {"image": "nav2:jazzy", "resources": {"cpu": 3, "gpu": 1}},
    }}
    r = _runner(monkeypatch, execution=execution, cluster_gpus=16, runtime_class="nvidia")
    m = _job_manifest(r)
    sut = _sidecar(m, "sut")
    assert sut["resources"]["limits"]["nvidia.com/gpu"] == "1"
    assert sut["resources"]["requests"]["nvidia.com/gpu"] == "1"
    assert _env_dict(sut)["NVIDIA_DRIVER_CAPABILITIES"] == "all"
    # The simulator still gets its own, so the pod asks for two replicas in total -- worth
    # knowing, because that halves how many such jobs a given replica count admits.
    assert _sidecar(m, "simulation")["resources"]["limits"]["nvidia.com/gpu"] == "1"
    assert m["spec"]["template"]["spec"]["runtimeClassName"] == "nvidia"


def test_a_sut_gpu_alone_still_sets_the_pod_runtime_class(monkeypatch):
    """Even with the simulator opted out: runtimeClassName is decided across every container,
    so a stack that needs a device does not silently lose the runtime that provides it."""
    execution = {"containers": {
        "scenario": {"image": "img:scenario"},
        "simulation": {"image": "img:sim", "command": ["roqsim"], "resources": {"gpu": 0}},
        "sut": {"image": "nav2:jazzy", "resources": {"gpu": 1}},
    }}
    r = _runner(monkeypatch, execution=execution, cluster_gpus=16, runtime_class="nvidia")
    m = _job_manifest(r)
    assert m["spec"]["template"]["spec"]["runtimeClassName"] == "nvidia"
    assert "nvidia.com/gpu" not in _sidecar(m, "simulation")["resources"].get("limits", {})
    assert _sidecar(m, "sut")["resources"]["limits"]["nvidia.com/gpu"] == "1"


def test_a_sut_gpu_is_not_requested_on_a_cpu_only_cluster(monkeypatch):
    """An explicit request is honoured, but the coverage pre-flight is what stops it becoming
    a job that hangs: asking for a device no node advertises must be caught, not scheduled."""
    execution = {"containers": {
        "scenario": {"image": "img:scenario"},
        "sut": {"image": "nav2:jazzy", "resources": {"gpu": 1}},
    }}
    r = _runner(monkeypatch, execution=execution, cluster_gpus=0, runtime_class=None)
    # The declaration stands -- it is the operator's word, not a guess ...
    assert _sidecar(_job_manifest(r), "sut")["resources"]["limits"]["nvidia.com/gpu"] == "1"
    # ... and the pre-flight is told, so the queue is checked before any job is created.
    assert r.gpu_resources_requested() is True
