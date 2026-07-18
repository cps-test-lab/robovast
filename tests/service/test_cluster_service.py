# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for ClusterService's launch behaviour (no cluster needed).

The service drives cluster campaigns **in-process** (one worker thread each) over a
KubernetesBackend; there is no per-campaign controller pod any more, so these cover
the launch *hooks* it overrides on LocalTransport plus the aux-pod manifest that
replaced the old controller-pod sidecar.
"""

import tempfile

import pytest

from robovast.execution.cluster_execution.container_runner import (
    AUX_LABEL, DEFAULT_AUX_DEADLINE_SECONDS, aux_pod_name,
    build_aux_pod_manifest)
from robovast.common.variation.container_runner import ContainerSpec
from robovast.service.cluster_service import ClusterService
from robovast.service.interface import CreateCampaignRequest
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore


@pytest.fixture
def cs():
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tempfile.mkdtemp()))
    # reap_on_start=False: the reaper talks to the kube API, which no test has.
    return ClusterService(namespace="ns1", cluster_config_name="rke2",
                          cluster_config_kwargs={"foo": "bar"},
                          image="example/robovast:test", store=store,
                          reap_on_start=False)


def test_version_reports_kubernetes_backend(cs):
    assert cs.version().backend == "kubernetes"


def test_cluster_config_requires_name():
    cs = ClusterService(namespace="ns", cluster_config_name=None,
                        cluster_config_kwargs={}, reap_on_start=False)
    with pytest.raises(ValueError, match="cluster config not configured"):
        cs._cluster_config()


def test_build_backend_threads_kube_context():
    """`vast serve --backend cluster -x local` must reach the K8s backend."""
    cs = ClusterService(namespace="ns2", cluster_config_name="rke2",
                        cluster_config_kwargs={}, reap_on_start=False,
                        kube_context="local")
    backend = cs._build_backend(state=None)
    assert backend.kube_context == "local"
    assert backend.namespace == "ns2"


def test_cleanup_campaign_data_runs_server_side(cs, monkeypatch):
    """Bucket cleanup goes through the service with its own config/context.

    So the CLI/MCP need no object-store credentials — the service passes its
    ``_cluster_config()``, namespace and context straight to ``bucket_ops``.
    """
    calls = {}

    def fake_cleanup(cluster_config, namespace, context, campaign_id,
                     running_campaigns):
        calls.update(namespace=namespace, context=context, campaign_id=campaign_id,
                     running=running_campaigns)
        return 3

    monkeypatch.setattr(
        "robovast.execution.cluster_execution.bucket_ops.cleanup_campaigns",
        fake_cleanup)
    monkeypatch.setattr(cs, "kube_context", "local")

    from robovast.service.interface import CleanupDataRequest
    res = cs.cleanup_campaign_data(CleanupDataRequest(campaign_id="camp-1"))
    assert res.ok and "3" in res.message
    assert calls["namespace"] == "ns1" and calls["context"] == "local"
    assert calls["campaign_id"] == "camp-1"


def test_cleanup_campaign_data_skips_live_campaigns(cs, monkeypatch):
    """A bulk delete must never remove a campaign the service is still driving."""
    from robovast.service.interface import (CampaignSummary,
                                            ListCampaignsResponse)

    monkeypatch.setattr(cs, "list_campaigns", lambda *a, **k: ListCampaignsResponse(
        total=2, campaigns=[
            CampaignSummary(campaign_id="live-1", phase="running"),
            CampaignSummary(campaign_id="done-1", phase="finished")]))
    seen = {}
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.bucket_ops.cleanup_campaigns",
        lambda *a, **kw: seen.update(kw) or 1)

    from robovast.service.interface import CleanupDataRequest
    cs.cleanup_campaign_data(CleanupDataRequest())  # campaign_id=None → bulk
    assert "live-1" in seen["running_campaigns"]
    assert "done-1" not in seen["running_campaigns"]


def test_read_service_config_from_cluster_parses_env(monkeypatch):
    """The cluster Deployment's env is the authoritative config source."""
    import types

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

    class _Apps:
        def read_namespaced_deployment(self, name, namespace):
            return dep

    monkeypatch.setattr(service_deploy, "SERVICE_NAME", "robovast-service")
    import kubernetes
    monkeypatch.setattr(kubernetes.config, "load_kube_config", lambda **k: None)
    monkeypatch.setattr(kubernetes.client, "AppsV1Api", lambda: _Apps())

    name, kwargs = service_deploy.read_service_config_from_cluster("default", "local")
    assert name == "rke2" and kwargs == {"namespace": "ns9"}


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
    assert opts.controller_image == "example/robovast:test"

    off = cs._run_options(CreateCampaignRequest(workspace_id="ws-x", postprocess=False))
    assert off.postprocess is False


