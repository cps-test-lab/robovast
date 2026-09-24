# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for ClusterService's launch behaviour (no cluster needed).

The service drives cluster campaigns **in-process** (one worker thread each) over a
KubernetesBackend; there is no per-campaign controller pod any more, so these cover
the launch *hooks* it answers for ServiceBase plus the aux-pod manifest that
replaced the old controller-pod sidecar.
"""

import datetime
import json
import tempfile
import types
import time
from pathlib import Path

import pytest

from robovast.common.variation.container_runner import ContainerSpec
from robovast.execution.cluster_execution.cluster_service import ClusterService
from robovast.execution.cluster_execution.container_runner import (AUX_LABEL,
                                                                   DEFAULT_AUX_DEADLINE_SECONDS,
                                                                   aux_pod_name,
                                                                   build_aux_pod_manifest)
from robovast.execution.control_server import (STOP_POSTPROCESSING, STOP_RUNS, Phase)
from robovast.service.interface import CreateCampaignRequest, JobKind
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore
from tests.service import behaviour_log


@pytest.fixture
def cs():
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tempfile.mkdtemp()))
    # reap_on_start=False: the reaper talks to the kube API, which no test has.
    svc = ClusterService(namespace="ns1", cluster_config_name="rke2",
                         cluster_config_kwargs={"foo": "bar"}, store=store,
                         reap_on_start=False)
    # No unit test may reach metrics.k8s.io. ``list_jobs`` reads pod metrics on every call, and
    # an unfaked client builds a real one and waits out its timeout against whatever kubeconfig
    # this machine happens to have -- a slow test whose result depends on the developer's
    # laptop. Tests that care about the reading install their own with ``_stub_metrics``.
    svc._k8s_custom = _MetricsApi
    return svc


def test_version_reports_kubernetes_backend(cs):
    assert cs.version().backend == "kubernetes"


def test_cluster_config_requires_name():
    cs = ClusterService(namespace="ns", cluster_config_name=None,
                        cluster_config_kwargs={}, reap_on_start=False)
    with pytest.raises(ValueError, match="cluster config not configured"):
        cs._cluster_config()


def test_nothing_is_adopted_before_the_auth_token_is_bound():
    """A resumed campaign mints its pods' data-plane token, and the secret it is minted
    from is bound to this object AFTER it is constructed. Adopting in the constructor
    failed every campaign a restart picked up with "no auth token bound to this service"
    -- which is exactly the campaign that had already spent its compute."""
    calls = []
    cs = ClusterService(namespace="ns2", cluster_config_name="rke2",
                        cluster_config_kwargs={}, reap_on_start=True)
    cs.reap_orphans = lambda: calls.append("reap")
    cs.resume_interrupted_campaigns = lambda: calls.append("resume") or {}

    assert calls == [], "the constructor adopted before anything could bind the secret"
    cs.bind_auth_token("master-secret")
    cs.start_serving()
    assert calls == ["reap", "resume"]
    assert cs.scoped_token("campaign:c-1")

    cs.start_serving()
    assert calls == ["reap", "resume"], "adopted twice"


def test_a_service_that_adopts_nothing_stays_quiet():
    calls = []
    cs = ClusterService(namespace="ns2", cluster_config_name="rke2",
                        cluster_config_kwargs={}, reap_on_start=False)
    cs.reap_orphans = lambda: calls.append("reap")
    cs.start_serving()
    assert calls == []


def test_a_campaigns_backend_carries_the_campaigns_data_plane_token():
    """Built from the controller state the campaign worker hands over, as the worker does.

    Without the token no pod of the campaign can fetch its inputs or deliver its outputs,
    and the backend refuses to start one -- so a state whose campaign id is not read here
    fails every campaign before its first Job.
    """
    from robovast.execution.cluster_execution import pod_access
    from robovast.execution.control_server import ControllerState

    cs = ClusterService(namespace="ns2", cluster_config_name="rke2",
                        cluster_config_kwargs={}, reap_on_start=False)
    cs.bind_auth_token("master-secret")
    cs._admission_controller = lambda: None
    backend = cs._build_backend(state=ControllerState(campaign_id="camp-2026-01-01-000000"))
    assert backend.data_token
    assert backend.data_token == cs.scoped_token(
        pod_access.campaign_scope("camp-2026-01-01-000000"))


def test_a_backend_for_no_campaign_carries_no_token():
    cs = ClusterService(namespace="ns2", cluster_config_name="rke2",
                        cluster_config_kwargs={}, reap_on_start=False)
    cs.bind_auth_token("master-secret")
    cs._admission_controller = lambda: None
    assert cs._build_backend(state=None).data_token == ""


def test_build_backend_threads_kube_context():
    """The context this service was built with must reach the K8s backend."""
    cs = ClusterService(namespace="ns2", cluster_config_name="rke2",
                        cluster_config_kwargs={}, reap_on_start=False,
                        kube_context="local")
    backend = cs._build_backend(state=None)
    assert backend.kube_context == "local"
    assert backend.namespace == "ns2"


def test_read_service_config_from_cluster_parses_env(monkeypatch):
    """The cluster Deployment's env is the authoritative config source."""

    from robovast.execution.cluster_execution import service_deploy

    class _EnvVar:
        def __init__(self, name, value):
            self.name, self.value = name, value

    container = types.SimpleNamespace(env=[
        _EnvVar("ROBOVAST_CLUSTER_CONFIG_NAME", "rke2"),
        _EnvVar("ROBOVAST_CLUSTER_CONFIG_KWARGS", '{"namespace": "ns9"}')])
    dep = types.SimpleNamespace(spec=types.SimpleNamespace(
        template=types.SimpleNamespace(spec=types.SimpleNamespace(
            containers=[container]))))

    seen = {}

    class _Apps:
        def read_namespaced_deployment(self, name, namespace, **kwargs):
            seen["request_timeout"] = kwargs.get("_request_timeout")
            return dep

    monkeypatch.setattr(service_deploy, "SERVICE_NAME", "robovast-service")
    import kubernetes
    monkeypatch.setattr(kubernetes.config, "load_kube_config", lambda **k: None)
    # The preflight builds its own ApiClient so it can bound retries, so this takes it.
    monkeypatch.setattr(kubernetes.client, "AppsV1Api", lambda *a, **k: _Apps())

    name, kwargs = service_deploy.read_service_config_from_cluster("default", "local")
    assert name == "rke2" and kwargs == {"namespace": "ns9"}
    # Explicitly bounded: the process-wide policy times out each *attempt*, and urllib3
    # would retry a failed connect three more times — so an unreachable cluster took
    # 4x the limit to report, when a caller told "10 seconds" expects one.
    assert seen["request_timeout"] is not None


# -- launch hooks -----------------------------------------------------------

def test_campaigns_run_in_parallel(cs):
    """Unlike local Docker, the cluster has no single-flight guard."""
    assert cs._guard_new_campaign() is None


def test_run_options_carry_postprocess_out_of_band(cs):
    """postprocess travels in the options, not the process env.

    One service process drives many campaigns, so an env var could not tell them
    apart — that is why RunOptions gained these fields.
    """
    opts = cs._run_options(CreateCampaignRequest(workspace_id="ws-x", postprocess=True))
    assert opts.postprocess is True
    assert opts.namespace == "ns1"

    off = cs._run_options(CreateCampaignRequest(workspace_id="ws-x", postprocess=False))
    assert off.postprocess is False


def test_run_options_carry_upload_to_share(cs):
    """The launch toggle flows into RunOptions (default off)."""
    on = cs._run_options(
        CreateCampaignRequest(workspace_id="ws-x", upload_to_share=True))
    assert on.upload_to_share is True
    default = cs._run_options(CreateCampaignRequest(workspace_id="ws-x"))
    assert default.upload_to_share is False


# -- a build the service cannot do is a config error, not a crash ------------

def _project_needing_a_build(tmp_path, python_packages=None):
    """A validated config whose scenario container adds packages, so an image is built."""
    from robovast.common.config import validate_config
    (tmp_path / "p.vast").write_text("")
    campaign_config = validate_config({
        "version": 6,
        "execution": {"runs": 1, "containers": {"scenario": {
            "image": "base:1",
            "python_packages": python_packages or ["shapely>=2.0"]}}}})
    return (types.SimpleNamespace(config_path=str(tmp_path / "p.vast")), campaign_config)


def test_a_build_ref_without_a_registry_fails_the_campaign_without_a_traceback(
        cs, monkeypatch, tmp_path):
    """The worker prints a stack trace for every exception it does not recognize, so a
    plain ValueError here made an unconfigured deployment read as a RoboVAST bug. It is
    bad input with an actionable message: the campaign fails carrying that message
    alone.

    Reachable for one reason now that RoboVAST ships its own registry: that registry is
    published on the service's Ingress, so a service with no Ingress still has nowhere a
    node could pull a built image back from."""

    from robovast.common.errors import CampaignConfigError
    from robovast.execution.cluster_config.base_config import RegistryConfig
    monkeypatch.setattr(
        cs, "_cluster_config",
        lambda: types.SimpleNamespace(get_registry_config=RegistryConfig))
    # A registry with no prefix: enabled() is false. The lookup that would fill in its
    # Secrets lives in the image store, so it is stubbed there — installing a store is how
    # the service supplies one.
    monkeypatch.setattr(
        cs, "_image_store",
        types.SimpleNamespace(
            registry=lambda require=True: RegistryConfig(registry_prefix="")),
        raising=False)
    project, campaign_config = _project_needing_a_build(tmp_path)

    with pytest.raises(CampaignConfigError, match="nowhere to push it"):
        cs._start_build_images(project, campaign_config)
    assert CampaignConfigError.include_traceback is False


def test_a_broken_build_section_is_a_config_error_too(cs, monkeypatch, tmp_path):
    from robovast.common.errors import CampaignConfigError

    project, campaign_config = _project_needing_a_build(
        tmp_path, python_packages=["./not_here"])

    with pytest.raises(CampaignConfigError, match="python_packages"):
        cs._start_build_images(project, campaign_config)


# -- jobs (live) ------------------------------------------------------------

def _job(name, *, succeeded=0, active=0, failed=0, full=None, suspend=False, kind=None):
    ann = {"job-name-full": full} if full is not None else {}
    # No ``labels`` attribute at all unless a kind is asked for: the listing has to read an
    # unlabelled Job as one of the campaign's runs.
    meta = ({"name": name} if kind is None
            else {"name": name, "labels": {"jobgroup": "scenario-runs", "job-kind": kind}})
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(**meta),
        status=types.SimpleNamespace(succeeded=succeeded, active=active, failed=failed),
        # suspend is vestigial -- nothing suspends a Job now -- but the field is part of
        # the Job shape the code reads, so the fake keeps it rather than diverging.
        spec=types.SimpleNamespace(suspend=suspend, template=types.SimpleNamespace(
            metadata=types.SimpleNamespace(annotations=ann))))


def test_list_jobs_reports_planned_jobs_as_waiting(cs, monkeypatch):
    """A job queued for capacity must be visibly ``waiting``, and only the controller knows.

    A batch that has not started must not read as "nothing is happening". A PLANNED
    job has no Kubernetes object at all -- so the listing has to ask the controller or the
    count silently becomes permanently zero, which is the failure this pins.
    """
    from robovast.execution.cluster_execution.node_admission import CREATED, PLANNED

    class _Admission:
        def states(self, owner):
            assert owner == "camp-2026-07-17-120000"
            return {"j-run": CREATED, "j-planned-b": PLANNED, "j-planned-a": PLANNED}

    class _Batch:
        def list_namespaced_job(self, namespace, label_selector):
            return types.SimpleNamespace(items=[_job("j-run", active=1)])

    monkeypatch.setattr(cs, "_k8s_batch", lambda: _Batch())
    monkeypatch.setattr(cs, "_k8s", lambda: _CoreWithPods([_job_pod("j-run")]))
    cs._admission = _Admission()
    resp = cs.list_jobs("camp-2026-07-17-120000")

    assert (resp.counts.running, resp.counts.waiting, resp.counts.total) == (1, 2, 3)
    waiting = [j for j in resp.jobs if j.status == "waiting"]
    # Sorted, so a listing does not reshuffle between polls for no reason.
    assert [j.job_name for j in waiting] == ["j-planned-a", "j-planned-b"]
    assert all(j.detail for j in waiting), "a waiting job must say why it is waiting"


