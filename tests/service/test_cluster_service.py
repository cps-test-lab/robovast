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
    import time as _time
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tempfile.mkdtemp()))
    # reap_on_start=False: the reaper talks to the kube API, which no test has.
    svc = ClusterService(namespace="ns1", cluster_config_name="rke2",
                         cluster_config_kwargs={"foo": "bar"}, store=store,
                         reap_on_start=False)
    # Seed the campaign-index cache empty. Campaign discovery reads the object store (see
    # _campaign_index), and off-cluster that opens a kubectl port-forward — which no test
    # has, and which *blocks* rather than failing. Tests that exercise discovery use the
    # ``indexed`` fixture below, which installs a fake store and clears this.
    svc._index_cache = (_time.monotonic(), {})
    return svc


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
        return ["camp-1", "camp-2", "camp-3"]

    monkeypatch.setattr(
        "robovast.execution.cluster_execution.bucket_ops.cleanup_campaigns",
        fake_cleanup)
    monkeypatch.setattr(cs, "kube_context", "local")
    # Retiring the index markers is tested separately; it needs an object store.
    monkeypatch.setattr(cs, "_unmark_removed", lambda removed: None)

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
        lambda *a, **kw: seen.update(kw) or [])
    monkeypatch.setattr(cs, "_unmark_removed", lambda removed: None)

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

    off = cs._run_options(CreateCampaignRequest(workspace_id="ws-x", postprocess=False))
    assert off.postprocess is False


def test_run_options_carry_upload_to_share(cs):
    """The launch toggle flows into RunOptions (default off)."""
    on = cs._run_options(
        CreateCampaignRequest(workspace_id="ws-x", upload_to_share=True))
    assert on.upload_to_share is True
    default = cs._run_options(CreateCampaignRequest(workspace_id="ws-x"))
    assert default.upload_to_share is False


def test_campaign_tar_stream_streams_object_store_excluding_postproc(cs, monkeypatch):
    """The download stream tars objects from the config's add_campaign_members,
    passing the _postproc exclusion — no scratch on the service."""
    import types

    seen = {}

    def _add_members(tar, campaign_id, exclude_prefixes=()):
        seen["campaign_id"] = campaign_id
        seen["exclude"] = set(exclude_prefixes)
        import io
        import tarfile
        info = tarfile.TarInfo(name=f"{campaign_id}/campaign.db")
        info.size = 2
        tar.addfile(info, io.BytesIO(b"db"))

    monkeypatch.setattr(
        cs, "_cluster_config",
        lambda: types.SimpleNamespace(add_campaign_members=_add_members))

    data = b"".join(cs.campaign_tar_stream("camp-2026-01-01-000000"))
    assert data  # a real gzip stream
    assert seen["campaign_id"] == "camp-2026-01-01-000000"
    assert seen["exclude"] == {"_postproc"}


def test_postprocessing_is_chained_by_the_builder_not_the_worker(cs):
    """So data.db rides the campaign's existing upload rather than a second one."""
    assert cs._postprocess_in_process() is False


# -- a build the lane cannot do is a config error, not a crash ---------------

def _project_needing_a_build(tmp_path):
    import types
    (tmp_path / "p.vast").write_text("")
    build = types.SimpleNamespace(tag="sim:v3", base_image=None,
                                  system_packages=[], python_packages=[])
    return (types.SimpleNamespace(config_path=str(tmp_path / "p.vast")),
            types.SimpleNamespace(build=build))


def test_a_build_ref_without_a_registry_fails_the_campaign_without_a_traceback(
        cs, monkeypatch, tmp_path):
    """The worker prints a stack trace for every exception it does not recognize, so a
    plain ValueError here made an unconfigured deployment read as a RoboVAST bug. It is
    bad input with an actionable message: the campaign fails carrying that message
    alone."""
    from robovast.common.errors import CampaignConfigError
    from robovast.execution.cluster_config.base_config import RegistryConfig

    import types
    monkeypatch.setattr(
        cs, "_cluster_config",
        lambda: types.SimpleNamespace(get_registry_config=RegistryConfig))
    monkeypatch.setattr(cs, "_resolve_registry_objects",
                        lambda registry: RegistryConfig(registry_prefix=""))
    project, campaign_config = _project_needing_a_build(tmp_path)

    with pytest.raises(CampaignConfigError, match="no container registry"):
        cs._start_build_image(project, campaign_config)
    assert CampaignConfigError.include_traceback is False