def test_postprocessing_is_chained_by_the_builder_not_the_worker(cs):
    """So data.db rides the campaign's existing upload rather than a second one."""
    assert cs._postprocess_in_process() is False


def test_unknown_campaign_status_falls_back_to_object_store(cs, monkeypatch):
    """A campaign this process is not driving is explained from the durable home."""
    monkeypatch.setattr(ClusterService, "_read_outcome", lambda self, cid: None)
    status = cs._status_from_disk("nope-2026-07-17-120000")
    assert status.phase == "unknown"
    assert status.campaign_id == "nope-2026-07-17-120000"


# -- jobs (live) ------------------------------------------------------------

def _job(name, *, succeeded=0, active=0, failed=0, full=None):
    import types
    ann = {"job-name-full": full} if full is not None else {}
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(name=name),
        status=types.SimpleNamespace(succeeded=succeeded, active=active, failed=failed),
        spec=types.SimpleNamespace(template=types.SimpleNamespace(
            metadata=types.SimpleNamespace(annotations=ann))))


def test_list_jobs_classifies_and_counts(cs, monkeypatch):
    """Per-job status mirrors the aggregate counter; counts sum to the total."""
    import types
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

    monkeypatch.setattr(cs, "_k8s_batch", lambda: _Batch())
    resp = cs.list_jobs("camp-2026-07-17-120000")

    assert seen["namespace"] == "ns1"
    assert "jobgroup=scenario-runs" in seen["label_selector"]
    assert "campaign-id=camp-2026-07-17-120000" in seen["label_selector"]
    assert (resp.counts.running, resp.counts.completed, resp.counts.failed,
            resp.counts.pending, resp.counts.total) == (1, 1, 1, 1, 4)
    running = next(j for j in resp.jobs if j.job_name == "j-run")
    assert running.status == "running"
    # campaign prefix stripped from job-name-full for a readable label
    assert running.display_name == "batch-0-job-0"


def _pod(name="pod-1", phase="Running"):
    import types
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(name=name),
        status=types.SimpleNamespace(phase=phase))


def test_get_job_log_streams_running_pod(cs, monkeypatch):
    import types
    seen = {}

    class _Core:
        def list_namespaced_pod(self, namespace, label_selector):
            seen["label_selector"] = label_selector
            return types.SimpleNamespace(items=[_pod(phase="Running")])

        def read_namespaced_pod_log(self, name, namespace, container):
            seen.update(name=name, container=container)
            return "hello world\n"

    monkeypatch.setattr(cs, "_k8s", lambda: _Core())
    chunk = cs.get_job_log("camp-2026-07-17-120000", "j-run")

    assert "job-name=j-run" in seen["label_selector"]
    assert seen["name"] == "pod-1" and seen["container"] == "robovast"
    assert chunk.text == "hello world\n"
    assert chunk.next_offset == len(b"hello world\n")
    assert chunk.eof is False  # pod still running → keep polling
    # byte-offset slicing resumes mid-stream
    assert cs.get_job_log("camp-2026-07-17-120000", "j-run", offset=6).text == "world\n"