def test_list_jobs_without_a_built_controller_reports_no_waiting(cs, monkeypatch):
    """A read path must degrade, not raise. If no campaign has submitted there is no
    controller and nothing planned -- the same answer either way, which is what lets the
    listing avoid building one just to ask."""
    class _Batch:
        def list_namespaced_job(self, namespace, label_selector):
            return types.SimpleNamespace(items=[_job("j-run", active=1)])

    monkeypatch.setattr(cs, "_k8s_batch", lambda: _Batch())
    monkeypatch.setattr(cs, "_k8s", lambda: _CoreWithPods([_job_pod("j-run")]))
    cs._admission = None
    resp = cs.list_jobs("camp-2026-07-17-120000")

    assert (resp.counts.waiting, resp.counts.total) == (0, 1)


def test_list_jobs_classifies_and_counts(cs, monkeypatch):
    """Per-job status mirrors the aggregate counter; counts sum to the total."""
    jobs = [
        _job("j-run", active=1, full="camp-2026-07-17-120000-batch-0-job-0"),
        _job("j-done", succeeded=1),
        _job("j-fail", failed=1),
        _job("j-pend"),
    ]
    seen = {}

    class _Batch:
        def list_namespaced_job(self, namespace, label_selector):
            seen.update(namespace=namespace, label_selector=label_selector)
            return types.SimpleNamespace(items=jobs)

    # j-run's pod is actually Running, so it counts as running (not just active).
    monkeypatch.setattr(cs, "_k8s_batch", lambda: _Batch())
    monkeypatch.setattr(cs, "_k8s", lambda: _CoreWithPods([_job_pod("j-run")]))
    resp = cs.list_jobs("camp-2026-07-17-120000")

    assert seen["namespace"] == "ns1"
    assert "jobgroup=scenario-runs" in seen["label_selector"]
    assert "campaign-id=camp-2026-07-17-120000" in seen["label_selector"]
    assert (resp.counts.running, resp.counts.completed, resp.counts.failed,
            resp.counts.pending, resp.counts.total) == (1, 1, 1, 1, 4)
    assert resp.counts.calibration == 0
    assert all(j.kind == "run" for j in resp.jobs), "an unlabelled Job is a campaign run"
    running = next(j for j in resp.jobs if j.job_name == "j-run")
    assert running.status == "running"
    # campaign prefix stripped from job-name-full for a readable label
    assert running.display_name == "batch-0-job-0"


def test_a_calibration_probe_is_listed_but_not_counted_as_a_run(cs, monkeypatch):
    """A probe carries the campaign's labels -- it is real work on a real node, and every
    selector that counts or cleans up scenario-runs has to keep seeing it -- so it reaches
    this listing. It must not reach the counts.

    These counts are read as facts about RUNS, not about jobs: the web UI takes
    ``failed`` as the runs that will never deliver (``lib/eta.ts``), and feeds it to the run
    meter, the ``done/total`` label and the ETA's divisor. Counted in, one failed probe
    reports a campaign run that never existed as finished.
    """
    jobs = [
        _job("j-run", active=1, full="camp-2026-07-17-120000-batch-0-job-0"),
        _job("probe-a", failed=1, full="calibration probe · node-a", kind="calibration"),
    ]

    class _Batch:
        def list_namespaced_job(self, namespace, label_selector):
            return types.SimpleNamespace(items=jobs)

    monkeypatch.setattr(cs, "_k8s_batch", lambda: _Batch())
    monkeypatch.setattr(cs, "_k8s", lambda: _CoreWithPods([_job_pod("j-run")]))
    resp = cs.list_jobs("camp-2026-07-17-120000")

    probe = next(j for j in resp.jobs if j.job_name == "probe-a")
    assert probe.kind == "calibration"
    assert probe.status == "failed", "its own status still reported -- a failed probe matters"
    # Named for the node it measures, not for the job whose manifest it was derived from.
    assert probe.display_name == "calibration probe · node-a"
    assert resp.counts.calibration == 1
    assert resp.counts.failed == 0, "the probe's failure is not a run's"
    assert (resp.counts.running, resp.counts.total) == (1, 1)


def _job_pod(job_name, phase="Running"):
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(
            name=f"{job_name}-pod",
            labels={"batch.kubernetes.io/job-name": job_name}),
        status=types.SimpleNamespace(phase=phase))


class _CoreWithPods:
    def __init__(self, pods, nodes=None):
        self._items = types.SimpleNamespace(items=pods)
        # Only an unschedulable pod makes the classifier ask for these, and without them
        # it gives the strict answer (see `_pod_signals`) -- so a test about contention
        # has to supply them or it silently tests the blocked path instead.
        self._nodes = types.SimpleNamespace(items=nodes or [])

    def list_namespaced_pod(self, namespace, label_selector):
        return self._items

    def list_node(self):
        return self._nodes


def test_list_jobs_reports_active_but_pending_pod_as_pending(cs, monkeypatch):
    """An 'active' Job whose pod is still Pending must not show as running."""
    jobs = [_job("j-admitted", active=1)]

    class _Batch:
        def list_namespaced_job(self, namespace, label_selector):
            return types.SimpleNamespace(items=jobs)

    monkeypatch.setattr(cs, "_k8s_batch", lambda: _Batch())
    monkeypatch.setattr(
        cs, "_k8s", lambda: _CoreWithPods([_job_pod("j-admitted", phase="Pending")]))
    resp = cs.list_jobs("camp-2026-07-17-120000")

    assert (resp.counts.running, resp.counts.pending) == (0, 1)
    assert next(j for j in resp.jobs if j.job_name == "j-admitted").status == "pending"


def test_resource_usage_counts_scenario_jobs_pod_accurate(cs, monkeypatch):
    """The jobs tally splits Running from still-waiting scenario runs, and ignores
    non-scenario workloads (the service pod, someone else's) entirely."""
    jobs = [_job("j-run-1", active=1), _job("j-run-2", active=1), _job("j-admitted", active=1)]
    pods = [
        _usage_pod({"jobgroup": "scenario-runs"}, "Running", node="n1"),
        _usage_pod({"app": "robovast-service"}, "Running", node="n1"),  # not a scenario run
    ]
    # j-admitted is 'active' but its pod has not reached Running, so it is pending.
    job_pods = [_job_pod("j-run-1"), _job_pod("j-run-2"),
                _job_pod("j-admitted", phase="Pending")]

    monkeypatch.setattr(cs, "_k8s_batch", lambda: _UsageBatch(jobs))
    monkeypatch.setattr(
        cs, "_k8s", lambda: _UsageCore([_usage_node("n1", "4", "8Gi")], pods, job_pods))
    _stub_metrics(cs, monkeypatch, {"n1": {"cpu": "1", "memory": "1Gi"}})
    usage = cs.resource_usage()

    assert (usage.jobs_running, usage.jobs_pending) == (2, 1)


def test_resource_usage_counts_podless_jobs_as_pending(cs, monkeypatch):
    """A Job whose pod does not exist yet must still count as pending.

    Regression: the tally read pods, so freshly created Jobs whose pods the scheduler had
    not bound yet reported ``0/0``, and the sidebar's jobs bar said nothing was happening
    while 3 runs were starting. A just-created Job is an active Job with no pod, so the
    tally has to read Jobs.
    """
    jobs = [_job("j-queued-1", active=1), _job("j-queued-2", active=1),
            _job("j-queued-3", active=1)]
    batch = _UsageBatch(jobs)

    monkeypatch.setattr(cs, "_k8s_batch", lambda: batch)
    monkeypatch.setattr(
        cs, "_k8s", lambda: _UsageCore([_usage_node("n1", "4", "8Gi")], [], []))
    _stub_metrics(cs, monkeypatch, {"n1": {"cpu": "1", "memory": "1Gi"}})
    usage = cs.resource_usage()

    assert batch.calls == 1, "the tally must read Jobs — a podless one is invisible to pods"
    assert (usage.jobs_running, usage.jobs_pending) == (0, 3)


def test_list_jobs_reports_a_contended_job_as_pending_not_blocked(cs, monkeypatch):
    """A job waiting for a node another campaign is holding.

    ``blocked`` is the count that says a human must intervene, so it must stay zero here:
    this job starts by itself as soon as a neighbour finishes. The scheduler's message
    still rides along, because "why is that one not moving" deserves an answer even when
    the answer is "it will".
    """
    jobs = [_job("j-busy", active=1)]

    class _Batch:
        def list_namespaced_job(self, namespace, label_selector):
            return types.SimpleNamespace(items=jobs)

    pod = _job_pod("j-busy", phase="Pending")
    pod.status.conditions = [types.SimpleNamespace(
        type="PodScheduled", status="False", reason="Unschedulable",
        message="0/4 nodes are available: 4 Insufficient cpu.")]
    pod.spec = types.SimpleNamespace(
        containers=[types.SimpleNamespace(
            name="scenario",
            resources=types.SimpleNamespace(requests={"cpu": "8"}))],
        init_containers=None)
    node = types.SimpleNamespace(
        metadata=types.SimpleNamespace(name="n1"),
        status=types.SimpleNamespace(allocatable={"cpu": "96"}))

    monkeypatch.setattr(cs, "_k8s_batch", lambda: _Batch())
    monkeypatch.setattr(cs, "_k8s", lambda: _CoreWithPods([pod], nodes=[node]))
    resp = cs.list_jobs("camp-2026-07-17-120000")

    assert (resp.counts.pending, resp.counts.blocked) == (1, 0)
    busy = next(j for j in resp.jobs if j.job_name == "j-busy")
    assert busy.status == "pending"
    assert "Insufficient cpu" in busy.detail


def test_resource_usage_counts_blocked_job_as_pending(cs, monkeypatch):
    """A job that cannot start on its own is accepted-but-not-executing: pending.

    The per-campaign ``JobCounts`` keeps ``blocked`` apart because that view has to act
    on it; a capacity meter only needs "not executing".
    """
    monkeypatch.setattr(cs, "_k8s_batch", lambda: _UsageBatch([_job("j-stuck", active=1)]))
    monkeypatch.setattr(
        cs, "_k8s",
        lambda: _UsageCore([_usage_node("n1", "4", "8Gi")], [],
                           [_blocked_job_pod("j-stuck")]))
    _stub_metrics(cs, monkeypatch, {"n1": {"cpu": "1", "memory": "1Gi"}})
    usage = cs.resource_usage()

    assert (usage.jobs_running, usage.jobs_pending) == (0, 1)



def _blocked_job_pod(job_name):
    """A job pod stuck on an unpullable image — ``pod_block_reason`` reads it as blocked."""
    pod = _job_pod(job_name, phase="Pending")
    pod.status.container_statuses = [types.SimpleNamespace(
        state=types.SimpleNamespace(
            waiting=types.SimpleNamespace(
                reason="ImagePullBackOff", message="Back-off pulling image"),
            terminated=None))]
    return pod


class _UsageBatch:
    """``list_namespaced_job`` for the scenario-run tally, counting its own calls.

    The count is asserted on, so a tally that stopped reading Jobs (and went back to
    guessing from pods) cannot leave these tests passing.
    """

    def __init__(self, jobs):
        self._items = types.SimpleNamespace(items=jobs)
        self.calls = 0

    def list_namespaced_job(self, namespace, label_selector):
        assert label_selector == "jobgroup=scenario-runs"   # every campaign, not one
        assert namespace == "ns1"
        self.calls += 1
        return self._items


def test_resource_usage_ignores_pods_no_node_granted(cs, monkeypatch):
    """Only pods bound to a live node count as used — a queue of pending runs must
    not report more cores in use than the cluster has ("29.7/24")."""
    pods = [
        # committed: 2 x 4 cores on the one node
        _usage_pod({"jobgroup": "scenario-runs"}, "Running", node="workstation", cpu="4",
                   mem=str(4 * 1024 ** 3)),
        _usage_pod({"jobgroup": "scenario-runs"}, "Running", node="workstation", cpu="4",
                   mem=str(4 * 1024 ** 3)),
        # queued for a node that has no room yet — demand, not usage
        _usage_pod({"jobgroup": "scenario-runs"}, "Pending", cpu="4", mem=str(4 * 1024 ** 3)),
        _usage_pod({"jobgroup": "scenario-runs"}, "Pending", cpu="4", mem=str(4 * 1024 ** 3)),
        # left behind by a node that was removed: its request is granted by nothing
        _usage_pod({"jobgroup": "scenario-runs"}, "Running", node="gone", cpu="8",
                   mem=str(8 * 1024 ** 3)),
    ]
    # The same five runs as Jobs: three own a Running pod, two are still queued.
    jobs = [_job(f"j-run-{i}", active=1) for i in range(3)] + \
           [_job(f"j-queued-{i}", suspend=True) for i in range(2)]
    job_pods = [_job_pod(f"j-run-{i}") for i in range(3)]

    monkeypatch.setattr(cs, "_k8s_batch", lambda: _UsageBatch(jobs))
    monkeypatch.setattr(
        cs, "_k8s",
        lambda: _UsageCore([_usage_node("workstation", "24", str(64 * 1024 ** 3))],
                           pods, job_pods))
    _stub_metrics(cs, monkeypatch, {"workstation": {"cpu": "1", "memory": "1Gi"}})
    usage = cs.resource_usage()

    assert usage.cpu_capacity == 24
    assert usage.cpu_used == 8
    assert usage.memory_used_bytes == 8 * 1024 ** 3
    assert usage.cpu_used <= usage.cpu_capacity
    # The request sum is also reported under the name that says what it is. `cpu_used` is
    # the same number, kept as the headline every existing consumer reads.
    assert usage.cpu_reserved == 8
    assert usage.memory_reserved_bytes == 8 * 1024 ** 3
    # the queued runs stay visible where pending work belongs
    assert (usage.jobs_running, usage.jobs_pending) == (3, 2)


