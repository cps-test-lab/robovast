# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Characterization tests for BatchJobRunner's job-manifest construction.

The critical path is create_job_manifest / _build_job_manifest /
get_job_manifest. These tests pin the manifest shape a scenario Job is submitted
with — the container env, volumes, init container, the per-job S3 wiring and
deadline — so the manifest builder can be refactored/split behind a safety net
instead of blind.
"""


from kubernetes import client

from robovast.execution.backends import RunOptions  # noqa: F401  # pylint: disable=unused-import  (import parity)
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

    The pull-secret lookup is stubbed for exactly the same reason, and it was missed:
    ``_build_job_manifest`` falls back to reading the well-known push Secret when no pull
    secret is configured, which this fake cluster config is, so every test here issued a
    live ``read_namespaced_secret`` against the developer's kubeconfig. It is fail-soft --
    an unreachable API server ends up in the same ``pull_secret = ""`` as a 404 -- so the
    tests passed either way; they just paid a connection timeout each, which is this
    module taking sixteen minutes instead of a second. Raising ``ApiException`` is what a
    reachable cluster without that Secret returns, so the manifests under test are
    unchanged and the fallback path stays exercised, only without the socket.
    """
    monkeypatch.setattr(kubernetes_backend, "resolve_resources",
                        lambda res, ctx: dict(res) if isinstance(res, dict) else {})

    def _fake_discover(self):
        self._gpu_capacity = cluster_gpus
        self._gpu_runtime_class = runtime_class

    monkeypatch.setattr(BatchJobRunner, "_discover_gpu_support", _fake_discover)

    def _no_such_secret(self, *args, **kwargs):
        raise client.exceptions.ApiException(status=404, reason="Not Found")

    monkeypatch.setattr(kubernetes_backend.client.CoreV1Api, "read_namespaced_secret",
                        _no_such_secret)
    # And the registry, which `for_batch` dials through `_pin_image_refs`: it resolves
    # every image ref to the digest it names right now, one HEAD each. The same
    # fail-soft shape as the Secret read -- an unreachable registry leaves the ref as it
    # was -- so it too cost time rather than correctness. It stayed hidden while the
    # Secret read above was costing thirty-five seconds a test, which is a good reason
    # to state both here rather than leave the next reader to find the second one.
    monkeypatch.setattr(BatchJobRunner, "_resolve_digest", lambda self, ref: "")
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


def test_base_manifest_carries_no_external_queue_label_and_has_deadline(monkeypatch):
    r = _runner(monkeypatch, execution={"timeout": 30})
    # Admission is RoboVAST's own: a Job is created only once the cluster has room for it,
    # so it must carry no label that would hand it to an external queue as well. Guarded
    # rather than merely absent, because re-adding one is silent -- a second gate in front
    # of a controller that already decided would suspend jobs the controller thinks it
    # placed, and the campaign would wait on pods that are never going to exist.
    labels = r.manifest["metadata"].get("labels", {})
    assert not [k for k in labels if k.startswith("kueue.x-k8s.io/")], labels
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