def test_get_job_log_terminal_pod_sets_eof(cs, monkeypatch):
    import types

    class _Core:
        def list_namespaced_pod(self, namespace, label_selector):
            return types.SimpleNamespace(items=[_pod(phase="Succeeded")])

        def read_namespaced_pod_log(self, name, namespace, container):
            return "done\n"

    monkeypatch.setattr(cs, "_k8s", lambda: _Core())
    assert cs.get_job_log("camp", "j").eof is True


def test_get_job_log_pending_pod_returns_empty(cs, monkeypatch):
    """A pod still Pending has no log yet — empty, non-terminal, no log call."""
    import types
    called = {"log": False}

    class _Core:
        def list_namespaced_pod(self, namespace, label_selector):
            return types.SimpleNamespace(items=[_pod(phase="Pending")])

        def read_namespaced_pod_log(self, *a, **k):
            called["log"] = True
            return ""

    monkeypatch.setattr(cs, "_k8s", lambda: _Core())
    chunk = cs.get_job_log("camp", "j")
    assert chunk.text == "" and chunk.eof is False
    assert called["log"] is False


def test_get_job_log_missing_pod_raises(cs, monkeypatch):
    import types

    class _Core:
        def list_namespaced_pod(self, namespace, label_selector):
            return types.SimpleNamespace(items=[])

    monkeypatch.setattr(cs, "_k8s", lambda: _Core())
    with pytest.raises(KeyError):
        cs.get_job_log("camp", "gone")


# -- stop (terminates in-flight cluster workloads) --------------------------

def test_stop_flags_state_and_tears_down_this_campaign(cs, monkeypatch):
    """Stop sets the cooperative flag AND deletes only this campaign's workloads.

    The batch wait loop never checks the flag, so without the teardown a batch
    campaign's Stop would do nothing; the teardown is campaign-scoped so other
    queued/running campaigns are untouched.
    """
    import types
    flagged = {}
    state = types.SimpleNamespace(request_stop=lambda: flagged.update(stopped=True))
    cs._campaigns["camp-1"] = types.SimpleNamespace(state=state)

    calls = {}
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.cluster_execution.cleanup_cluster_campaign",
        lambda **kw: calls.update(kw))

    res = cs.stop("camp-1")
    assert res.ok and flagged.get("stopped") is True
    # Scoped to this campaign, in this namespace/context (reuses run-cleanup).
    assert calls == {"namespace": "ns1", "campaign": "camp-1", "context": None}


def test_stop_unknown_campaign_touches_no_cluster(cs, monkeypatch):
    """A campaign not driven here reports not-tracked and deletes nothing."""
    called = {"n": 0}
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.cluster_execution.cleanup_cluster_campaign",
        lambda **kw: called.__setitem__("n", called["n"] + 1))
    res = cs.stop("nope")
    assert res.ok is False and "not running here" in res.message
    assert called["n"] == 0


def test_shutdown_tears_down_every_running_campaign(cs, monkeypatch):
    """Ctrl+C on a cluster ``vast serve`` deletes each running campaign's Jobs.

    Without this, a bare service exit would orphan the in-flight scenario Jobs, which
    would keep consuming cluster resources.
    """
    import types
    calls = []
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.cluster_execution.cleanup_cluster_campaign",
        lambda **kw: calls.append(kw))
    running = [types.SimpleNamespace(campaign_id="camp-a"),
               types.SimpleNamespace(campaign_id="camp-b")]

    cs._terminate_running_campaigns(running)

    assert [c["campaign"] for c in calls] == ["camp-a", "camp-b"]
    assert all(c["namespace"] == "ns1" for c in calls)


def test_shutdown_teardown_is_best_effort(cs, monkeypatch):
    """One campaign's teardown failure never blocks the others (or the process exit)."""
    import types
    seen = []

    def boom(**kw):
        seen.append(kw["campaign"])
        if kw["campaign"] == "camp-a":
            raise RuntimeError("kube api down")

    monkeypatch.setattr(
        "robovast.execution.cluster_execution.cluster_execution.cleanup_cluster_campaign",
        boom)
    running = [types.SimpleNamespace(campaign_id="camp-a"),
               types.SimpleNamespace(campaign_id="camp-b")]

    cs._terminate_running_campaigns(running)  # must not raise
    assert seen == ["camp-a", "camp-b"]  # continued past the failure