def _metrics_env(cs, monkeypatch, nodes, usage_by_node=None, error=None):
    """A usage read over *nodes* (8 cores / 16 GiB each), with metrics answering as told."""
    monkeypatch.setattr(cs, "_k8s_batch", lambda: _UsageBatch([]))
    monkeypatch.setattr(
        cs, "_k8s",
        lambda: _UsageCore([_usage_node(n, "8", str(16 * 1024 ** 3)) for n in nodes], [], []))
    return _stub_metrics(cs, monkeypatch, usage_by_node, error)


def test_measured_usage_sums_metrics_over_the_capacity_node_set(cs, monkeypatch):
    """Measured cpu/memory is metrics-server's, summed over the SAME nodes as capacity.

    A node reporting metrics while not being in the node set (drained between the two reads,
    or belonging to a cluster the capacity sum skipped) must not land in the total: measured
    would then exceed capacity, which is the same class of wrong answer the request sum
    avoids by ignoring pods no node has granted.
    """
    _metrics_env(cs, monkeypatch, ["n1", "n2"], {
        "n1": {"cpu": "1500m", "memory": str(2 * 1024 ** 3)},
        "n2": {"cpu": "500000000n", "memory": str(1024 ** 3)},   # nanocores, as it publishes
        "gone": {"cpu": "8", "memory": str(16 * 1024 ** 3)},
    })

    usage = cs.resource_usage()

    assert usage.cpu_measured == pytest.approx(2.0)          # 1.5 + 0.5, not 10.0
    assert usage.memory_measured_bytes == 3 * 1024 ** 3
    assert usage.cpu_measured <= usage.cpu_capacity
    assert usage.metrics_unavailable is None


def test_measured_usage_is_absent_not_partial_when_a_node_is_missing(cs, monkeypatch):
    """A node with no metrics item blanks the reading and says so -- it does not halve it.

    ``parse_resource`` answers 0 for what it cannot parse, so summing blind would report a
    cluster at 60% of its cores as being at 30%: a wrong answer that looks right. A node that
    just joined is missing for ~15s, and a gap in the chart is the truth for that window --
    which is also why this reason is NOT memoised.
    """
    api = _metrics_env(cs, monkeypatch, ["n1", "n2"], {
        "n1": {"cpu": "4", "memory": str(8 * 1024 ** 3)},
        # n2 absent entirely, and n3 present with a memory quantity that will not parse.
        "n3": {"cpu": "1", "memory": "not-a-quantity"},
    })

    usage = cs.resource_usage()

    assert usage.cpu_measured is None
    assert usage.memory_measured_bytes is None
    assert "1 of 2 nodes" in usage.metrics_unavailable
    # Capacity and the request sum are unaffected: one missing reading is not an outage.
    assert usage.cpu_capacity == 16
    assert usage.cpu_reserved == 0

    cs._usage_cache = None
    cs.resource_usage()
    assert api.calls == 2, "a transient miss must be retried, not remembered"


@pytest.mark.parametrize(("status", "expected"), [
    (403, "vast service upgrade"),          # RBAC that predates the metrics grant
    (404, "install metrics-server"),        # a cluster that does not serve the API
])
def test_measured_usage_names_the_fix_and_stops_asking(cs, monkeypatch, status, expected):
    """No metrics API is not an error: the rest of the reading stands, with the reason.

    And the reason is remembered. A missing metrics-server changes only when someone installs
    one, so retrying every 10s window would spend a round trip and an audit-log line six
    times a minute to learn the same thing.
    """
    api = _metrics_env(cs, monkeypatch, ["n1"], error=_Refused(status=status))

    usage = cs.resource_usage()

    assert usage.cpu_measured is None
    assert expected in usage.metrics_unavailable
    # The service still answers what it can -- a chart with no fill, not a broken meter.
    assert usage.cpu_capacity == 8
    assert usage.cpu_reserved == 0

    cs._usage_cache = None
    again = cs.resource_usage()
    assert again.metrics_unavailable == usage.metrics_unavailable
    assert api.calls == 1, "the absent metrics API was asked twice"


def _disk_env(cs, monkeypatch, nodes, summaries, service_node, raise_on=None):
    """A usage read with the service pod placed on *service_node*. Returns the fake core."""
    monkeypatch.setattr(cs, "_k8s_batch", lambda: _UsageBatch([]))
    core = _UsageCore([_usage_node(n, "8", str(16 * 1024 ** 3)) for n in nodes],
                      [_service_pod(service_node)], [],
                      summaries=summaries, raise_on=raise_on)
    monkeypatch.setattr(cs, "_k8s", lambda: core)
    # These tests are about the disk meter, but a usage read also asks metrics-server.
    _stub_metrics(cs, monkeypatch, {n: {"cpu": "1", "memory": "1Gi"} for n in nodes})
    return core


def test_resource_usage_meters_the_services_own_node_not_a_sum(cs, monkeypatch):
    """Disk is the SERVICE's node filesystem. Not a cluster-wide sum, not capacityBytes.

    A sum answers a question nobody asks: the disk that decides whether a campaign can be
    written is the one under the service's workspaces -- a hostPath on a stock RKE2, so
    pinned to one node and absent from the kubelet's per-volume stats. At one node a sum
    coincided with it; at twenty it reports tens of terabytes free while that single disk
    fills, which is the reading that matters going wrong precisely as the cluster grows.

    Three separate mistakes are excluded by the numbers here, each of which would otherwise
    hide in the arithmetic: summing the nodes, folding in ``node.runtime.imageFs`` (the same
    device on a single-disk node), and taking ``capacityBytes`` (which counts reserved blocks
    nothing can write).
    """
    core = _disk_env(
        cs, monkeypatch, ["n1", "n2"],
        {"n1": _summary(40, 60, image_fs=777), "n2": _summary(400, 600, image_fs=888)},
        service_node="n2")

    usage = cs.resource_usage()

    # n2 alone: used 400, and 400 + 600 available.
    assert (usage.disk.capacity_bytes, usage.disk.used_bytes) == (1000, 400)
    assert usage.disk_unavailable is None
    assert usage.disk.capacity_bytes != 1100, "the two nodes were summed"
    assert usage.disk.capacity_bytes != 1000 + _RESERVED, "capacityBytes was used"
    assert core.proxy_calls[0] == ("n2", "stats/summary"), \
        "the service's node must be read first, so the disk figure survives a short budget"


def test_a_silent_kubelet_elsewhere_does_not_blank_the_disk(cs, monkeypatch):
    """Another node refusing is not the disk meter's problem any more.

    Under the old cluster-wide sum one unreadable node meant no disk figure at all, because
    a partial sum was indistinguishable from a real reading. Node-local, that coupling is
    gone: only the service's own node can take the disk figure down.
    """
    _disk_env(cs, monkeypatch, ["n1", "n2"],
              {"n1": _summary(40, 60)}, service_node="n1",
              raise_on={"n2": _Refused(status=500)})

    usage = cs.resource_usage()

    assert (usage.disk.capacity_bytes, usage.disk.used_bytes) == (100, 40)
    assert usage.disk_unavailable is None


def test_a_silent_kubelet_on_the_services_node_says_what_actually_failed(cs, monkeypatch):
    """No disk figure, and a reason that reports the real failure rather than guessing.

    Answering "the service needs `nodes/proxy`; run upgrade to reconcile RBAC" for every
    exception -- a timeout, a TLS refusal, a summary missing a key -- leaves the real cause
    no further than a debug log, and reconciling RBAC then returns the identical message.
    That is what makes a guess worse than no reason at all: the reader cannot tell a fix
    that did not work from a diagnosis that was never right.

    The capacity meter must survive it: an unreadable kubelet is not allowed to blank cpu and
    memory too.
    """
    _disk_env(cs, monkeypatch, ["n1"], {}, service_node="n1",
              raise_on={"n1": _Refused(status=None, reason="connection refused")})

    usage = cs.resource_usage()

    assert usage.disk is None and usage.results is None
    assert "connection refused" in usage.disk_unavailable
    assert "nodes/proxy" not in usage.disk_unavailable, \
        "only a 403 is an RBAC verdict; anything else must report its own cause"
    # The rest of the reading is untouched.
    assert usage.cpu_capacity == 8
    assert usage.memory_capacity_bytes == 16 * 1024 ** 3


def test_a_403_on_the_services_node_names_the_rbac_fix(cs, monkeypatch):
    """The one case that IS an RBAC verdict, and the only one allowed to claim it."""
    _disk_env(cs, monkeypatch, ["n1"], {}, service_node="n1",
              raise_on={"n1": _Refused(status=403, reason="Forbidden")})

    unavailable = cs.resource_usage().disk_unavailable

    assert "nodes/proxy" in unavailable and "403" in unavailable
    assert "--no-restart" in unavailable, \
        "the fix is offered without rolling the pod, so the reason must say so"


def test_no_service_pod_means_no_disk_and_a_reason_that_says_so(cs, monkeypatch):
    """The meter is node-local, so it cannot answer without knowing which node.

    Reported rather than silently omitted: a blank disk row with no explanation reads as a
    full disk to some people and a broken meter to others.
    """
    monkeypatch.setattr(cs, "_k8s_batch", lambda: _UsageBatch([]))
    monkeypatch.setattr(cs, "_k8s", lambda: _UsageCore(
        [_usage_node("n1", "8", "16Gi")], [], [], summaries={"n1": _summary(40, 60)}))
    _stub_metrics(cs, monkeypatch, {"n1": {"cpu": "1", "memory": "1Gi"}})

    usage = cs.resource_usage()

    assert usage.disk is None
    assert "could not be identified" in usage.disk_unavailable


def test_resource_usage_memoises_the_kubelet_summary(cs, monkeypatch):
    """The Summary read has its own, longer TTL than the usage cache.

    One payload carries every pod's stats on that node, and a disk fills over minutes — so a
    poll that refreshed it every usage window would be paying per open browser tab for a
    number that had not changed.
    """
    core = _disk_env(cs, monkeypatch, ["n1"], {"n1": _summary(40, 60)}, service_node="n1")

    cs.resource_usage()
    cs._usage_cache = None          # force a fresh capacity sample
    usage = cs.resource_usage()

    assert usage.disk.capacity_bytes == 100
    assert core.proxy_calls == [("n1", "stats/summary")]


def test_the_kubelet_connection_is_released(cs, monkeypatch):
    """Raw reads must be released, or the pool leaks one connection per node walked."""
    core = _disk_env(cs, monkeypatch, ["n1"], {"n1": _summary(40, 60)}, service_node="n1")
    cs.resource_usage()
    assert core.responses and all(r.released for r in core.responses)


def test_resource_usage_reports_the_results_volume(cs, monkeypatch):
    """The volume the campaigns live on, out of the per-pod stats already fetched."""
    _disk_env(cs, monkeypatch, ["n1"],
              {"n1": _summary(400, 600, pods=[_results_volume_pod(200, 600)])},
              service_node="n1")

    usage = cs.resource_usage()

    # The volume's own used, plus what the filesystem will still take: 200 + 600.
    assert (usage.results.capacity_bytes, usage.results.used_bytes) == (800, 200)
    # NOT the node filesystem's total. A volume with no size limit reports the whole
    # filesystem as its capacity -- a filesystem it shares with images, containers and
    # every campaign directory -- so `capacityBytes` reads as headroom that is not there.
    assert usage.disk.capacity_bytes == 1000
    assert usage.results.capacity_bytes < usage.disk.capacity_bytes


def test_the_node_walk_stops_at_the_services_own_node(cs, monkeypatch):
    """Both figures are the service pod's, so no other node can change either."""
    core = _disk_env(cs, monkeypatch, ["n1", "n2", "n3"],
                     {"n1": _summary(400, 600, pods=[_results_volume_pod(200, 600)]),
                      "n2": _summary(1, 1), "n3": _summary(1, 1)},
                     service_node="n1")

    assert cs.resource_usage().results is not None
    assert core.proxy_calls == [("n1", "stats/summary")], \
        "the walk must stop at the node that carries the service pod"