def test_a_broken_build_section_is_a_config_error_too(cs, monkeypatch, tmp_path):
    from robovast.common.errors import CampaignConfigError

    project, campaign_config = _project_needing_a_build(tmp_path)
    campaign_config.build.python_packages = ["./not_here"]

    with pytest.raises(CampaignConfigError, match="build.python_packages"):
        cs._start_build_image(project, campaign_config)


# -- discovery: the object store is the durable home ------------------------
#
# In-pod the disk is scratch, so a campaign from a previous service life exists only in
# the object store. Two things make it visible again: the campaign index supplies its id
# (and the start time the listing orders by), and ``_record_dir`` fetches the two small
# objects that carry its recorded facts.


class _IndexStorage:
    """Object store holding just the keys these tests exercise."""

    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.reads: list[str] = []
        self.prefix_downloads = 0

    # -- index --
    def upload_file(self, local_path, bucket, key):
        with open(local_path, "rb") as fh:
            self.objects[key] = fh.read()

    def list_keys(self, bucket, prefix=""):
        head = f"{prefix.rstrip('/')}/" if prefix else ""
        return sorted(k for k in self.objects if k.startswith(head))

    def delete_prefix(self, bucket, prefix):
        gone = [k for k in self.objects if k.startswith(f"{prefix.rstrip('/')}/")]
        for key in gone:
            del self.objects[key]
        return len(gone)

    # -- single-object reads --
    def stat_object(self, bucket, key):
        self.reads.append(key)
        blob = self.objects.get(key)
        return None if blob is None else len(blob)

    def download_object(self, bucket, key, dst):
        with open(dst, "wb") as fh:
            fh.write(self.objects[key])
        return True

    def download_prefix(self, *a, **kw):  # pragma: no cover - must never be called
        self.prefix_downloads += 1
        raise AssertionError("a campaign summary must not fetch the whole prefix")


@pytest.fixture
def indexed(cs, monkeypatch, tmp_path):
    """A ClusterService whose object store is an ``_IndexStorage``, with no local disk."""
    storage = _IndexStorage()
    monkeypatch.setattr(cs, "_campaigns_root", lambda: tmp_path / "results")
    monkeypatch.setattr(cs, "_cache_dir", lambda cid: tmp_path / "cache" / cid)
    monkeypatch.setattr(cs, "_campaign_object_location",
                        lambda cid: (storage, "bkt", ""))
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.in_pod_storage.storage_client_for",
        lambda cfg: storage)
    monkeypatch.setattr(cs, "_cluster_config", lambda: object())
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.in_pod_storage.campaign_index_bucket",
        lambda cfg: "bkt")
    cs._index_cache = None  # discovery is the subject here; read it from the fake store
    return cs, storage


def test_a_campaign_with_no_records_anywhere_is_unknown(indexed):
    """"Not reconstructable" is a phase, not an exception: the id resolves and says so."""
    cs, _storage = indexed
    status = cs._status_from_disk("nope-2026-07-17-120000")
    assert status.phase == "unknown"
    assert status.campaign_id == "nope-2026-07-17-120000"


def test_a_stored_campaign_reports_its_real_phase_not_unknown(indexed):
    """The durable ``outcome.json`` explains a campaign this process never drove.

    It travels through the *inherited* ``_status_from_disk``: ``_record_dir`` puts the
    object where every reader already looks, so the list view and the per-campaign status
    cannot disagree — which is what the deleted cluster-only override promised but could
    not deliver, since ``_summary_for`` never called it.
    """
    cs, storage = indexed
    cid = "camp-2026-07-17-120000"
    from robovast.execution.cluster_execution import in_pod_storage
    in_pod_storage.mark_campaign_indexed(storage, object(), cid, "t")
    storage.objects["_execution/outcome.json"] = (
        b'{"phase": "failed", "campaign_id": "' + cid.encode() + b'", '
        b'"error": "the runs were aborted"}')

    status = cs._status_from_disk(cid)
    assert status.phase == "failed"
    assert status.error == "the runs were aborted"


def test_records_are_two_single_object_reads_never_a_prefix_fetch(indexed):
    """A 2 KB record must not drag a 1 TB campaign — the point of the whole seam."""
    cs, storage = indexed
    from robovast.execution.cluster_execution import in_pod_storage
    in_pod_storage.mark_campaign_indexed(
        storage, object(), "camp-2026-07-17-120000", "t")
    storage.reads.clear()
    cs._record_dir("camp-2026-07-17-120000")
    assert storage.reads == list(ClusterService._RECORD_OBJECTS)
    assert storage.prefix_downloads == 0