# -- driver S3 endpoint (off-cluster host reachability) ---------------------

def test_driver_endpoint_in_cluster_uses_cluster_internal(cs, monkeypatch):
    """In-cluster (robovast:9000 resolves) → no override, no port-forward."""
    opened = []
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.bucket_ops.open_minio_port_forward",
        lambda ns, ctx: opened.append((ns, ctx)) or (object(), 1))
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")

    cfg = cs._cluster_config()
    assert cfg.get_driver_s3_endpoint() == cfg.get_s3_endpoint() == "http://robovast:9000"
    assert opened == []  # never port-forwarded in-cluster


def test_driver_endpoint_off_cluster_embedded_lazily_forwards(cs, monkeypatch):
    """Off-cluster + embedded MinIO → localhost port-forward, opened once, reused."""
    import types

    calls = []
    alive = types.SimpleNamespace(poll=lambda: None)  # a running port-forward
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.bucket_ops.open_minio_port_forward",
        lambda ns, ctx: calls.append((ns, ctx)) or (alive, 18099))
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.setattr(cs, "kube_context", "local")

    cfg = cs._cluster_config()
    assert calls == []  # lazy: building the config opens nothing

    assert cfg.get_driver_s3_endpoint() == "http://localhost:18099"
    # A second config's resolver reuses the one shared forward.
    assert cs._cluster_config().get_driver_s3_endpoint() == "http://localhost:18099"
    assert calls == [("ns1", "local")]  # opened exactly once


def test_shutdown_terminates_port_forward(cs, monkeypatch):
    """Service teardown closes the shared MinIO port-forward."""
    import types

    proc = types.SimpleNamespace(_alive=True)
    proc.poll = lambda: None if proc._alive else 0
    terminated = {}
    proc.terminate = lambda: terminated.update(done=True)
    proc.wait = lambda timeout=None: 0

    monkeypatch.setattr(
        "robovast.execution.cluster_execution.bucket_ops.open_minio_port_forward",
        lambda ns, ctx: (proc, 18099))
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    cs._cluster_config().get_driver_s3_endpoint()  # opens the forward
    assert cs._minio_pf is proc

    cs.shutdown()
    assert terminated.get("done") is True
    assert cs._minio_pf is None


# -- aux pod (replaces the controller-pod sidecar) --------------------------

def _spec():
    return ContainerSpec(image="ghcr.io/secorolab/scenery_builder:1.2",
                         command_prefix=["/entry.sh"],
                         keep_alive_command=["sleep", "infinity"],
                         env={"A": "1"}, run_as_user="1000:1000")


def test_aux_pod_manifest_shape():
    m = build_aux_pod_manifest("nav-2026-07-17-120000", [_spec()], "ns1")
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


def test_aux_pod_is_labelled_per_campaign():
    """So concurrent campaigns' aux pods never collide and cleanup can target one."""
    a = build_aux_pod_manifest("camp-a-2026-07-17-120000", [_spec()], "ns")
    b = build_aux_pod_manifest("camp-b-2026-07-17-120000", [_spec()], "ns")
    assert a["metadata"]["name"] != b["metadata"]["name"]
    assert (a["metadata"]["labels"]["campaign-id"]
            != b["metadata"]["labels"]["campaign-id"])


def test_aux_pod_owner_reference_ties_it_to_the_service_pod():
    """K8s then GCs it if the service is replaced — the sidecar's old guarantee."""
    owner = {"apiVersion": "v1", "kind": "Pod", "name": "robovast-service-x",
             "uid": "abc", "controller": False, "blockOwnerDeletion": False}
    m = build_aux_pod_manifest("c-2026-07-17-120000", [_spec()], "ns", owner_ref=owner)
    assert m["metadata"]["ownerReferences"] == [owner]