def test_a_host_path_results_dir_draws_no_second_meter(cs, monkeypatch):
    """A hostPath has no per-volume stats, and `disk` is already that filesystem.

    Reported as no figure rather than as a volume of size zero: no figure is honest, a
    wrong one is not.
    """
    core = _disk_env(cs, monkeypatch, ["n1", "n2", "n3"],
                     {"n1": _summary(400, 600, pods=[_service_pod_stats(volumes=[])]),
                      "n2": _summary(1, 1), "n3": _summary(1, 1)},
                     service_node="n1")

    usage = cs.resource_usage()
    assert usage.results is None
    assert usage.disk is not None, "the disk meter must survive a volume that cannot answer"
    assert core.proxy_calls == [("n1", "stats/summary")]


def test_resource_usage_has_no_results_meter_when_no_pod_reports_one(cs, monkeypatch):
    """The service pod is not in the stats at all -- the honest answer is no figure."""
    _disk_env(cs, monkeypatch, ["n1"], {"n1": _summary(400, 600)},
              service_node="n1")

    usage = cs.resource_usage()

    assert usage.results is None
    assert usage.disk is not None      # the disk meter is unaffected


def _usage_node(name, cpu, mem):
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(name=name),
        status=types.SimpleNamespace(allocatable={"cpu": cpu, "memory": mem}))


def _summary(fs_used, fs_available, image_fs=None, pods=()):
    """One kubelet ``stats/summary`` payload, as the node proxy returns it (JSON text).

    The meter reads ``usedBytes`` and ``availableBytes`` and derives capacity as their sum,
    because a filesystem's ``capacityBytes`` includes reserved blocks that cannot be
    written. So ``capacityBytes`` is emitted here as a DECOY -- deliberately not equal to
    ``used + available`` -- and every assertion below is written against the sum. A reader
    that went back to ``capacityBytes`` would fail rather than quietly over-report headroom.

    ``image_fs`` is likewise set to values DIFFERENT from ``node.fs``: on a single-disk node
    the two are the same device, and a reader that summed them would double the disk.
    Distinct numbers make that mistake visible instead of arithmetically invisible.
    """
    node = {"fs": {"capacityBytes": fs_used + fs_available + _RESERVED,
                   "usedBytes": fs_used, "availableBytes": fs_available}}
    if image_fs is not None:
        node["runtime"] = {"imageFs": {"capacityBytes": image_fs, "usedBytes": image_fs,
                                       "availableBytes": image_fs}}
    return json.dumps({"node": node, "pods": list(pods)})


#: Reserved blocks: in a filesystem's capacity, never writable. Non-zero here so that
#: ``capacityBytes`` and ``used + available`` can never coincide by accident.
_RESERVED = 7


def _service_pod_stats(volumes):
    """The service pod as the kubelet's per-pod stats carry it."""
    from robovast.execution.cluster_execution.service_deploy import SERVICE_NAME
    return {"podRef": {"name": f"{SERVICE_NAME}-abc123", "namespace": "ns1"},
            "volume": list(volumes)}


def _results_volume_pod(used, available):
    """The service pod carrying a measurable results volume.

    Same denominator as the disk meter: a volume with no ``sizeLimit`` reports the whole
    node filesystem as its ``capacityBytes`` -- a filesystem it shares with images,
    containers and every campaign directory. ``used + available`` is what it can really
    still reach, so ``capacityBytes`` is a decoy here too.
    """
    from robovast.execution.cluster_execution.service_deploy import RESULTS_VOLUME_NAME
    return _service_pod_stats([{"name": RESULTS_VOLUME_NAME, "usedBytes": used,
                                "availableBytes": available,
                                "capacityBytes": used + available + _RESERVED}])


def _usage_pod(labels, phase, node=None, cpu=None, mem=None, namespace="other"):
    """A pod as the cluster-wide list returns it.

    ``namespace`` defaults to one that is NOT the service's, so a pod only counts as the
    service's own when a test says so: the disk meter finds the service's node by matching
    both the ``app`` label and the namespace, and a default that matched would silently give
    every test a service node it never asked for.
    """
    requests = {}
    if cpu is not None:
        requests["cpu"] = cpu
    if mem is not None:
        requests["memory"] = mem
    containers = [types.SimpleNamespace(
        resources=types.SimpleNamespace(requests=requests))] if requests else []
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(labels=labels, namespace=namespace),
        status=types.SimpleNamespace(phase=phase),
        spec=types.SimpleNamespace(containers=containers, node_name=node))


def _service_pod(node, namespace="ns1"):
    """The robovast-service pod itself, which is how the disk meter learns its node.

    Taken from the cluster-wide pod list the CPU sum already fetches, so it costs no read
    of its own -- and it is the reason the disk figure is node-local rather than a sum.
    """
    from robovast.execution.cluster_execution.service_deploy import SERVICE_NAME
    return _usage_pod({"app": SERVICE_NAME}, "Running", node=node, namespace=namespace)


class _RawResponse:
    """urllib3-shaped response: ``.data`` bytes and an explicit ``release_conn``.

    ``release_conn`` is asserted on, because an unreleased connection leaks the pool one
    node at a time -- invisible until a long-lived service has walked enough nodes.
    """

    def __init__(self, payload):
        self.data = payload.encode() if isinstance(payload, str) else payload
        self.released = False

    def release_conn(self):
        self.released = True


class _Refused(Exception):
    """A kubelet read that failed, carrying the ``status`` the reason-builder reads."""

    def __init__(self, status=None, reason="connection refused"):
        super().__init__(reason)
        self.status = status
        self.reason = reason


class _MetricsApi:
    """``metrics.k8s.io`` node metrics, or the failure the cluster answers with.

    Counts calls, because the failure path is memoised (``_METRICS_ABSENT_TTL``) and a memo
    that quietly stopped working would leave the tests passing while the service asked an
    absent metrics-server six times a minute forever.
    """

    def __init__(self, usage_by_node=None, error=None, pods=None, pod_error=None):
        self._usage = usage_by_node or {}
        self._error = error
        self._pods = list(pods or [])
        self._pod_error = pod_error
        self.calls = 0
        self.pod_calls = 0

    def list_cluster_custom_object(self, group, version, plural, **kwargs):
        assert (group, version, plural) == ("metrics.k8s.io", "v1beta1", "nodes")
        # Unbounded, this read would turn "is the backend there?" into a hang.
        assert kwargs.get("_request_timeout")
        self.calls += 1
        if self._error is not None:
            raise self._error
        return {"items": [{"metadata": {"name": name}, "usage": usage}
                          for name, usage in self._usage.items()]}

    def list_namespaced_custom_object(self, group, version, namespace, plural, **kwargs):
        """Pod metrics, counted separately from the node read.

        Two counters and two error slots because the two grants are given independently: a
        role may carry ``nodes`` and not ``pods``, and one fake answering both identically
        could not tell a shared memo from a separate one.
        """
        assert (group, version, plural) == ("metrics.k8s.io", "v1beta1", "pods")
        assert kwargs.get("_request_timeout")
        # Every campaign's jobs are read in one list; a per-campaign selector would make the
        # cost grow with the number of open campaign cards.
        assert "campaign-id" not in (kwargs.get("label_selector") or "")
        self.pod_calls += 1
        if self._pod_error is not None:
            raise self._pod_error
        return {"items": list(self._pods)}


def _stub_metrics(cs, monkeypatch, usage_by_node=None, error=None, pods=None, pod_error=None):
    """Install the metrics fake and hand it back.

    Every test that reads usage needs one: without it ``_measured_cpu_mem`` builds a real
    client and tries the live cluster, which turns a unit test into a two-second timeout.
    """
    api = _MetricsApi(usage_by_node, error, pods, pod_error)
    monkeypatch.setattr(cs, "_k8s_custom", lambda: api)
    return api


class _UsageCore:
    """Two distinct pod reads: cluster-wide for CPU/memory, namespaced for the job tally.

    They are separate on purpose — capacity and usage must be summed over every node the
    cluster has, while the scenario-run tally answers "what is *this* service running".
    """

    def __init__(self, nodes, pods, job_pods=(), summaries=None, raise_on=None):
        self._nodes = types.SimpleNamespace(items=nodes)
        self._pods = types.SimpleNamespace(items=pods)
        self._job_pods = types.SimpleNamespace(items=list(job_pods))
        # {node: stats/summary JSON} for the disk meter, plus the nodes whose kubelet
        # refuses. proxy_calls counts reads so a test can prove the summary is memoised
        # on its own TTL rather than re-fetched with every usage poll.
        self._summaries = summaries or {}
        # {node: exception}. A mapping rather than a name set, because *why* a kubelet did
        # not answer is now part of the reported reason: only a 403 is an RBAC verdict.
        self._raise_on = dict(raise_on or {})
        self.proxy_calls = []
        # Every raw response handed out, so a test can prove each was released.
        self.responses = []

    def connect_get_node_proxy_with_path(self, name, path, **kwargs):
        """The RAW response the real client returns for ``_preload_content=False``.

        Not the parsed body, and that shape is load-bearing: the generated client declares
        this endpoint's response_type as ``str``, so a preloaded read hands back a
        single-quoted Python repr that ``json.loads`` rejects. The meter reads ``.data`` and
        releases the connection, so a fake returning a bare string would pass while the real
        client failed -- which is exactly the bug that shipped.
        """
        self.proxy_calls.append((name, path))
        raiser = self._raise_on.get(name)
        if raiser is not None:
            raise raiser
        resp = _RawResponse(self._summaries[name])
        self.responses.append(resp)
        return resp

    def list_node(self):
        return self._nodes

    def list_pod_for_all_namespaces(self, field_selector):
        return self._pods

    def list_namespaced_pod(self, namespace, label_selector):
        return self._job_pods


def _pod(name="pod-1", phase="Running", sidecars=()):
    """A scenario-run pod: the main ``robovast`` container plus *sidecars* by name.

    Sidecars go where Kubernetes puts native ones — ``initContainers`` with
    ``restartPolicy: Always`` — alongside the ordinary ``s3-init``, so a test that says
    "all three containers" is testing the real pod shape.
    """
    init = [types.SimpleNamespace(name="s3-init", restart_policy=None)]
    init += [types.SimpleNamespace(name=n, restart_policy="Always") for n in sidecars]
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(name=name),
        spec=types.SimpleNamespace(
            containers=[types.SimpleNamespace(name="robovast")],
            init_containers=init),
        status=types.SimpleNamespace(phase=phase))


def _api_exception(status):
    """The kube API's "container is waiting to start" (400) / "gone" (404)."""
    from kubernetes import client
    return client.exceptions.ApiException(status=status)


def _no_pod(cs, monkeypatch, tmp_path, files):
    """A job whose pod is gone, and a campaign holding *files* (rel path -> bytes)."""

    class _Core:
        def list_namespaced_pod(self, namespace, label_selector):
            return types.SimpleNamespace(items=[])

    monkeypatch.setattr(cs, "_k8s", lambda: _Core())
    monkeypatch.setattr(cs, "_campaigns_root", lambda: tmp_path)
    for rel, blob in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)


# -- stop (terminates in-flight cluster workloads) --------------------------

def _stop_state(flagged, phase=Phase.RUNNING):
    """A control-channel double for stop: which scope was flagged, and the phase it is
    chosen by. Records the scope rather than a bare boolean -- which unit of work a stop
    lands on is the thing under test."""
    return types.SimpleNamespace(
        request_stop=lambda scope=STOP_RUNS: flagged.update(stopped=True, scope=scope),
        snapshot=lambda: types.SimpleNamespace(phase=phase))



def test_stop_flags_state_and_tears_down_this_campaign(cs, monkeypatch):
    """Stop sets the cooperative flag AND deletes only this campaign's workloads.

    The batch wait loop never checks the flag, so without the teardown a batch
    campaign's Stop would do nothing; the teardown is campaign-scoped so other
    queued/running campaigns are untouched.
    """
    flagged = {}
    cs._campaigns["camp-1"] = types.SimpleNamespace(state=_stop_state(flagged))

    calls = {}
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.cluster_execution.cleanup_cluster_campaign",
        lambda **kw: calls.update(kw))

    res = cs.stop("camp-1")
    assert res.ok and flagged.get("scope") == STOP_RUNS
    # Scoped to this campaign, in this namespace/context (reuses jobs-cleanup), and
    # without its aux pods -- see test_stop_leaves_the_aux_pod_its_own_composition_holds.
    assert calls == {"namespace": "ns1", "campaign": "camp-1", "context": None,
                     "aux": False}