def test_a_campaign_this_process_drives_is_never_fetched(indexed, monkeypatch):
    """Its driver owns ``campaign.db`` and is writing it right now."""
    cs, storage = indexed
    cid = "live-2026-07-17-120000"
    monkeypatch.setitem(cs._campaigns, cid, object())
    assert cs._record_dir(cid) == cs._campaign_dir(cid)
    assert storage.reads == []


def test_the_index_supplies_the_ids_the_disk_scan_cannot_see(indexed):
    cs, storage = indexed
    from robovast.execution.cluster_execution import in_pod_storage
    in_pod_storage.mark_campaign_indexed(
        storage, object(), "Camp_One-2026-07-17-120000", "2026-07-17T12:00:00+00:00")

    assert cs._durable_campaign_ids() == {"Camp_One-2026-07-17-120000"}


def test_the_ordering_pass_costs_no_object_reads(indexed):
    """``list_campaigns`` asks every candidate for its start time before it paginates, so
    a start time read per campaign would be one round-trip per campaign on a listing the
    SSE stream repeats every second. The marker carries it in its key instead."""
    cs, storage = indexed
    from robovast.execution.cluster_execution import in_pod_storage
    for i in range(5):
        in_pod_storage.mark_campaign_indexed(
            storage, object(), f"c{i}-2026-07-17-12000{i}", f"2026-07-17T12:00:0{i}+00:00")
    storage.reads.clear()

    started = {cid: cs._started_at_for(cid) for cid in cs._durable_campaign_ids()}
    assert started["c3-2026-07-17-120003"] == "2026-07-17T12:00:03+00:00"
    assert storage.reads == []


def test_an_indexed_campaign_is_marked_at_driver_start(indexed):
    """Before the image build and the run, so every later failure is still findable."""
    cs, storage = indexed
    cs._on_campaign_started("camp-2026-07-17-120000", "2026-07-17T12:00:00+00:00")
    assert cs._durable_campaign_ids() == {"camp-2026-07-17-120000"}


def test_indexing_failure_never_fails_the_campaign(cs, monkeypatch):
    """Discoverability is worth a warning, not a dead campaign — and a store broken
    enough to refuse this fails the campaign's own uploads with a real error anyway."""
    monkeypatch.setattr(cs, "_cluster_config",
                        lambda: (_ for _ in ()).throw(RuntimeError("no store")))
    cs._on_campaign_started("camp-2026-07-17-120000", "t")  # must not raise


def test_deleting_a_campaign_retires_its_marker(indexed):
    """Otherwise it keeps being listed with nothing behind it."""
    cs, storage = indexed
    cs._on_campaign_started("camp-2026-07-17-120000", "t")
    cs._unmark_campaign("camp-2026-07-17-120000")
    assert cs._durable_campaign_ids() == set()


def test_an_unreachable_store_keeps_the_last_known_index(indexed, monkeypatch):
    """A brief outage must not make every stored campaign blink out of the list."""
    cs, storage = indexed
    cs._on_campaign_started("camp-2026-07-17-120000", "t")
    assert cs._durable_campaign_ids() == {"camp-2026-07-17-120000"}

    # Expire the TTL while keeping the value, which is exactly the state a poll after a
    # brief outage is in.
    cs._index_cache = (cs._index_cache[0] - 999, cs._index_cache[1])
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.in_pod_storage.list_indexed_campaigns",
        lambda *a: (_ for _ in ()).throw(RuntimeError("store down")))
    assert cs._durable_campaign_ids() == {"camp-2026-07-17-120000"}


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

    # j-run's pod is actually Running, so it counts as running (not just active).
    monkeypatch.setattr(cs, "_k8s_batch", lambda: _Batch())
    monkeypatch.setattr(cs, "_k8s", lambda: _CoreWithPods([_job_pod("j-run")]))
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


def _job_pod(job_name, phase="Running"):
    import types
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(
            name=f"{job_name}-pod",
            labels={"batch.kubernetes.io/job-name": job_name}),
        status=types.SimpleNamespace(phase=phase))


class _CoreWithPods:
    def __init__(self, pods):
        import types
        self._items = types.SimpleNamespace(items=pods)

    def list_namespaced_pod(self, namespace, label_selector):
        return self._items