def test_the_pod_is_told_which_node_it_landed_on(monkeypatch):
    """Only the downward API can answer it: a pod cannot see its own node, and
    ``instance_type`` is the same string on every node of a bare-metal cluster. Without
    this a heterogeneous cluster's slow machine reads as run-to-run variance."""
    r = _runner(monkeypatch)
    m = r.create_job_manifest(r._build_jobs()[0], total_jobs=1)
    env = {e["name"]: e for e in m["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["NODE_NAME"]["valueFrom"]["fieldRef"]["fieldPath"] == "spec.nodeName"
    # It must carry no literal value: a hardcoded node name would be recorded as the host
    # of every run in the campaign, on whatever machine they actually ran.
    assert "value" not in env["NODE_NAME"]


def test_bt_log_is_always_on_and_always_stated(monkeypatch):
    """Stated rather than omitted: the pod spec says what the run did, instead of deferring
    to a container default that may differ between image versions.

    There is no way to turn it off -- a run whose tree state was not recorded cannot be
    explained afterwards, and the file is small beside the rosbag.
    """
    r = _runner(monkeypatch)
    job = r._build_jobs()[0]
    m = r.create_job_manifest(job, total_jobs=1)
    main_env = _env_dict(m["spec"]["template"]["spec"]["containers"][0])
    assert main_env["BT_LOG"] == "true"


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
    """A sidecar does not inherit the main container's image: it states one, which is
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


_ROS_SHAPE_GPU = {
    "containers": {
        "scenario": {"image": "img:scenario"},
        "simulation": {"image": "img:sim", "command": ["roqsim", "sim", "w.yaml"],
                       "resources": {"gpu": 1}},
    },
}


def test_a_declared_gpu_lands_on_the_simulation_sidecar(monkeypatch):
    """The ROS shape: the simulator is a sidecar, so that is where the request must land.
    Putting it on the scenario container instead would consume a replica and charge quota
    while the process that actually renders still saw no device."""
    r = _runner(monkeypatch, execution=_ROS_SHAPE_GPU, cluster_gpus=16, runtime_class="nvidia")
    m = _job_manifest(r)
    sim = _sidecar(m, "simulation")
    assert sim["resources"]["limits"]["nvidia.com/gpu"] == "1"
    # Both, because admission sizes from the pod TEMPLATE -- no pod exists yet for Kubernetes to
    # default one from the other, so an empty request is accounted as zero GPUs.
    assert sim["resources"]["requests"]["nvidia.com/gpu"] == "1"
    assert _env_dict(sim)["NVIDIA_DRIVER_CAPABILITIES"] == "all"
    assert "nvidia.com/gpu" not in _main_of(m)["resources"].get("limits", {})


def test_a_gpu_on_a_sidecar_still_sets_the_pod_runtime_class(monkeypatch):
    """runtimeClassName is a pod field with no per-container form, so a sidecar's request
    has to reach it -- and this is the case a main-container-only implementation loses."""
    r = _runner(monkeypatch, execution=_ROS_SHAPE_GPU, cluster_gpus=16, runtime_class="nvidia")
    assert _job_manifest(r)["spec"]["template"]["spec"]["runtimeClassName"] == "nvidia"


def test_the_stepped_shape_puts_the_gpu_on_the_main_container(monkeypatch):
    """A simulator stepped in-process IS the scenario container, so the request belongs on
    the main container and there is no sidecar to carry it.

    Asserted on the base manifest rather than a per-job one: building a job for a stepped
    backend runs the config expansion that feeds the backend its per-job ``sim`` block, and
    that machinery is beside the point here -- the GPU decision is made once, where the main
    container is described.
    """
    stepped = {"containers": {
        "scenario": {"image": "img:both", "resources": {"gpu": 1}},
        "simulation": {"backend": "roqsim", "config": "w.yaml"},
    }}
    r = _runner(monkeypatch, execution=stepped, cluster_gpus=16, runtime_class="nvidia")
    main = _main_of(r.manifest)
    assert main["resources"]["limits"]["nvidia.com/gpu"] == "1"
    assert main["resources"]["requests"]["nvidia.com/gpu"] == "1"
    assert _env_dict(main)["NVIDIA_DRIVER_CAPABILITIES"] == "all"
    assert r.manifest["spec"]["template"]["spec"]["runtimeClassName"] == "nvidia"


def test_a_gpu_cluster_hands_out_nothing_undeclared(monkeypatch):
    """A GPU is opt-IN. The cluster advertising devices is not a reason to charge quota for
    one: a headless simulator that renders nothing was measured to run identically without
    it, while the request capped how many runs the ClusterQueue would admit."""
    r = _runner(monkeypatch, execution=_ROS_SHAPE, cluster_gpus=16, runtime_class="nvidia")
    m = _job_manifest(r)
    assert "nvidia.com/gpu" not in _sidecar(m, "simulation")["resources"].get("limits", {})
    assert "nvidia.com/gpu" not in _sidecar(m, "simulation")["resources"].get("requests", {})
    assert "runtimeClassName" not in m["spec"]["template"]["spec"]
    assert r.gpu_resources_requested() is False


def test_gpu_zero_opts_a_campaign_out(monkeypatch):
    """Still honoured, and still meaningful: it states the intent explicitly where a reader
    would otherwise wonder whether a GPU was simply forgotten."""
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
    r = _runner(monkeypatch, execution=_ROS_SHAPE_GPU, cluster_gpus=8, runtime_class=None)
    m = _job_manifest(r)
    assert "runtimeClassName" not in m["spec"]["template"]["spec"]
    # The request itself still stands: the resource is advertised, so it is schedulable.
    assert _sidecar(m, "simulation")["resources"]["limits"]["nvidia.com/gpu"] == "1"


def test_the_preflight_is_told_about_the_gpu_requirement(monkeypatch):
    """An uncovered request is suspended forever rather than rejected, so the ClusterQueue
    has to be checked before any job is created."""
    r = _runner(monkeypatch, execution=_ROS_SHAPE_GPU, cluster_gpus=16, runtime_class="nvidia")
    assert r.gpu_resources_requested() is True
    # And false when nothing declared one -- including on a cluster that has devices, which is
    # what keeps an undeclared campaign out of the GPU quota entirely.
    assert _runner(monkeypatch, execution=_ROS_SHAPE,
                   cluster_gpus=16).gpu_resources_requested() is False


def test_a_non_simulator_container_gets_a_gpu_when_it_asks_for_one(monkeypatch):
    """A system under test can be a legitimate GPU consumer -- a perception or inference stack
    -- and asking is how it says so. No role is privileged: a request is honoured on any
    container, and no container gets one it did not ask for."""
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
    # And the simulator gets none, so the pod asks for exactly the one device that was asked
    # for. Auto-claiming a second halves how many such jobs a replica count admits, for a
    # renderer nobody asked to render.
    assert "nvidia.com/gpu" not in _sidecar(m, "simulation")["resources"].get("limits", {})
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


# --- /dev/shm, and which containers the kubelet can restart -------------------------------

def _dshm(manifest):
    spec = manifest["spec"]["template"]["spec"]
    return next(v for v in spec["volumes"] if v["name"] == "dshm")


def test_dev_shm_is_unbounded_when_the_execution_block_names_no_size(monkeypatch):
    """The lane stays honest about an absent key rather than inventing a size of its own.

    A *composed* campaign does not reach this state -- composition settles `shm_size`
    for every campaign, declared or not (see
    tests/execution/test_shm_size_reaches_both_lanes.py). This pins the lane's own
    behaviour, which is what keeps the default in one place instead of two.
    """
    r = _runner(monkeypatch)
    volume = _dshm(r.create_job_manifest(r._build_jobs()[0], total_jobs=1))
    assert volume["emptyDir"] == {"medium": "Memory"}


def test_shm_size_bounds_the_shared_dev_shm(monkeypatch):
    """Without a sizeLimit a memory-backed emptyDir is sized from the pod's memory limits,
    so a campaign that declares none is handed the whole NODE's memory as its /dev/shm --
    charged to whichever container faults the page, and fatal by SIGBUS when it runs out."""
    r = _runner(monkeypatch, execution={"shm_size": "1Gi"})
    volume = _dshm(r.create_job_manifest(r._build_jobs()[0], total_jobs=1))
    assert volume["emptyDir"] == {"medium": "Memory", "sizeLimit": "1Gi"}


def test_every_container_shares_the_one_dev_shm(monkeypatch):
    """Which is the point of it -- ROS 2's default Fast DDS uses shared memory across the
    scenario / sut / simulation boundary -- and also why sizing it is a pod-level knob
    rather than a per-container one."""
    r = _runner(monkeypatch, execution={
        "containers": {"sut": {"image": "an-image"},
                       "simulation": {"image": "another-image"}}})
    spec = r.create_job_manifest(r._build_jobs()[0],
                                 total_jobs=1)["spec"]["template"]["spec"]
    mounted = [c["name"] for c in spec["containers"] + spec["initContainers"]
               if any(m["mountPath"] == "/dev/shm" for m in c.get("volumeMounts") or [])]
    assert "robovast" in mounted and "sut" in mounted and "simulation" in mounted


def test_only_native_sidecars_can_be_restarted(monkeypatch):
    """The invariant `pod_invalidating_restart` rests on: the pod is restartPolicy Never,
    so its regular container and the one-shot s3-init are never restarted by the kubelet at
    all. Only the native sidecars carry restartPolicy Always -- which is why the useful
    filter on a restart is the EXIT CODE, not the container's name."""
    r = _runner(monkeypatch, execution={
        "containers": {"sut": {"image": "an-image"},
                       "simulation": {"image": "another-image"}}})
    spec = r.create_job_manifest(r._build_jobs()[0],
                                 total_jobs=1)["spec"]["template"]["spec"]

    assert spec["restartPolicy"] == "Never"
    assert r.campaign_data is not None
    assert spec["initContainers"][0]["name"] == "s3-init"
    assert "restartPolicy" not in spec["initContainers"][0]
    sidecars = {c["name"]: c for c in spec["initContainers"][1:]}
    assert set(sidecars) == {"sut", "simulation"}
    assert all(c["restartPolicy"] == "Always" for c in sidecars.values())


def test_the_main_container_is_named_as_the_constant_says(monkeypatch):
    """`_container_role` maps this one name onto the `scenario` role; it is the single
    container name that never appears in a .vast."""
    from robovast.execution.cluster_execution.manifests import MAIN_CONTAINER_NAME

    r = _runner(monkeypatch)
    spec = r.create_job_manifest(r._build_jobs()[0],
                                 total_jobs=1)["spec"]["template"]["spec"]
    assert [c["name"] for c in spec["containers"]] == [MAIN_CONTAINER_NAME]

def test_a_job_pod_tolerates_the_campaign_node_taint_itself(monkeypatch):
    """The pod itself must carry it, not whatever admits it.

    A deployment that taints its campaign nodes depends on this toleration to schedule at
    all, and the failure mode if it goes missing is silent -- pods that never place, rather
    than an error -- so it is pinned here."""
    from robovast.execution.cluster_execution.node_placement import CAMPAIGN_NODE_TOLERATIONS

    m = _job_manifest(_runner(monkeypatch))
    tolerations = m["spec"]["template"]["spec"].get("tolerations") or []
    for expected in CAMPAIGN_NODE_TOLERATIONS:
        assert dict(expected) in tolerations, f"job pod must tolerate {expected}"


def test_the_toleration_is_not_duplicated(monkeypatch):
    """Additive and idempotent: rendering twice must not accumulate copies of the same
    toleration."""
    m = _job_manifest(_runner(monkeypatch))
    tolerations = m["spec"]["template"]["spec"].get("tolerations") or []
    assert len(tolerations) == len({tuple(sorted(t.items())) for t in tolerations})


def test_a_probe_asks_the_scenario_runner_to_report_on_itself(monkeypatch):
    """`--tick-log` is what gives the scenario container its only guard, and the probe is the
    run that needs it: its measurement decides the allocation every later run gets."""
    from robovast.execution.cluster_execution.kubernetes_backend import (SCENARIO_PARAMS_ENV,
                                                                        TICK_LOG_FLAG,
                                                                        probe_manifest)

    r = _runner(monkeypatch)
    base = r.create_job_manifest(r._build_jobs()[0], total_jobs=1)
    probe = probe_manifest(base, job_name="probe-n1", params_file="/config/p.yaml",
                           output_dir="/out/_calibration/n1",
                           display_name="calibration probe · n1")
    main = probe["spec"]["template"]["spec"]["containers"][0]
    params = next(e for e in main["env"] if e["name"] == SCENARIO_PARAMS_ENV)
    assert TICK_LOG_FLAG in params["value"]


def test_a_campaign_run_is_not_asked_to(monkeypatch):
    """Per-tick instrumentation on the trial's hot path, so every run paying for it is a cost
    with no reader: only the probe's file is ever read."""
    from robovast.execution.cluster_execution.kubernetes_backend import (SCENARIO_PARAMS_ENV,
                                                                        TICK_LOG_FLAG)

    r = _runner(monkeypatch)
    manifest = r.create_job_manifest(r._build_jobs()[0], total_jobs=1)
    main = manifest["spec"]["template"]["spec"]["containers"][0]
    values = [e.get("value", "") for e in main["env"] if e["name"] == SCENARIO_PARAMS_ENV]
    assert TICK_LOG_FLAG not in " ".join(values)


def test_a_campaigns_own_scenario_flags_survive_the_probe(monkeypatch):
    """Appended, not assigned: a campaign passing flags of its own would otherwise have them
    replaced on the one run whose behaviour must match the others'."""
    from robovast.execution.cluster_execution.kubernetes_backend import (SCENARIO_PARAMS_ENV,
                                                                        TICK_LOG_FLAG,
                                                                        probe_manifest)

    r = _runner(monkeypatch, execution={"log_tree": True})
    base = r.create_job_manifest(r._build_jobs()[0], total_jobs=1)
    probe = probe_manifest(base, job_name="p", params_file="/config/p.yaml", output_dir="/out/x",
                           display_name="calibration probe · n1")
    main = probe["spec"]["template"]["spec"]["containers"][0]
    value = next(e["value"] for e in main["env"] if e["name"] == SCENARIO_PARAMS_ENV)
    assert TICK_LOG_FLAG in value and "-t" in value