def test_stop_during_postprocessing_says_what_it_leaves(cs, monkeypatch):
    """Postprocessing runs in the service process, so the flag is what ends it: the
    pipeline polls it between steps.

    The reply has to say so, because the outcome differs from stopping a run: the runs are
    over and every result they produced is kept -- what the stop gives up is the derived
    data, and only a re-run brings it back. It must not name the phase the campaign ends
    in, which depends on how its runs ended rather than on this stop.
    """
    flagged = {}
    cs._campaigns["camp-1"] = types.SimpleNamespace(
        state=_stop_state(flagged, phase=Phase.POSTPROCESSING))
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.cluster_execution.cleanup_cluster_campaign",
        lambda **kw: None)

    res = cs.stop("camp-1")

    assert res.ok
    # The analysis, not the runs: flagging the runs here is what used to discard the
    # analysis of the batches that had already finished.
    assert flagged.get("scope") == STOP_POSTPROCESSING
    assert "postprocessing" in res.message and "re-run postprocessing" in res.message
    assert "finished" not in res.message
    # Not the run-phase wording: nothing of this campaign was still executing.
    assert "in-flight jobs terminated" not in res.message


def test_cluster_stop_on_an_ended_campaign_is_refused(cs, monkeypatch):
    """The stop scope decision is on the base, and it refuses a campaign that is over."""
    flagged = {}
    cs._campaigns["camp-1"] = types.SimpleNamespace(
        state=_stop_state(flagged, phase=Phase.FINISHED))
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.cluster_execution.cleanup_cluster_campaign",
        lambda **kw: None)

    res = cs.stop("camp-1")

    assert res.ok is False and "already over" in res.message
    assert flagged == {}


def test_stop_unknown_campaign_touches_no_cluster(cs, monkeypatch):
    """A campaign not driven here reports not-tracked and deletes nothing."""
    called = {"n": 0}
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.cluster_execution.cleanup_cluster_campaign",
        lambda **kw: called.__setitem__("n", called["n"] + 1))
    res = cs.stop("nope")
    assert res.ok is False and "not running here" in res.message
    assert called["n"] == 0


def test_shutdown_leaves_running_campaigns_for_the_successor(cs, monkeypatch):
    """Exiting a cluster service does NOT tear down its campaigns' Jobs.

    A cluster campaign's compute outlives the process and the next one adopts it, so a
    pod replacement must not be a data-loss event. The stop is not merely wasteful: it
    persists a terminal outcome, after which no successor would pick the campaign up.
    """
    calls = []
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.cluster_execution.cleanup_cluster_campaign",
        lambda **kw: calls.append(kw))
    stopped = []
    state = types.SimpleNamespace(
        request_stop=lambda scope=STOP_RUNS: stopped.append(scope))
    entry = types.SimpleNamespace(campaign_id="camp-a", state=state, thread=None)
    cs._campaigns["camp-a"] = entry
    monkeypatch.setattr(type(cs), "_is_done", lambda self, e: False)

    cs.shutdown()

    assert calls == []      # no teardown
    assert stopped == []    # and no cooperative stop, which would end the campaign


def test_shutdown_never_shells_out(cs, monkeypatch):
    """Exiting leaves the running campaigns to the successor and touches no container
    runtime: a teardown by shell would run inside the controller pod, where there is no
    daemon and a container name means nothing. The answer is ClusterService's own hook, not a
    predicate on a shared ``shutdown``.
    """
    import subprocess

    def _no_daemon_here(*_a, **_k):
        raise AssertionError("ClusterService shelled out on shutdown")

    monkeypatch.setattr(subprocess, "run", _no_daemon_here)
    state = types.SimpleNamespace(request_stop=lambda scope=STOP_RUNS: None)
    cs._campaigns["camp-a"] = types.SimpleNamespace(
        campaign_id="camp-a", state=state, thread=None)
    monkeypatch.setattr(type(cs), "_is_done", lambda self, e: False)

    cs.shutdown()

    assert "_shutdown_running_campaigns" in vars(type(cs)), "the answer is ClusterService's own"


def test_stop_still_tears_down_that_campaigns_jobs(cs, monkeypatch):
    """Not adopting on exit does not weaken ``stop``: that still deletes the Jobs.

    Guards the seam the change above rests on -- exiting is not how a campaign is
    stopped, and the way that *is* has to keep working.
    """
    calls = []
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.cluster_execution.cleanup_cluster_campaign",
        lambda **kw: calls.append(kw))

    cs._teardown_campaign_jobs("camp-a")

    assert [c["campaign"] for c in calls] == ["camp-a"]
    assert calls[0]["namespace"] == "ns1"


def test_a_long_aux_wait_keeps_saying_how_long_it_has_waited(monkeypatch, caplog):
    """A reason logged once, seconds in, is what made a slow pull read as a hang.

    ``wait_pod_ready`` polls every two seconds, so every poll must not be logged either.
    What separates the two cases is the elapsed figure on a repeat.
    """
    import logging

    from robovast.execution.cluster_execution import cluster_service as cs_mod

    clock = [0.0]
    monkeypatch.setattr(cs_mod.time, "monotonic", lambda: clock[0])
    report = cs_mod._aux_pending_logger("camp-a")

    with caplog.at_level(logging.INFO, logger=cs_mod.logger.name):
        report("PodInitializing: init container mc-tools is still running")
        clock[0] = 2.0
        report("PodInitializing: init container mc-tools is still running")
        clock[0] = 200.0
        report("PodInitializing: init container mc-tools is still running")

    said = [r.getMessage() for r in caplog.records]
    assert len(said) == 2  # the poll two seconds later says nothing new
    assert "after 0s" in said[0]
    assert "after 200s" in said[1]


def test_stop_leaves_the_aux_pod_its_own_composition_holds(cs, monkeypatch):
    """The driver is in this process, and its composition span owns that pod.

    Reaping it here removes a pod the span is exec'ing into: every exec then fails with a
    404 on the ``pods/exec`` subresource, and a campaign that was merely stopped reports a
    simulator that could not be asked about its world.
    """
    calls = []
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.cluster_execution.cleanup_cluster_campaign",
        lambda **kw: calls.append(kw))

    cs._teardown_campaign_jobs("camp-a")

    assert calls[0]["aux"] is False


# -- the launch record is written before anything can fail ------------------

def test_recording_the_launch_writes_it_into_the_campaign(cs, tmp_path):
    """At the top of the driver, into the campaign itself.

    The campaign directory is the campaign's durable home, so a campaign that never finished
    still carries the record of what it was launched with -- which is the set someone comes
    looking at, and the set a restart re-launches from.
    """
    cs._record_launch("camp-a", str(tmp_path), CreateCampaignRequest(workspace_id="ws"))

    assert (tmp_path / "camp-a" / "_execution" / "launch.yaml").is_file()


# -- aux pod (replaces the controller-pod sidecar) --------------------------

def _spec():
    return ContainerSpec(image="ghcr.io/secorolab/scenery_builder:1.2",
                         command_prefix=["/entry.sh"],
                         keep_alive_command=["sleep", "infinity"],
                         env={"A": "1"}, run_as_user="1000:1000")


def _staging(tmp_path):
    return {"stage_dir": lambda slot: tmp_path / "_staged" / slot,
            "token_for": lambda scope: "tok"}


def test_aux_pod_manifest_shape(tmp_path):
    m = build_aux_pod_manifest("nav-2026-07-17-120000", [_spec()], "ns1", **_staging(tmp_path))
    assert m["kind"] == "Pod"
    assert m["metadata"]["name"] == aux_pod_name("nav-2026-07-17-120000")
    assert m["metadata"]["namespace"] == "ns1"
    assert m["metadata"]["labels"]["app"] == "robovast-aux"
    assert AUX_LABEL == "app=robovast-aux"
    spec = m["spec"]
    assert spec["restartPolicy"] == "Never"
    # Backstop so a leaked aux pod always dies by itself.
    assert spec["activeDeadlineSeconds"] == DEFAULT_AUX_DEADLINE_SECONDS
    c = spec["containers"][0]
    assert c["image"] == "ghcr.io/secorolab/scenery_builder:1.2"
    # The image's one-shot entrypoint is overridden so it stays up for the campaign.
    assert c["command"] == ["sleep", "infinity"]
    assert c["env"] == [{"name": "A", "value": "1"}]
    assert c["securityContext"] == {"runAsUser": 1000}


def test_aux_pod_is_labelled_per_campaign(tmp_path):
    """So concurrent campaigns' aux pods never collide and cleanup can target one."""
    a = build_aux_pod_manifest("camp-a-2026-07-17-120000", [_spec()], "ns", **_staging(tmp_path))
    b = build_aux_pod_manifest("camp-b-2026-07-17-120000", [_spec()], "ns", **_staging(tmp_path))
    assert a["metadata"]["name"] != b["metadata"]["name"]
    assert (a["metadata"]["labels"]["campaign-id"]
            != b["metadata"]["labels"]["campaign-id"])


def test_aux_pod_owner_reference_ties_it_to_the_service_pod(tmp_path):
    """K8s then GCs it if the service is replaced — the sidecar's old guarantee."""
    owner = {"apiVersion": "v1", "kind": "Pod", "name": "robovast-service-x",
             "uid": "abc", "controller": False, "blockOwnerDeletion": False}
    m = build_aux_pod_manifest("c-2026-07-17-120000", [_spec()], "ns", owner_ref=owner,
                               **_staging(tmp_path))
    assert m["metadata"]["ownerReferences"] == [owner]


def test_scene_geometry_is_keyed_on_the_simulators_image():
    """The world lives in the SIMULATION image, so its digest is what identifies geometry.

    Keying on `image_revision` -- the scenario container's -- sent the build into an image
    with neither the world nor the exporter. It failed as an exec that could not start,
    reported through the Kubernetes client's int() of the exec status as
    "invalid literal for int()", which reads as a RoboVAST bug rather than a wrong image.
    """
    from unittest.mock import patch

    from robovast.service import scene_cache

    meta = {"image_revision": "reg/scenario@sha256:" + "a" * 64,
            "image_revisions": {"simulation": "reg/sim@sha256:" + "b" * 64,
                                "scenario": "reg/scenario@sha256:" + "a" * 64}}
    # Patched at its source: world_identity imports it inside the function.
    with patch("robovast.common.campaign_data.read_execution_metadata",
               lambda _p: meta):
        identity = scene_cache.world_identity("/campaign", {"world": "w.yaml",
                                                            "overrides": {}})
    assert identity["image"] == "reg/sim@sha256:" + "b" * 64


def _stepped_campaign(tmp_path, revision):
    """A campaign whose simulator is stepped in-process: the ``simulation`` block names no
    image or command, so it IS the scenario container."""
    import yaml
    (tmp_path / "_execution").mkdir(parents=True, exist_ok=True)
    (tmp_path / "_execution" / "execution.yaml").write_text(
        yaml.safe_dump({"image_revision": revision}))
    (tmp_path / "_config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "_config" / "p.vast").write_text(yaml.safe_dump(
        {"version": 6, "execution": {"containers": {"scenario": {"image": "reg/combined:1"},
                                                    "simulation": {}}}}))
    return tmp_path


def test_scene_geometry_uses_the_campaign_image_for_a_stepped_simulator(tmp_path):
    """A campaign recorded before per-role digests still resolves **when the simulator is
    folded onto the scenario container** -- there the campaign-level digest really is the
    simulator's.

    Deliberately narrower than the fallback this replaces: that one applied to *every*
    campaign, which is how a separate simulation container ended up compiling its geometry
    in the scenario image (see the test below).
    """
    from robovast.service import scene_cache

    revision = "reg/combined@sha256:" + "c" * 64
    identity = scene_cache.world_identity(_stepped_campaign(tmp_path, revision),
                                          {"world": "w.yaml", "overrides": {}})
    assert identity["image"] == revision


def test_scene_geometry_refuses_rather_than_borrow_the_scenario_image(tmp_path):
    """The regression: a separate simulation container with no per-role digest must refuse.

    Borrowing ``image_revision`` here ran ``roqsim-export-web`` in an image that does not
    contain it, reported as a bare ``exit status 127``.
    """
    import yaml

    from robovast.service import scene_cache

    (tmp_path / "_execution").mkdir(parents=True)
    (tmp_path / "_execution" / "execution.yaml").write_text(
        yaml.safe_dump({"image_revision": "reg/scenario@sha256:" + "a" * 64}))
    (tmp_path / "_config").mkdir(parents=True)
    (tmp_path / "_config" / "p.vast").write_text(yaml.safe_dump(
        {"version": 6, "execution": {"containers": {"scenario": {"image": "reg/scenario:1"},
                                                    "simulation": {"image": "reg/sim:1"}}}}))
    with pytest.raises(scene_cache.SceneUnavailable) as err:
        scene_cache.world_identity(tmp_path, {"world": "w.yaml", "overrides": {}})
    assert "reg/sim:1" in str(err.value)