def test_list_jobs_reports_active_but_pending_pod_as_pending(cs, monkeypatch):
    """An 'active' Job whose pod is still Pending must not show as running."""
    import types
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
    """Backend-wide jobs tally splits Running from still-waiting scenario-run pods,
    ignoring non-scenario pods (the service pod, someone else's workload)."""
    import types

    def _node(cpu, mem):
        return types.SimpleNamespace(
            status=types.SimpleNamespace(allocatable={"cpu": cpu, "memory": mem}))

    def _pod_full(labels, phase):
        return types.SimpleNamespace(
            metadata=types.SimpleNamespace(labels=labels),
            status=types.SimpleNamespace(phase=phase),
            spec=types.SimpleNamespace(containers=[]))

    pods = [
        _pod_full({"jobgroup": "scenario-runs"}, "Running"),
        _pod_full({"jobgroup": "scenario-runs"}, "Running"),
        _pod_full({"jobgroup": "scenario-runs"}, "Pending"),
        _pod_full({"app": "robovast-service"}, "Running"),  # not a scenario run
    ]

    class _Core:
        def list_node(self):
            return types.SimpleNamespace(items=[_node("4", "8Gi")])

        def list_pod_for_all_namespaces(self, field_selector):
            return types.SimpleNamespace(items=pods)

    monkeypatch.setattr(cs, "_k8s", lambda: _Core())
    usage = cs.resource_usage()

    assert (usage.jobs_running, usage.jobs_pending) == (2, 1)


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

        def read_namespaced_pod_log(self, name, namespace, container, **_kw):
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

        def read_namespaced_pod_log(self, name, namespace, container, **_kw):
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


def test_get_job_log_reads_incrementally_across_polls(cs, monkeypatch):
    """A second poll fetches only a trailing window, not the whole log, yet the
    byte-offset stream continues seamlessly as the pod log grows."""
    import types
    calls = []  # since_seconds seen per read_namespaced_pod_log call

    def line(sec, nano, msg):
        return f"2026-07-21T10:00:{sec:02d}.{nano:09d}Z {msg}"

    class _Core:
        def __init__(self):
            self.rows = [(0, line(0, 1, "boot"))]  # (wall_second, timestamped line)

        def list_namespaced_pod(self, namespace, label_selector):
            return types.SimpleNamespace(items=[_pod(phase="Running")])

        def read_namespaced_pod_log(self, name, namespace, container,
                                    timestamps=False, since_seconds=None):
            calls.append(since_seconds)
            sel = self.rows if since_seconds is None else self.rows[-1:]
            text = "\n".join(r[1] for r in sel)
            return text + "\n" if text else ""

    core = _Core()
    monkeypatch.setattr(cs, "_k8s", lambda: core)

    first = cs.get_job_log("camp", "j")
    assert first.text == "boot\n"          # timestamp stripped for a single container
    assert calls[0] is None                # first poll reads the whole log

    core.rows.append((0, line(0, 2, "step 1")))  # log grows
    second = cs.get_job_log("camp", "j", offset=first.next_offset)
    assert second.text == "step 1\n"       # only the delta crosses the wire
    assert calls[1] is not None            # later polls read a bounded window
    # Full assembled text is still addressable from offset 0.
    assert cs.get_job_log("camp", "j", offset=0).text == "boot\nstep 1\n"


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


def test_cleanup_retires_the_markers_of_what_it_actually_removed(indexed):
    """A swept campaign must stop being listed — and a *survivor* must not.

    Driven by what the sweep did rather than what it was asked to do: a campaign whose
    bucket delete failed keeps its marker, because its data is still there and a listing
    that omits stored data is the defect this index exists to fix.
    """
    cs, storage = indexed
    from robovast.execution.cluster_execution import in_pod_storage
    for cid in ("gone_One-2026-07-17-120000", "kept-2026-07-17-120001"):
        in_pod_storage.mark_campaign_indexed(storage, object(), cid, "t")

    # In per-campaign-bucket mode the sweep reports sanitised *bucket* names; matching is
    # forward-only (id → bucket name), never the lossy inverse.
    cs._unmark_removed(["gone-one-2026-07-17-120000"])
    assert cs._durable_campaign_ids() == {"kept-2026-07-17-120001"}


def test_cleanup_that_removed_nothing_does_not_touch_the_store(indexed):
    cs, storage = indexed
    storage.reads.clear()
    cs._unmark_removed([])
    assert storage.objects == {} and storage.reads == []


def test_an_unindexed_campaign_is_never_fetched(indexed):
    """Nothing of it is in the store, so there is nothing to fetch — and this is what
    keeps a listing behind an unreachable store to one timeout instead of one per row."""
    cs, storage = indexed
    assert cs._record_dir("stranger-2026-07-17-120000") == \
        cs._campaign_dir("stranger-2026-07-17-120000")
    assert storage.reads == []