def _scene_identity_for(tmp_path, world, archive=True):
    from unittest.mock import patch

    from robovast.service import scene_cache
    if archive:
        f = tmp_path / "_config" / "files" / "depot_nav2.yaml"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("extends: roqsim_scenes:depot\n")
    # The frozen `.vast` names the simulator, which is who says how to rebuild the geometry.
    vast = tmp_path / "_config" / "p.vast"
    vast.parent.mkdir(parents=True, exist_ok=True)
    vast.write_text("version: 6\nexecution:\n  mode: ros2\n  containers:\n    simulation:\n"
                    "      backend: roqsim\n      config: roqsim_scenes:depot\n")
    meta = {"image_revisions": {"simulation": "reg/sim@sha256:" + "b" * 64}}
    with patch("robovast.common.campaign_data.read_execution_metadata", lambda _p: meta):
        return scene_cache.world_identity(str(tmp_path), {"world": world, "overrides": {}})


@pytest.mark.requires_simulator
def test_a_campaign_owned_world_is_staged_into_the_build_container(tmp_path):
    """A world declared as a path in the .vast is a run_file, mounted at /config only for
    the job. Passing that recorded path to a fresh container asks it to read something
    that was never there -- the exporter started and failed on a missing file.

    The whole ``_config/`` tree travels and is mounted back at ``/config``, because a world
    is not one file: it names its meshes and colliders by the path the job had them at. So
    the command keeps the RECORDED path and the world resolves its own references."""
    from robovast.service import scene_cache

    ident = _scene_identity_for(tmp_path, "/config/files/depot_nav2.yaml")
    assert ident["world_file"].endswith("_config/files/depot_nav2.yaml")
    entry = scene_cache._generate_entry(ident, "k", 1024)
    assert entry["shell"]["inputs"] == [ident["config_root"]]
    assert entry["shell"]["mount_at"] == {ident["config_root"]: "/config"}
    assert "--world /config/files/depot_nav2.yaml" in entry["shell"]["command"]


def test_a_campaign_worlds_neighbours_travel_with_it(tmp_path):
    """The mesh a world names is not the world file, and staging the YAML alone failed on it.

    Mounting the tree rather than enumerating the world's dependencies is deliberate: an
    enumeration that under-reports (roqsim's own walks `extends` and MJCF assets, never a
    plugin's config values) reproduces exactly the failure this replaced."""
    mesh = tmp_path / "_config" / "environments" / "hex" / "3d-mesh" / "hex.stl"
    mesh.parent.mkdir(parents=True)
    mesh.write_bytes(b"solid\n")
    ident = _scene_identity_for(tmp_path, "/config/files/depot_nav2.yaml")
    staged = Path(ident["config_root"])
    assert (staged / "environments" / "hex" / "3d-mesh" / "hex.stl").is_file()
    assert (staged / "files" / "depot_nav2.yaml").is_file()


def test_the_cache_key_covers_a_referenced_file_not_just_the_world(tmp_path):
    """A changed mesh is different geometry, and the world YAML naming it does not change."""
    from robovast.service import scene_cache

    def _with_mesh(root, payload):
        mesh = root / "_config" / "environments" / "hex" / "3d-mesh" / "hex.stl"
        mesh.parent.mkdir(parents=True)
        mesh.write_bytes(payload)
        return _scene_identity_for(root, "/config/files/depot_nav2.yaml")

    a = _with_mesh(tmp_path / "a", b"solid one\n")
    b = _with_mesh(tmp_path / "b", b"solid two\n")
    assert a["world"] == b["world"]
    assert scene_cache.cache_key(a) != scene_cache.cache_key(b)


@pytest.mark.requires_simulator
def test_a_packaged_world_keeps_its_recorded_path(tmp_path):
    """It lives in the image, so the path is valid there by construction -- nothing to stage."""
    from robovast.service import scene_cache

    ident = _scene_identity_for(tmp_path, "roqsim_scenes:depot", archive=False)
    assert "world_file" not in ident
    entry = scene_cache._generate_entry(ident, "k", 1024)
    assert "inputs" not in entry["shell"]
    assert "mount_at" not in entry["shell"]
    assert "roqsim_scenes:depot" in entry["shell"]["command"]


def test_the_cache_key_covers_a_campaign_worlds_contents(tmp_path):
    """The image digest says nothing about a campaign file's bytes, so two campaigns whose
    worlds share a path would otherwise serve each other's geometry."""
    from robovast.service import scene_cache

    a = _scene_identity_for(tmp_path / "a", "/config/files/depot_nav2.yaml")
    b_dir = tmp_path / "b"
    (b_dir / "_config" / "files").mkdir(parents=True)
    (b_dir / "_config" / "files" / "depot_nav2.yaml").write_text("extends: roqsim_scenes:other\n")
    b = _scene_identity_for(b_dir, "/config/files/depot_nav2.yaml", archive=False)
    assert a["world"] == b["world"]
    assert scene_cache.cache_key(a) != scene_cache.cache_key(b)


def test_a_missing_archived_world_says_so(tmp_path):
    """Rather than failing later inside the container with a path nobody can place."""
    from robovast.service import scene_cache

    with pytest.raises(scene_cache.SceneUnavailable, match="not archived with the campaign"):
        _scene_identity_for(tmp_path, "/config/files/depot_nav2.yaml", archive=False)


# -- get_job_state on the cluster: same read, a pod instead of a container ------------------------


def _container(name, restart_policy=None):
    return types.SimpleNamespace(name=name, restart_policy=restart_policy)


class _Pod:
    """A pod as the API returns one: workload sidecars live in ``init_containers``.

    The default is the real single-container shape -- the scenario container, named ``robovast``
    by the manifest. *sidecars* are declared the way the backend declares them, as **native**
    sidecars (``restartPolicy: Always`` on an init container), because that is what made every
    role but ``scenario`` unreachable while the pod was visibly running three containers.
    """

    def __init__(self, name, container="robovast", sidecars=(), init=()):
        self.metadata = types.SimpleNamespace(name=name)
        self.spec = types.SimpleNamespace(
            containers=[_container(container)],
            init_containers=[_container(n, "Always") for n in sidecars]
            + [_container(n) for n in init])


#: The campaign shape that goes with a single-container pod: nothing declares a simulator of its
#: own, so the simulation role is backed by the scenario container. Stated rather than left as an
#: empty block, because the execution block and the pod have to describe the same campaign -- an
#: empty one beside a ROS-shape pod is a fixture that cannot exist, and it is what let the wrong
#: container look right.
_FOLDED_EXECUTION = {"mode": "base", "containers": {
    "scenario": {"image": "scen:1"},
    # Declared, with no image of its own: that is what "stepped in-process" looks like in a config,
    # and it is what makes the plan fold the role onto the scenario container. An ABSENT simulation
    # block would be a campaign with no simulator at all -- a third case, and one where a health
    # command could not exist, so a fixture that omitted the block while forcing one described a
    # campaign that cannot be.
    "simulation": {"backend": "roqsim", "config": "w.yaml"}}}

#: The ROS shape: the simulator is a sidecar with its own image and its own container.
_ROS_EXECUTION = {"mode": "ros2", "containers": {"simulation": {"image": "sim:1", "backend": "roqsim",
                                                               "config": "w.yaml"},
                                                 "sut": {"image": "sut:1"}}}


def _cluster_job_state(cs, monkeypatch, *, pods, exec_result=(0, "{}", "", False),
                       execution=None, live_run=None):
    """*live_run* is the run key the pod's own ``find`` answers with, and the run whose
    behaviour log then sits in the campaign directory -- where the file agent delivers it, and
    where the scenario's tree is folded from. ``None`` is a job that has written no run yet."""
    # A running job, as the real precondition returns one: the state read reports the status it was
    # checked against rather than asserting "running" a second time.
    monkeypatch.setattr(cs, "_require_running_job",
                        lambda cid, job: types.SimpleNamespace(job_name=job, status="running"))
    monkeypatch.setattr(cs, "_campaign_execution",
                        lambda cid: execution if execution is not None else _FOLDED_EXECUTION)
    # Stubbed because the real one READS THE JOB over the API: unmocked it reached a live cluster
    # and every test in this file waited out a connect timeout. Its own resolution is asserted
    # separately, in test_the_job_output_dir_is_read_off_the_job.
    monkeypatch.setattr(cs, "_job_artifact_dir", lambda job: "_jobs/batch-0/job-0")
    monkeypatch.setattr("robovast.common.simulators.health_command",
                        lambda execution, *, run_dir, base_dir="": f"tool --json {run_dir}")

    class _Core:
        def list_namespaced_pod(self, namespace, label_selector=""):
            self.selector = label_selector
            return types.SimpleNamespace(items=pods)

    core = _Core()
    monkeypatch.setattr(cs, "_k8s", lambda: core)

    if live_run is not None:
        campaign_root = Path(tempfile.mkdtemp()) / "results"
        monkeypatch.setattr(cs, "_campaigns_root", lambda: campaign_root)
        config, run_id = live_run.split("/")
        behaviour_log.write_store(campaign_root / "camp-1", {config: [int(run_id)]})
        behaviour_log.write_log(campaign_root / "camp-1" / config / run_id)

    class _Service:
        calls: list = []

        def exec_in(self, target, argv, limit_s):
            _Service.calls.append((target, argv))
            # Matched on the joined argv: every read runs through a shell that sources the run's
            # ROS overlay first, so the command is inside one element rather than being them.
            joined = " ".join(argv)
            if "-regex" in joined and live_run is not None:
                return (0, live_run + "\n", "", False)
            if "resource_usage_" in joined:
                return (0, "", "", False)
            return exec_result

    _Service.calls = []
    monkeypatch.setattr(cs, "_exec_runner", lambda: _Service())
    return core, _Service


def test_cluster_get_job_state_execs_into_the_job_s_pod(cs, monkeypatch):
    """The exec target is the Job's pod. ``/out`` is *this pod's* emptyDir, so naming it is
    exact even though its ``job_name`` is not a run key. The scenario's tree is the one
    exception: it is folded from the run's log in the campaign directory, not read in the pod,
    and the run it is of is the one the pod named."""
    core, runner = _cluster_job_state(
        cs, monkeypatch, pods=[_Pod("scenario-abc-x9")], live_run="cfgA/1",
        exec_result=(0, '{"findings": [], "state": {"sim_ts": 4.0}}', "", False))

    state = cs.get_job_state("camp-1", "scenario-abc")

    assert state.simulator == {"findings": [], "state": {"sim_ts": 4.0}}
    assert state.run == "cfgA/1"
    assert state.scenario["running"]["name"] == "drive_to"
    assert state.scenario["log"].endswith("/camp-1/cfgA/1/behaviors.jsonl")
    assert not [c for c in runner.calls if "tree_state" in " ".join(c[1])]
    target, argv = [c for c in runner.calls if "tool --json" in " ".join(c[1])][0]
    # The job dir: this is a live run, and that is where its simulator's records are.
    # The container comes from the pod, not from a constant repeated here. This campaign steps its
    # simulator in-process, so the simulation role IS this container.
    assert target == ("scenario-abc-x9", "robovast")
    # The command, run in the environment the run's own processes have -- a bare argv would not
    # find anything the run built into its overlay.
    script = " ".join(argv)
    assert "/ws/install/setup.bash" in script
    assert script.endswith("tool --json /out/_jobs/batch-0/job-0")
    assert "job-name=scenario-abc" in core.selector


def test_cluster_get_job_state_says_when_there_is_no_pod_yet(cs, monkeypatch):
    """Between scheduling and running there is a Job but no pod. That is a reason, not an empty
    answer -- and not a crash on ``items[0]``."""
    _cluster_job_state(cs, monkeypatch, pods=[])

    state = cs.get_job_state("camp-1", "scenario-abc")

    assert state.simulator is None
    assert any("no pod for job" in line for line in state.unavailable)


def test_the_scenario_tree_is_read_even_when_the_simulator_cannot_report(cs, monkeypatch):
    """The two readers are independent on purpose: a scenario's tree is there whatever the
    simulator is, and the stuck action is the more useful half. Coupling them would let the
    absence of one hide the other."""
    _cluster_job_state(cs, monkeypatch, pods=[_Pod("scenario-abc-x9")], live_run="cfgA/1")
    monkeypatch.setattr("robovast.common.simulators.health_command",
                        lambda execution, *, run_dir, base_dir="": None)

    state = cs.get_job_state("camp-1", "scenario-abc")

    assert state.simulator is None
    assert state.scenario["running"]["name"] == "drive_to"
    assert any("does not report its own state" in line for line in state.unavailable)


def test_a_job_whose_run_is_not_known_yet_has_no_tree_to_fold(cs, monkeypatch):
    """A Job between starting and its first record names no run, so there is no log to fold;
    said as such rather than searched for, because the run the reader would find under ``/out``
    on its own is a guess the service cannot check."""
    _cluster_job_state(cs, monkeypatch, pods=[_Pod("scenario-abc-x9")])

    state = cs.get_job_state("camp-1", "scenario-abc")

    assert state.run is None and state.scenario is None
    assert any("could not be resolved" in line for line in state.unavailable)


def test_the_health_pull_resolves_every_running_pod_on_the_cluster(cs, monkeypatch):
    """Each running Job is asked in *its own* pod, over the Kubernetes exec API, and a job
    with no pod yet is skipped rather than crashing the sweep.

    The inherited resolver walks ``list_jobs`` and asks the ClusterService hook for each
    running one, so this pins the composition rather than a second implementation of it.
    """
    # A ROS-shape pod: the simulator is a sidecar with its own image, which is the container the
    # health read has to reach -- `roqsim health` sent to the scenario container names a tool that
    # container does not have.
    pod = _Pod("scenario-abc-x9", sidecars=("simulation", "sut"))
    core, runner = _cluster_job_state(cs, monkeypatch, pods=[pod], execution=_ROS_EXECUTION)
    monkeypatch.setattr(cs, "list_jobs", lambda cid: types.SimpleNamespace(jobs=[
        # ``kind`` as the service always sets it: the sweep skips what is not a run.
        types.SimpleNamespace(job_name="scenario-abc", status="running", kind="run"),
        types.SimpleNamespace(job_name="scenario-def", status="completed", kind="run"),
    ]))

    targets = cs._health_targets("camp-1")

    # ``/out`` and not a run key: this pod's own emptyDir holds only this job's run, and the Job's
    # name is not a run key.
    # Both paths, because the simulator's records and the job's artifacts are different subtrees:
    # the job dir first (where a LIVE run's clock record is), the run dir after it. The run dir is
    # the resolved one -- the fixture's runner returns no run key, so it falls back to the job root,
    # which is the documented behaviour for a job that has not written a record yet.
    assert targets == [("scenario-abc", "/out/_jobs/batch-0/job-0", "/out")]
    assert "job-name=scenario-abc" in core.selector
    # One exec, and only the run-dir resolution: the reads themselves are the caller's to make, so
    # a target that is merely being ENUMERATED must not trigger a health command.
    assert [" ".join(c[1]) for c in runner.calls if "tool --json" in " ".join(c[1])] == []
    # And the read itself lands in the simulator's own container, from the pod rather than from a
    # name built here: that is the difference between asking the simulator and asking a container
    # that has never heard of it.
    assert cs._job_state_target("camp-1", "scenario-abc", "simulation")[0] == (
        "scenario-abc-x9", "simulation")


def test_the_health_pull_asks_a_calibration_probe_as_it_asks_a_run(cs, monkeypatch):
    """The probe runs one real configuration in the job shape so that its measurement stands for
    the jobs' -- and the health read is part of that shape: a process the service starts inside
    the simulator's container, charged to the simulator's memory. A probe spared it is sized
    without it, and every job then meets, over a limit with no room for it, the one cost the probe
    never saw."""
    pod = _Pod("scenario-abc-x9", sidecars=("simulation", "sut"))
    _cluster_job_state(cs, monkeypatch, pods=[pod], execution=_ROS_EXECUTION)
    monkeypatch.setattr(cs, "list_jobs", lambda cid: types.SimpleNamespace(jobs=[
        types.SimpleNamespace(job_name="scenario-abc", status="running",
                              kind=JobKind.CALIBRATION),
    ]))

    assert [name for name, *_ in cs._health_targets("camp-1")] == ["scenario-abc"]


def test_a_role_in_a_native_sidecar_is_found(cs, monkeypatch):
    """The simulator and the system under test are ``initContainers`` with ``restartPolicy:
    Always`` -- workload containers that Kubernetes files under a field whose name says the
    opposite. Reading ``spec.containers`` alone refused every role but ``scenario`` on a pod that
    was running three of them, and quoted the one-name list as its evidence."""
    _cluster_job_state(cs, monkeypatch, pods=[_Pod("scenario-abc-x9",
                                                  sidecars=("simulation", "sut"))],
                       execution=_ROS_EXECUTION)

    assert cs._job_pod_target("c", "j", "simulation") == ("scenario-abc-x9", "simulation")
    assert cs._job_pod_target("c", "j", "sut") == ("scenario-abc-x9", "sut")
    # Still by position, not by name: the manifest owns what the scenario's container is called.
    assert cs._job_pod_target("c", "j", "scenario") == ("scenario-abc-x9", "robovast")


def test_a_one_shot_init_container_is_not_a_role(cs, monkeypatch):
    """``s3-init`` populates ``/config`` and exits. Counting it as a workload container would
    offer a caller a container that is gone by the time anything could be run in it."""
    _cluster_job_state(cs, monkeypatch,
                       pods=[_Pod("scenario-abc-x9", sidecars=("sut",), init=("s3-init",))],
                       execution=_ROS_EXECUTION)

    # Reachable roles resolve past it, and it is absent from the list the refusal offers: that
    # list is the caller's next move, so naming a container that has already exited would send
    # them to run something in it.
    assert cs._job_pod_target("c", "j", "sut") == ("scenario-abc-x9", "sut")
    with pytest.raises(KeyError) as raised:
        cs._job_pod_target("c", "j", "simulation")
    assert "s3-init" not in str(raised.value)
    assert "robovast, sut" in str(raised.value)


def test_a_jobs_run_is_located(cs, monkeypatch):
    """A Job is one run, but its NAME is not the run key -- so the run has to be resolved here
    rather than left to the readers. Both of them can search a couple of levels
    down for their own file, which is two other components modelling this layout, answering with
    a heuristic ("the newest below here") where the service has the fact. Worse, searching around
    a directory MASKS a wrong one: pointed at ``_jobs/batch-0`` a reader looks past it and then
    blames ``--bt-log``."""
    _core, runner = _cluster_job_state(cs, monkeypatch, pods=[_Pod("scenario-abc-x9")])
    monkeypatch.setattr(cs, "_exec_runner", lambda: types.SimpleNamespace(
        exec_in=lambda target, argv, limit_s, env=None: (0, "cfga/0\n", "", False)))

    assert cs._job_live_run("c", "scenario-abc", ("p", "c"), "/out") == ("/out/cfga/0", "cfga/0")
    del runner


def test_the_live_run_search_looks_for_run_dirs_and_not_for_the_newest_file(cs, monkeypatch):
    """A campaign root holds ``_jobs/`` beside its runs, and the job artifacts under it are the
    files most recently written -- so taking the newest file anywhere named the run ``_jobs/batch-0``
    and pointed every reader at a subtree with no run in it. The search is for the run LAYOUT."""
    seen = {}
    _cluster_job_state(cs, monkeypatch, pods=[_Pod("scenario-abc-x9")])
    monkeypatch.setattr(cs, "_exec_runner", lambda: types.SimpleNamespace(
        exec_in=lambda target, argv, limit_s, env=None: (
            seen.setdefault("argv", " ".join(argv)), "", "", False) and (0, "", "", False)))

    cs._job_live_run("c", "scenario-abc", ("p", "c"), "/out")

    script = seen["argv"]
    assert "-type d" in script, "a run dir is a directory; the newest FILE is a job artifact"
    assert "[0-9]+" in script, "a run number is digits -- that shape is what excludes _jobs"
    assert "-maxdepth 2" in script


def test_the_job_output_dir_is_read_off_the_job(cs, monkeypatch):
    """Where the resource samples and logs are, which is NOT where the runs are: the backend stamps
    ``OUTPUT_DIR=/out/_jobs/<batch>/job-<idx>`` on the pod, so this is a read rather than a guess --
    and no exec at all."""
    _cluster_job_state(cs, monkeypatch, pods=[_Pod("scenario-abc-x9")])
    monkeypatch.setattr(cs, "_job_artifact_dir", lambda job: "_jobs/batch-0/job-0")

    assert cs._job_output_dir("c", "scenario-abc", "/out") == "/out/_jobs/batch-0/job-0"


def test_the_pod_outvotes_the_config_about_which_containers_exist(cs, monkeypatch):
    """A pod that HAS a container called ``simulation`` is not something an unreadable -- or simply
    simulator-less -- config can outvote. Asking the plan first resolves the role to the
    scenario container for such a config, and the health read then enters a container with no
    simulator in it, confidently and with nothing saying so."""
    _cluster_job_state(cs, monkeypatch,
                       pods=[_Pod("scenario-abc-x9", sidecars=("simulation", "sut"))],
                       execution={"mode": "base", "containers": {}})

    assert cs._job_pod_target("c", "j", "simulation") == ("scenario-abc-x9", "simulation")


def test_a_stepped_simulator_still_resolves_through_the_plan(cs, monkeypatch):
    """The case the pod cannot answer: a simulator stepped in-process has no container of its own,
    so there is no name to find and only the plan knows the role is backed by the scenario's."""
    _cluster_job_state(cs, monkeypatch, pods=[_Pod("scenario-abc-x9")],
                       execution={"mode": "base", "containers": {
                           "scenario": {"image": "s:1"},
                           "simulation": {"backend": "roqsim", "config": "w.yaml"}}})

    assert cs._job_pod_target("c", "j", "simulation") == ("scenario-abc-x9", "robovast")


def test_a_job_that_has_written_nothing_keeps_the_job_root(cs, monkeypatch):
    """A run between starting and its first record is normal. The readers' own "nothing here yet"
    is a better answer than a failure from the step that was only trying to be more precise."""
    _cluster_job_state(cs, monkeypatch, pods=[_Pod("scenario-abc-x9")])
    monkeypatch.setattr(cs, "_exec_runner", lambda: types.SimpleNamespace(
        exec_in=lambda target, argv, limit_s, env=None: (0, "", "", False)))

    assert cs._job_live_run("c", "scenario-abc", ("p", "c"), "/out") == ("/out", None)


def test_a_job_between_scheduling_and_running_is_skipped_not_fatal(cs, monkeypatch):
    """Normal on a cluster: a Job exists before its pod does. One unanswerable job must not cost
    the campaign's other jobs their check."""
    _cluster_job_state(cs, monkeypatch, pods=[])
    monkeypatch.setattr(cs, "list_jobs", lambda cid: types.SimpleNamespace(jobs=[
        types.SimpleNamespace(job_name="scenario-abc", status="running", kind="run")]))

    assert cs._health_targets("camp-1") == []


def test_results_dir_decides_where_driven_campaigns_live(tmp_path):
    """``vast serve --results-dir`` decides ClusterService's results root.

    The results volume is where a cluster campaign lives: its pods deliver their runs into
    it, per-run extraction reads it through a path, and postprocessing runs against it. Without the flag that root is ``local_results_root``'s
    ``<workspaces_root>/../results``, which in the deployed pod is one directory outside
    the only mount it has: every restart would discard it, and since resume reads it before
    the port is bound, a restart with live campaigns could never finish.
    """
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tempfile.mkdtemp()))
    svc = ClusterService(namespace="ns1", cluster_config_name="rke2",
                         cluster_config_kwargs={"foo": "bar"}, store=store,
                         reap_on_start=False, results_dir=str(tmp_path / "mounted"))

    assert svc._campaigns_root() == tmp_path / "mounted"
    assert svc.campaign_dir("camp-a") == tmp_path / "mounted" / "camp-a"


def test_without_results_dir_the_service_keeps_its_default(cs):
    """No flag, no surprise: the shared ``local_results_root`` precedence still decides."""
    from robovast.common.results_root import local_results_root
    assert cs._campaigns_root() == local_results_root(cs.store.registry.root)


# --- per-job live usage ------------------------------------------------------------------
#
# What a running job is consuming, against what it was given, on the campaign's job listing.
# The failures worth pinning are all the same shape: a number that is WRONG but plausible --
# a stale sample on a finished job, a ceiling summed over the wrong containers, an absence
# that reads as an idle cluster.


def _pod_metrics_item(job_name, containers, window="12s"):
    """One ``PodMetrics``, shaped as metrics-server returns it.

    The ``job-name`` label is the join: the item names the POD, and only its labels say which
    Job that pod belongs to. Written here as the real thing writes it, both keys included,
    because the older ``job-name`` is all an older cluster sets.
    """
    return {"metadata": {"name": f"{job_name}-pod",
                         "labels": {"batch.kubernetes.io/job-name": job_name,
                                    "job-name": job_name, "jobgroup": "scenario-runs"}},
            "window": window,
            "containers": [{"name": name, "usage": {"cpu": cpu, "memory": memory}}
                           for name, (cpu, memory) in containers.items()]}


def _sized(job, containers):
    """Give a Job fake the container spec a real one carries, as ``(name, sidecar, req, lim)``."""
    job.spec.template.spec = types.SimpleNamespace(
        containers=[types.SimpleNamespace(name=n, restart_policy=None,
                                          resources={"requests": req, "limits": lim})
                    for n, side, req, lim in containers if not side],
        init_containers=[types.SimpleNamespace(name=n, restart_policy="Always",
                                               resources={"requests": req, "limits": lim})
                         for n, side, req, lim in containers if side])
    return job


def _one_running_job(cs, monkeypatch, *, job=None, pods=None, phase="Running", **fake):
    """A campaign of exactly one job, with the metrics fake installed. Returns the fake."""
    job = job if job is not None else _job("j-1", active=1)

    class _Batch:
        def list_namespaced_job(self, namespace, label_selector):
            return types.SimpleNamespace(items=[job])

    monkeypatch.setattr(cs, "_k8s_batch", lambda: _Batch())
    monkeypatch.setattr(cs, "_k8s", lambda: _CoreWithPods([_job_pod("j-1", phase=phase)]))
    return _stub_metrics(cs, monkeypatch, pods=pods, **fake)


def test_list_jobs_reports_what_a_running_job_uses_against_both_figures_it_was_given(
        cs, monkeypatch):
    """Measured, requested and limited -- all three, and the sidecars counted in the last two.

    The request and the limit answer different questions ("was the reservation right?" against
    "is this near being throttled?") and differ in practice, so neither stands in for the
    other. And the ceiling has to include the native sidecars: summing ``spec.containers``
    alone would report 2 cores for a pod that holds 6.5.
    """
    job = _sized(_job("j-1", active=1), [
        ("robovast", False, {"cpu": "1.429", "memory": "1Gi"}, {"cpu": "2", "memory": "1Gi"}),
        ("sut", True, {"cpu": "2.495", "memory": "2Gi"}, {"cpu": "2.495", "memory": "2Gi"}),
        ("simulation", True, {"cpu": "425m", "memory": "1Gi"}, {"cpu": "2", "memory": "1Gi"}),
    ])
    _one_running_job(cs, monkeypatch, job=job, pods=[_pod_metrics_item("j-1", {
        "robovast": ("194517872n", "219580Ki"),
        "sut": ("537237547n", "307968Ki"),
        "simulation": ("212990095n", "130856Ki")})])

    usage = cs.list_jobs("camp-2026-07-17-120000").jobs[0].usage

    assert usage.cpu_cores == pytest.approx(0.944745514)
    assert usage.cpu_request == pytest.approx(4.349)
    assert usage.cpu_limit == pytest.approx(6.495)
    assert usage.memory_bytes == (219580 + 307968 + 130856) * 1024
    assert usage.memory_limit_bytes == 4 * 1024 ** 3


def test_list_jobs_withholds_usage_from_a_job_that_is_no_longer_running(cs, monkeypatch):
    """A sample outlives the pod that produced it.

    metrics-server keeps serving a reading for a pod that has just finished, so a completed row
    carrying one shows a job apparently still burning cores. The status is the gate, not the
    presence of a sample -- which is why the fake here HAS one for this job.
    """
    _one_running_job(cs, monkeypatch, phase="Succeeded",
                     pods=[_pod_metrics_item("j-1", {"robovast": ("1", "1Gi")})])

    job = cs.list_jobs("camp-2026-07-17-120000").jobs[0]

    assert job.status == "completed"
    assert job.usage is None


def test_list_jobs_reports_when_a_job_started_even_before_it_runs(cs, monkeypatch):
    """Age comes from the JOB, so a job that has not started executing still has one.

    That is the point of taking it here rather than from the pod's containers: "how long has
    this been stuck?" is the question a pending or blocked row raises, and a start time that
    only existed once execution began could not answer it.
    """
    job = _job("j-1", active=1)
    job.status.start_time = datetime.datetime(2026, 9, 3, 8, 40, 45,
                                              tzinfo=datetime.timezone.utc)
    _one_running_job(cs, monkeypatch, job=job, phase="Pending")

    listed = cs.list_jobs("camp-2026-07-17-120000").jobs[0]

    assert listed.status == "pending"
    assert listed.started_at == job.status.start_time.timestamp()


def test_a_job_the_cluster_gave_no_start_time_reports_none(cs, monkeypatch):
    """Absent, not zero: an epoch-zero start renders as fifty-six years of runtime."""
    _one_running_job(cs, monkeypatch)

    assert cs.list_jobs("camp-2026-07-17-120000").jobs[0].started_at is None


def test_the_denominator_covers_only_the_containers_that_were_measured(cs, monkeypatch):
    """Numerator and denominator must describe the same containers.

    A metrics-server that reported one container of three, divided by the whole pod's ceiling,
    is a ratio between two different things -- and it reads as a comfortably idle job.
    """
    job = _sized(_job("j-1", active=1), [
        ("robovast", False, {"cpu": "1", "memory": "1Gi"}, {"cpu": "2", "memory": "1Gi"}),
        ("sut", True, {"cpu": "4", "memory": "8Gi"}, {"cpu": "4", "memory": "8Gi"}),
    ])
    _one_running_job(cs, monkeypatch, job=job,
                     pods=[_pod_metrics_item("j-1", {"robovast": ("1", "1Gi")})])

    usage = cs.list_jobs("camp-2026-07-17-120000").jobs[0].usage

    assert usage.cpu_limit == pytest.approx(2), "the unmeasured sidecar is not in the ceiling"
    assert usage.memory_limit_bytes == 1024 ** 3


def test_a_job_with_no_measurement_reports_no_usage_rather_than_zeros(cs, monkeypatch):
    """Zero is a claim about an idle job; this is the absence of a reading."""
    _one_running_job(cs, monkeypatch, pods=[_pod_metrics_item("someone-elses-job",
                                                             {"robovast": ("1", "1Gi")})])

    assert cs.list_jobs("camp-2026-07-17-120000").jobs[0].usage is None


def test_one_metrics_read_serves_repeated_listings(cs, monkeypatch):
    """The job list is polled every couple of seconds; the sample changes far more slowly.

    Without the snapshot every poll of every open campaign card would be another round trip to
    be handed the same numbers back.
    """
    api = _one_running_job(cs, monkeypatch,
                           pods=[_pod_metrics_item("j-1", {"c": ("1", "1Gi")}, window="12s")])

    for _ in range(3):
        assert cs.list_jobs("camp-2026-07-17-120000").jobs[0].usage is not None

    assert api.pod_calls == 1


def test_the_snapshot_is_held_for_the_window_the_cluster_states(cs, monkeypatch):
    """The TTL is read from the data, not chosen here.

    Each sample says which window it covers, and that is how often the cluster can have a new
    one. A cluster reporting a long window must not be polled at some rate we picked, and the
    SHORTEST window wins so no sample is served past its own life.
    """
    api = _one_running_job(cs, monkeypatch, pods=[
        _pod_metrics_item("j-1", {"c": ("1", "1Gi")}, window="30s"),
        _pod_metrics_item("j-2", {"c": ("1", "1Gi")}, window="11s")])

    cs.list_jobs("camp-2026-07-17-120000")
    held_until, _ = cs._pod_metrics_snapshot
    remaining = held_until - time.monotonic()

    assert 10 < remaining <= 11, "the shortest window sets the life of the snapshot"
    assert api.pod_calls == 1


def test_an_unreadable_window_falls_back_rather_than_reading_every_poll(cs, monkeypatch):
    """A cluster that states no window still gets a sane cadence.

    Zero would mean re-reading on every poll of every campaign card, which is the cost this
    whole snapshot exists to avoid.
    """
    _one_running_job(cs, monkeypatch,
                     pods=[_pod_metrics_item("j-1", {"c": ("1", "1Gi")}, window="soon")])

    cs.list_jobs("camp-2026-07-17-120000")
    held_until, _ = cs._pod_metrics_snapshot

    assert held_until - time.monotonic() > cs._POD_METRICS_TTL_MIN - 1


def test_a_cluster_without_metrics_server_says_so_and_stops_asking(cs, monkeypatch):
    """Rows carry no numbers, and the response says why.

    Silence alone is indistinguishable from an idle cluster, which is the reading this field
    exists to prevent. And the failure is memoised: a 404 here means an add-on nobody
    installed, so asking again every poll spends a round trip to learn the same thing.
    """
    api = _one_running_job(cs, monkeypatch, pod_error=_api_exception(404),
                           pods=[_pod_metrics_item("j-1", {"c": ("1", "1Gi")})])

    first = cs.list_jobs("camp-2026-07-17-120000")
    second = cs.list_jobs("camp-2026-07-17-120000")

    assert first.jobs[0].usage is None
    assert "metrics-server" in first.metrics_unavailable
    assert second.metrics_unavailable == first.metrics_unavailable
    assert api.pod_calls == 1, "a missing add-on is remembered, not re-probed every poll"


def test_rbac_that_predates_the_grant_names_the_command_that_fixes_it(cs, monkeypatch):
    """A 403 is a deployment that was never upgraded, not a broken cluster.

    The reason names ``pods`` specifically: the two metrics grants are given independently, and
    a message naming the wrong sub-resource sends a reader to reconcile something already there.
    """
    _one_running_job(cs, monkeypatch, pod_error=_api_exception(403))

    reason = cs.list_jobs("camp-2026-07-17-120000").metrics_unavailable

    assert "metrics.k8s.io/pods" in reason
    assert "vast service upgrade" in reason


def test_a_transient_failure_is_reported_but_not_remembered(cs, monkeypatch):
    """A timeout fixes itself; memoising it would blank the numbers for ten minutes.

    Only settled facts about the cluster -- an absent add-on, an unreconciled role -- are worth
    remembering. Anything else is asked again.
    """
    api = _one_running_job(cs, monkeypatch, pod_error=TimeoutError("aggregated API is slow"))

    first = cs.list_jobs("camp-2026-07-17-120000")
    cs.list_jobs("camp-2026-07-17-120000")

    assert first.jobs[0].usage is None
    assert "could not be read" in first.metrics_unavailable
    assert api.pod_calls == 2, "a transient failure must not stop the service asking"


def test_a_pods_403_does_not_blind_the_capacity_meter(cs, monkeypatch):
    """The two metrics reads memoise separately, because the two grants are separate.

    A shared memo would take the Admin chart's measured fill down with a missing per-job grant,
    and explain it with a reason naming the wrong sub-resource.
    """
    api = _stub_metrics(cs, monkeypatch, {"n1": {"cpu": "1", "memory": "1Gi"}},
                        pod_error=_api_exception(403))
    monkeypatch.setattr(cs, "_k8s_batch", lambda: _UsageBatch([]))
    monkeypatch.setattr(
        cs, "_k8s", lambda: _UsageCore([_usage_node("n1", "4", "8Gi")], [], []))

    assert cs._pod_metrics()[1] is not None
    usage = cs.resource_usage()

    assert usage.cpu_measured == pytest.approx(1)
    assert usage.metrics_unavailable is None
    assert api.calls == 1


def test_list_jobs_says_which_node_a_job_landed_on(cs, monkeypatch):
    """The Jobs list carries placement, so a campaign skewed onto one machine is visible.

    It costs nothing: the pod list the classifier already makes is the only object that
    knows, and a job's own row is where the answer is worth having.
    """
    job = _job("j-1", active=1)

    class _Batch:
        def list_namespaced_job(self, namespace, label_selector):
            return types.SimpleNamespace(items=[job])

    pod = _job_pod("j-1")
    pod.spec = types.SimpleNamespace(node_name="worker-b")
    monkeypatch.setattr(cs, "_k8s_batch", lambda: _Batch())
    monkeypatch.setattr(cs, "_k8s", lambda: _CoreWithPods([pod]))

    assert cs.list_jobs("camp-2026-07-17-120000").jobs[0].node == "worker-b"


def test_a_job_with_no_pod_yet_reports_no_node(cs, monkeypatch):
    """A queued job has no placement, and absent must not render as a blank machine name."""
    _one_running_job(cs, monkeypatch, phase="Pending")

    assert cs.list_jobs("camp-2026-07-17-120000").jobs[0].node is None
