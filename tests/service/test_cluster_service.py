# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for ClusterService's launch behaviour (no cluster needed).

The service drives cluster campaigns **in-process** (one worker thread each) over a
KubernetesBackend; there is no per-campaign controller pod any more, so these cover
the launch *hooks* it overrides on LocalTransport plus the aux-pod manifest that
replaced the old controller-pod sidecar.
"""

import json
import tempfile
import types
import time

import pytest

from robovast.common.variation.container_runner import ContainerSpec
from robovast.execution.cluster_execution.cluster_service import ClusterService
from robovast.execution.cluster_execution.container_runner import (AUX_LABEL,
                                                                   DEFAULT_AUX_DEADLINE_SECONDS,
                                                                   aux_pod_name,
                                                                   build_aux_pod_manifest)
from robovast.service.interface import CreateCampaignRequest
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore


@pytest.fixture
def cs():
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tempfile.mkdtemp()))
    # reap_on_start=False: the reaper talks to the kube API, which no test has.
    svc = ClusterService(namespace="ns1", cluster_config_name="rke2",
                         cluster_config_kwargs={"foo": "bar"}, store=store,
                         reap_on_start=False)
    # Seed the campaign-index cache empty. Campaign discovery reads the object store (see
    # _campaign_index), and off-cluster that opens a kubectl port-forward — which no test
    # has, and which *blocks* rather than failing. Tests that exercise discovery use the
    # ``indexed`` fixture below, which installs a fake store and clears this.
    svc._index_cache = (time.monotonic(), {})
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
    from robovast.service.interface import CampaignSummary, ListCampaignsResponse

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


def test_campaign_tar_stream_refuses_an_unknown_campaign_before_it_streams(cs, monkeypatch):
    """A campaign that is not here must fail *before* the response starts.

    Found live. Once a byte has been streamed the status line is already 200, so an
    unknown campaign reached the client as a truncated body -- ``ChunkedEncodingError:
    Response ended prematurely``, which names neither the campaign nor the problem, and
    left a ``.part`` file behind. Raised eagerly it is a 404 with a sentence in it.

    The predicate is the one ``list_campaigns`` answers with, so the archive route and the
    listing cannot disagree about what this service has.
    """
    import types

    monkeypatch.setattr(cs, "_durable_campaign_ids", lambda: {"other-2026-01-01-000000"})
    monkeypatch.setattr(
        cs, "_cluster_config",
        lambda: types.SimpleNamespace(add_campaign_members=lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not touch the object store for a campaign that is not here"))))

    with pytest.raises(KeyError, match="camp-2026-01-01-000000"):
        cs.campaign_tar_stream("camp-2026-01-01-000000")


def test_campaign_tar_stream_streams_object_store_excluding_postproc(cs, monkeypatch):
    """The download stream tars objects from the config's add_campaign_members,
    passing the _postproc exclusion — no scratch on the service."""
    import types

    monkeypatch.setattr(cs, "_durable_campaign_ids", lambda: {"camp-2026-01-01-000000"})
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

def _project_needing_a_build(tmp_path, python_packages=None):
    """A validated config whose scenario container adds packages, so an image is built."""
    from robovast.common.config import validate_config
    (tmp_path / "p.vast").write_text("")
    campaign_config = validate_config({
        "version": 2,
        "execution": {"runs": 1, "containers": {"scenario": {
            "image": "base:1",
            "python_packages": python_packages or ["shapely>=2.0"]}}}})
    import types
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
    import types

    from robovast.common.errors import CampaignConfigError
    from robovast.execution.cluster_config.base_config import RegistryConfig
    monkeypatch.setattr(
        cs, "_cluster_config",
        lambda: types.SimpleNamespace(get_registry_config=RegistryConfig))
    # A registry with no prefix: enabled() is false. The lookup that would fill in its
    # Secrets lives in the image store now, so it is stubbed there — installing a store is
    # how a lane supplies one.
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
    # ``interactive=`` selects the timeout budget (fail-fast for polled request paths vs
    # patient for bulk transfers); it changes no behaviour these tests observe, so the
    # doubles accept and ignore it rather than each caller having to know.
    monkeypatch.setattr(cs, "_campaign_object_location",
                        lambda cid, *, interactive=False: (storage, "bkt", ""))
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.in_pod_storage.storage_client_for",
        lambda cfg, *, interactive=False: storage)
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
    cs, _storage = indexed
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
    cs, _storage = indexed
    cs._on_campaign_started("camp-2026-07-17-120000", "t")
    cs._unmark_campaign("camp-2026-07-17-120000")
    assert cs._durable_campaign_ids() == set()


def test_an_unreachable_store_keeps_the_last_known_index(indexed, monkeypatch):
    """A brief outage must not make every stored campaign blink out of the list."""
    cs, _storage = indexed
    cs._on_campaign_started("camp-2026-07-17-120000", "t")
    assert cs._durable_campaign_ids() == {"camp-2026-07-17-120000"}

    # Expire the TTL while keeping the value, which is exactly the state a poll after a
    # brief outage is in.
    cs._index_cache = (cs._index_cache[0] - 999, cs._index_cache[1])
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.in_pod_storage.list_indexed_campaigns",
        lambda *a: (_ for _ in ()).throw(RuntimeError("store down")))
    assert cs._durable_campaign_ids() == {"camp-2026-07-17-120000"}


def test_a_campaign_start_leaves_the_index_cache_warm(indexed):
    """Starting a campaign must not force a cold listing.

    Dropping the cache here was the worst possible timing: the campaign whose start
    invalidated it is about to saturate the same connection with its own uploads, and the
    1 Hz campaign-list poll would then meet a cold cache on every tick until some listing
    finally returned. The marker is the one fact a listing would have added, so add it.
    """
    cs, _storage = indexed
    cs._campaign_index()                      # populate the cache
    cs._on_campaign_started("camp-2026-07-17-120000", "2026-07-17T12:00:00+00:00")

    cached = cs._index_cache
    assert cached is not None, "the start must not drop the cache"
    assert cached[1]["camp-2026-07-17-120000"] == "2026-07-17T12:00:00+00:00"


def test_only_one_caller_lists_the_index_at_a_time(indexed, monkeypatch):
    """Single-flight: concurrent pollers take the stale value instead of each listing.

    The listing deliberately runs outside ``_index_lock`` (network I/O under a lock would
    queue every reader), which without this let every caller past a cold cache start its
    own round-trip. Behind a 1 Hz SSE poll against a slow store that grows without bound,
    each in-flight listing holding a worker thread — the mechanism that took the API down.
    """
    import threading
    cs, storage = indexed
    from robovast.execution.cluster_execution import in_pod_storage
    in_pod_storage.mark_campaign_indexed(storage, object(), "c-2026-07-17-120000", "t")
    cs._campaign_index()                      # warm, so there is a stale value to serve
    cs._index_cache = (cs._index_cache[0] - 999, cs._index_cache[1])   # expire the TTL

    in_listing, release = threading.Event(), threading.Event()
    calls = []

    def slow_list(*a):
        calls.append(1)
        in_listing.set()
        release.wait(5)          # hold the "network" open, like a stalled tunnel
        return {"c-2026-07-17-120000": "t"}.items()

    monkeypatch.setattr(
        "robovast.execution.cluster_execution.in_pod_storage.list_indexed_campaigns",
        slow_list)

    refresher = threading.Thread(target=cs._campaign_index, daemon=True)
    refresher.start()
    assert in_listing.wait(5), "the first caller should be out listing"

    # A second poll arriving mid-listing must return immediately with the stale value.
    assert cs._campaign_index() == {"c-2026-07-17-120000": "t"}
    assert len(calls) == 1, "the second caller must not start its own listing"

    release.set()
    refresher.join(5)
    assert len(calls) == 1


def test_a_failed_listing_releases_the_single_flight_flag(indexed, monkeypatch):
    """Otherwise one error would wedge the index on its stale value forever."""
    cs, _storage = indexed
    cs._campaign_index()
    cs._index_cache = (cs._index_cache[0] - 999, cs._index_cache[1])
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.in_pod_storage.list_indexed_campaigns",
        lambda *a: (_ for _ in ()).throw(RuntimeError("store down")))

    cs._campaign_index()
    assert cs._index_refreshing is False


# -- the MinIO port-forward keep-alive --------------------------------------

class _FakePf:
    """A ``kubectl port-forward`` child that stays alive, like the real stalled one."""

    def poll(self):
        return None

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0


@pytest.fixture
def pf(cs, monkeypatch):
    """*cs* with a fake port-forward, and a knob for whether the tunnel serves.

    Returns ``(cs, serving, opened)``: flip ``serving["ok"]`` to simulate the tunnel going
    stalled-but-alive, and read ``opened`` for the ports handed out.
    """
    import robovast.execution.cluster_execution.bucket_ops as bo
    serving, opened = {"ok": True}, []

    def _open(ns, ctx):
        port = 40000 + len(opened)
        opened.append(port)
        return _FakePf(), port

    monkeypatch.setattr(bo, "open_minio_port_forward", _open)
    monkeypatch.setattr(bo, "forward_is_serving",
                        lambda port, timeout_s=5.0: serving["ok"])
    cs._PF_PROBE_INTERVAL_S = 0.05      # the cadence is not what these tests are about
    yield cs, serving, opened
    cs._pf_monitor_stop.set()


def test_a_healthy_forward_is_never_rotated(pf):
    """The keep-alive must be invisible when nothing is wrong — rotating a working tunnel
    would break the transfers running over it."""
    cs, _serving, opened = pf
    cs._minio_port_forward_endpoint()
    time.sleep(0.4)                     # many probe intervals
    assert opened == [40000], "a serving forward was replaced"
    assert cs._pf_generation == 1


def test_a_stalled_forward_is_rotated_without_a_request_waiting_on_it(pf):
    """The point of the keep-alive.

    Before it, a stalled-but-alive tunnel was discovered only by an S3 request *timing
    out* — so the discovery cost that request its whole timeout budget, and every
    concurrent request paid it too. Nothing here issues an S3 call at all.
    """
    cs, serving, opened = pf
    first = cs._minio_port_forward_endpoint()
    serving["ok"] = False

    deadline = time.monotonic() + 5
    while cs._pf_generation < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert cs._pf_generation >= 2, "the stalled forward was never rotated"
    assert cs._minio_pf_endpoint != first
    assert len(opened) >= 2


def test_one_missed_probe_does_not_rotate(pf):
    """Two consecutive failures, so a single dropped probe — a tunnel busy mid-transfer,
    a GC pause — does not throw away a forward that is fine."""
    cs, serving, opened = pf
    cs._minio_port_forward_endpoint()
    assert cs._PF_FAILURES_BEFORE_ROTATE >= 2

    serving["ok"] = False               # exactly one probe fails, then it recovers
    time.sleep(cs._PF_PROBE_INTERVAL_S * 1.5)
    serving["ok"] = True
    time.sleep(0.3)
    assert opened == [40000], "a single missed probe rotated the forward"


def test_shutdown_stops_the_keepalive_before_closing_the_forward(pf):
    """Otherwise the keep-alive reads the teardown as a stall and reopens the tunnel the
    process is closing — leaking a kubectl child past exit."""
    cs, _serving, _opened = pf
    cs._minio_port_forward_endpoint()
    monitor = cs._pf_monitor
    assert monitor is not None and monitor.is_alive()

    cs.shutdown()
    assert not monitor.is_alive()
    assert cs._pf_monitor is None
    assert cs._minio_pf is None and cs._minio_pf_port is None


# -- jobs (live) ------------------------------------------------------------

def _job(name, *, succeeded=0, active=0, failed=0, full=None, suspend=False):
    import types
    ann = {"job-name-full": full} if full is not None else {}
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(name=name),
        status=types.SimpleNamespace(succeeded=succeeded, active=active, failed=failed),
        # suspend: Kueue creates every Job suspended and un-suspends it on admission.
        # A suspended Job has no pod, so only this flag reveals it exists.
        spec=types.SimpleNamespace(suspend=suspend, template=types.SimpleNamespace(
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

    _fake_kueue(monkeypatch)
    monkeypatch.setattr(cs, "_k8s_batch", lambda: _UsageBatch(jobs))
    monkeypatch.setattr(
        cs, "_k8s", lambda: _UsageCore([_usage_node("n1", "4", "8Gi")], pods, job_pods))
    usage = cs.resource_usage()

    assert (usage.jobs_running, usage.jobs_pending) == (2, 1)


def test_resource_usage_counts_kueue_suspended_jobs_as_pending(cs, monkeypatch):
    """A Kueue-suspended Job has **no pod**, and must still count as pending.

    Regression: the tally read pods, so the state every cluster batch *starts* in —
    the whole batch suspended, waiting for quota — reported ``0/0``, and the sidebar's
    jobs bar said nothing was happening while 3 runs were queued.
    """
    jobs = [_job("j-queued-1", suspend=True), _job("j-queued-2", suspend=True),
            _job("j-queued-3", suspend=True)]
    batch = _UsageBatch(jobs)

    _fake_kueue(monkeypatch)
    monkeypatch.setattr(cs, "_k8s_batch", lambda: batch)
    monkeypatch.setattr(
        cs, "_k8s", lambda: _UsageCore([_usage_node("n1", "4", "8Gi")], [], []))
    usage = cs.resource_usage()

    assert batch.calls == 1, "the tally must read Jobs — pods cannot see a suspended one"
    assert (usage.jobs_running, usage.jobs_pending) == (0, 3)


def test_resource_usage_counts_blocked_job_as_pending(cs, monkeypatch):
    """A job that cannot start on its own is accepted-but-not-executing: pending.

    The per-campaign ``JobCounts`` keeps ``blocked`` apart because that view has to act
    on it; a capacity meter only needs "not executing".
    """
    _fake_kueue(monkeypatch)
    monkeypatch.setattr(cs, "_k8s_batch", lambda: _UsageBatch([_job("j-stuck", active=1)]))
    monkeypatch.setattr(
        cs, "_k8s",
        lambda: _UsageCore([_usage_node("n1", "4", "8Gi")], [],
                           [_blocked_job_pod("j-stuck")]))
    usage = cs.resource_usage()

    assert (usage.jobs_running, usage.jobs_pending) == (0, 1)


def _fake_kueue(monkeypatch):
    """Stub Kueue's wait-reason lookup — it builds a CustomObjectsApi against whatever
    kube config the host has, which no test has (it degrades to ``{}``, but only after
    trying to reach an API server)."""
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.kubernetes_kueue.workload_wait_reasons",
        lambda namespace, job_names=None, k8s_custom=None: {
            name: "insufficient unused quota for cpu" for name in (job_names or ())})


def _blocked_job_pod(job_name):
    """A job pod stuck on an unpullable image — ``pod_block_reason`` reads it as blocked."""
    import types
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
        import types
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

    _fake_kueue(monkeypatch)
    monkeypatch.setattr(cs, "_k8s_batch", lambda: _UsageBatch(jobs))
    monkeypatch.setattr(
        cs, "_k8s",
        lambda: _UsageCore([_usage_node("workstation", "24", str(64 * 1024 ** 3))],
                           pods, job_pods))
    usage = cs.resource_usage()

    assert usage.cpu_capacity == 24
    assert usage.cpu_used == 8
    assert usage.memory_used_bytes == 8 * 1024 ** 3
    assert usage.cpu_used <= usage.cpu_capacity
    # the queued runs stay visible where pending work belongs
    assert (usage.jobs_running, usage.jobs_pending) == (3, 2)


def test_resource_usage_sums_node_filesystems(cs, monkeypatch):
    """Disk is the sum of every node's nodefs — and NOT of its imageFs.

    On a single-disk node ``node.fs`` and ``node.runtime.imageFs`` are two views of the
    same device, so a reader that summed both would double capacity and used alike. The
    imageFs figures here are deliberately different, so that mistake shows up as a wrong
    total rather than hiding in the arithmetic.
    """
    _fake_kueue(monkeypatch)
    monkeypatch.setattr(cs, "_k8s_batch", lambda: _UsageBatch([]))
    core = _UsageCore(
        [_usage_node("n1", "8", "16Gi"), _usage_node("n2", "8", "16Gi")], [], [],
        summaries={"n1": _summary(100, 40, image_fs=777),
                   "n2": _summary(200, 60, image_fs=888)})
    monkeypatch.setattr(cs, "_k8s", lambda: core)

    usage = cs.resource_usage()

    assert (usage.disk.capacity_bytes, usage.disk.used_bytes) == (300, 100)
    assert usage.disk_unavailable is None


def test_resource_usage_reports_no_disk_when_a_kubelet_is_silent(cs, monkeypatch):
    """One unreadable node means NO disk figure — never a partial sum, never a zero.

    A sum over the nodes that answered understates capacity and usage together, and
    nothing downstream could tell it from a real reading. The capacity meter must survive
    the failure: a missing `nodes/proxy` grant is not allowed to blank cpu and memory too.
    """
    _fake_kueue(monkeypatch)
    monkeypatch.setattr(cs, "_k8s_batch", lambda: _UsageBatch([]))
    core = _UsageCore(
        [_usage_node("n1", "8", str(16 * 1024 ** 3)),
         _usage_node("n2", "8", str(16 * 1024 ** 3))], [], [],
        summaries={"n1": _summary(100, 40)}, raise_on=["n2"])
    monkeypatch.setattr(cs, "_k8s", lambda: core)

    usage = cs.resource_usage()

    assert usage.disk is None and usage.store is None
    assert "nodes/proxy" in usage.disk_unavailable
    # the rest of the reading is untouched
    assert usage.cpu_capacity == 16
    assert usage.memory_capacity_bytes == 32 * 1024 ** 3


def test_resource_usage_memoises_the_kubelet_summary(cs, monkeypatch):
    """The Summary read has its own, longer TTL than the usage cache.

    One payload carries every pod's stats on that node, and a disk fills over minutes —
    so a poll that refreshed it every usage window would be paying per open browser tab
    for a number that had not changed.
    """
    _fake_kueue(monkeypatch)
    monkeypatch.setattr(cs, "_k8s_batch", lambda: _UsageBatch([]))
    core = _UsageCore([_usage_node("n1", "8", "16Gi")], [], [],
                      summaries={"n1": _summary(100, 40)})
    monkeypatch.setattr(cs, "_k8s", lambda: core)

    cs.resource_usage()
    cs._usage_cache = None          # force a fresh capacity sample
    usage = cs.resource_usage()

    assert usage.disk.capacity_bytes == 100
    assert core.proxy_calls == [("n1", "stats/summary")]


def test_resource_usage_reports_the_rke2_results_store(cs, monkeypatch):
    """The store meter comes from the provider, out of the summaries already fetched."""
    from robovast.execution.cluster_config.rke2 import Rke2ClusterConfig

    _fake_kueue(monkeypatch)
    monkeypatch.setattr(cs, "_k8s_batch", lambda: _UsageBatch([]))
    core = _UsageCore([_usage_node("n1", "8", "16Gi")], [], [],
                      summaries={"n1": _summary(1000, 400,
                                                pods=[_minio_pod(200, 1000)])})
    monkeypatch.setattr(cs, "_k8s", lambda: core)
    monkeypatch.setattr(cs, "_cluster_config", lambda: Rke2ClusterConfig())

    usage = cs.resource_usage()

    assert (usage.store.capacity_bytes, usage.store.used_bytes) == (1000, 200)
    # the emptyDir has no sizeLimit, so it shares the node filesystem's capacity
    assert usage.store.capacity_bytes == usage.disk.capacity_bytes


def test_resource_usage_has_no_store_when_the_provider_cannot_say(cs, monkeypatch):
    """A provider that cannot measure its store reports none — not a store of size zero.

    That is the honest answer for a cloud bucket, which has no capacity to fill, and for
    a MinIO pod on a node whose kubelet was not read.
    """
    _fake_kueue(monkeypatch)
    monkeypatch.setattr(cs, "_k8s_batch", lambda: _UsageBatch([]))
    core = _UsageCore([_usage_node("n1", "8", "16Gi")], [], [],
                      summaries={"n1": _summary(1000, 400)})   # no MinIO pod in the stats
    monkeypatch.setattr(cs, "_k8s", lambda: core)

    usage = cs.resource_usage()

    assert usage.store is None
    assert usage.disk is not None      # the disk meter is unaffected


def test_base_config_reports_no_store_usage_by_default():
    """The hook defaults to "cannot say", so a provider opts in rather than out."""
    from robovast.execution.cluster_config.base_config import BaseConfig

    assert BaseConfig.get_store_usage(object(), {"n1": {}}) == (None, None)


def _usage_node(name, cpu, mem):
    import types
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(name=name),
        status=types.SimpleNamespace(allocatable={"cpu": cpu, "memory": mem}))


def _summary(fs_capacity, fs_used, image_fs=None, pods=()):
    """One kubelet ``stats/summary`` payload, as the node proxy returns it (JSON text).

    ``image_fs`` is set to values DIFFERENT from ``node.fs`` on purpose: on a single-disk
    node the two are the same device, and a reader that summed them would double the disk.
    Distinct numbers make that mistake visible instead of arithmetically invisible.
    """
    node = {"fs": {"capacityBytes": fs_capacity, "usedBytes": fs_used}}
    if image_fs is not None:
        node["runtime"] = {"imageFs": {"capacityBytes": image_fs, "usedBytes": image_fs}}
    return json.dumps({"node": node, "pods": list(pods)})


def _minio_pod(used, capacity):
    """A pod entry shaped like the RKE2 MinIO pod's, for the results-store hook."""
    from robovast.execution.cluster_config.rke2 import MINIO_POD_NAME, MINIO_VOLUME_NAME
    return {"podRef": {"name": MINIO_POD_NAME, "namespace": "default"},
            "volume": [{"name": MINIO_VOLUME_NAME,
                        "usedBytes": used, "capacityBytes": capacity}]}


def _usage_pod(labels, phase, node=None, cpu=None, mem=None):
    import types
    requests = {}
    if cpu is not None:
        requests["cpu"] = cpu
    if mem is not None:
        requests["memory"] = mem
    containers = [types.SimpleNamespace(
        resources=types.SimpleNamespace(requests=requests))] if requests else []
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(labels=labels),
        status=types.SimpleNamespace(phase=phase),
        spec=types.SimpleNamespace(containers=containers, node_name=node))


class _UsageCore:
    """Two distinct pod reads: cluster-wide for CPU/memory, namespaced for the job tally.

    They are separate on purpose — capacity and usage must be summed over every node the
    cluster has, while the scenario-run tally answers "what is *this* service running".
    """

    def __init__(self, nodes, pods, job_pods=(), summaries=None, raise_on=()):
        import types
        self._nodes = types.SimpleNamespace(items=nodes)
        self._pods = types.SimpleNamespace(items=pods)
        self._job_pods = types.SimpleNamespace(items=list(job_pods))
        # {node: stats/summary JSON} for the disk meter, plus the nodes whose kubelet
        # refuses. proxy_calls counts reads so a test can prove the summary is memoised
        # on its own TTL rather than re-fetched with every usage poll.
        self._summaries = summaries or {}
        self._raise_on = set(raise_on)
        self.proxy_calls = []

    def connect_get_node_proxy_with_path(self, name, path, **kwargs):
        self.proxy_calls.append((name, path))
        if name in self._raise_on:
            raise RuntimeError("forbidden: nodes/proxy")
        return self._summaries[name]

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
    import types
    init = [types.SimpleNamespace(name="s3-init", restart_policy=None)]
    init += [types.SimpleNamespace(name=n, restart_policy="Always") for n in sidecars]
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(name=name),
        spec=types.SimpleNamespace(
            containers=[types.SimpleNamespace(name="robovast")],
            init_containers=init),
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


def _api_exception(status):
    """The kube API's "container is waiting to start" (400) / "gone" (404)."""
    from kubernetes import client
    return client.exceptions.ApiException(status=status)


def test_get_job_log_reads_a_pending_pods_sidecars(cs, monkeypatch):
    """A Pending pod is still read: its native sidecars are already logging.

    Kubelet runs native sidecars during the init phase, so the pod stays ``Pending``
    while the simulator starts — and a simulator that cannot load its world says so
    there and then keeps the pod Pending forever. Short-circuiting on the phase, as this
    used to, discarded exactly the output that explains the hang.
    """
    import types

    class _Core:
        def list_namespaced_pod(self, namespace, label_selector):
            return types.SimpleNamespace(
                items=[_pod(phase="Pending", sidecars=["simulation"])])

        def read_namespaced_pod_log(self, name, namespace, container, **_kw):
            if container == "simulation":
                return "2026-08-07T10:00:00Z could not load world\n"
            raise _api_exception(400)  # the scenario container has not started yet

    monkeypatch.setattr(cs, "_k8s", lambda: _Core())
    chunk = cs.get_job_log("camp", "j")
    assert "could not load world" in chunk.text
    assert "[simulation]" in chunk.text
    assert chunk.eof is False  # still Pending → keep polling


def test_get_job_log_merges_all_three_containers(cs, monkeypatch):
    """The reported break: sidecars moved to initContainers and vanished from the panel.

    Every line must carry a ``[container]`` prefix — that is what the web UI colors —
    and the three containers must interleave by kubelet's per-line timestamp rather than
    arriving in three blocks.
    """
    import types

    logs = {
        "robovast": "2026-08-07T10:00:02Z executing scenario\n",
        "simulation": "2026-08-07T10:00:01Z mujoco model loaded\n",
        "sut": "2026-08-07T10:00:03Z bt_navigator ready\n",
    }

    class _Core:
        def list_namespaced_pod(self, namespace, label_selector):
            return types.SimpleNamespace(
                items=[_pod(sidecars=["simulation", "sut"])])

        def read_namespaced_pod_log(self, name, namespace, container, **_kw):
            return logs[container]

    monkeypatch.setattr(cs, "_k8s", lambda: _Core())
    lines = cs.get_job_log("camp", "j").text.splitlines()
    assert [line.split("]")[0] + "]" for line in lines] == [
        "[simulation]", "[robovast]", "[sut]"]  # timestamp order, not spec order
    assert "mujoco model loaded" in lines[0]


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
        {"version": 2, "execution": {"containers": {"scenario": {"image": "reg/combined:1"},
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
        {"version": 2, "execution": {"containers": {"scenario": {"image": "reg/scenario:1"},
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
    vast.write_text("version: 2\nexecution:\n  mode: ros2\n  containers:\n    simulation:\n"
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
    from pathlib import Path

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


def test_the_cluster_lane_fetches_a_campaign_owned_world_before_resolving_identity():
    """Nothing is on local disk here until it is asked for.

    A world declared as a path in the .vast is archived under _config/, and its path is
    only known once the capture has been read -- so it cannot join _scene_source_dir's
    fetch and has to be materialised separately, exactly as the capture is. Without it the
    build failed with "not archived with the campaign" for a world that was archived, just
    not yet local.

    The whole prefix, not the world object: what the world references travels with it, and
    the cache key is computed over the tree.
    """
    from types import SimpleNamespace
    from unittest.mock import patch

    svc = ClusterService.__new__(ClusterService)
    fetched = []
    svc._materialize = lambda cid, rels, what, interactive=False: fetched.extend(rels)
    svc._scene_capture = lambda cid, cn, rid: {"world": "/config/files/depot_nav2.yaml"}
    svc.list_files = lambda address, recursive=False, limit=100: SimpleNamespace(
        entries=["files/", "files/depot_nav2.yaml", "environments/hex/3d-mesh/hex.stl"])
    with patch.object(type(svc).__mro__[1], "_scene_identity",
                      lambda *a, **k: ("identity", "key")):
        svc._scene_identity("camp", "goal-1", "0")
    assert "_config/files/depot_nav2.yaml" in fetched
    assert "_config/environments/hex/3d-mesh/hex.stl" in fetched
    # Directory entries keep a trailing "/" and are not objects to fetch.
    assert "_config/files/" not in fetched


def test_a_packaged_world_fetches_the_vast_and_nothing_else():
    """The world lives in the image, so its meshes are not this side's to stage.

    The ``.vast`` still is: it names the simulator, and without it the build cannot ask which
    backend knows how to compile the geometry -- a campaign whose world needs no staging at all
    would otherwise be refused with "no simulator declared".
    """
    from types import SimpleNamespace
    from unittest.mock import patch

    svc = ClusterService.__new__(ClusterService)
    fetched = []
    svc._materialize = lambda cid, rels, what, interactive=False: fetched.extend(rels)
    svc._scene_capture = lambda cid, cn, rid: {"world": "roqsim_scenes:depot"}
    svc.list_files = lambda address, recursive=False, limit=100: SimpleNamespace(
        entries=["p.vast", "environments/hex/3d-mesh/hex.stl"])
    with patch.object(type(svc).__mro__[1], "_scene_identity",
                      lambda *a, **k: ("identity", "key")):
        svc._scene_identity("camp", "goal-1", "0")
    assert fetched == ["_config/p.vast"]


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
                       execution=None):
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

    class _Lane:
        calls: list = []

        def exec_in(self, target, argv, limit_s, env=None):
            _Lane.calls.append((target, argv))
            # Matched on the joined argv: every read runs through a shell that sources the run's
            # ROS overlay first, so the command is inside one element rather than being them.
            joined = " ".join(argv)
            if "scenario_execution.tree_state" in joined:
                return (0, '{"found": true, "running": {"name": "drive_to"}}', "", False)
            if "resource_usage_" in joined:
                return (0, "", "", False)
            return exec_result

    _Lane.calls = []
    monkeypatch.setattr(cs, "_exec_lane", lambda: _Lane())
    return core, _Lane


def test_cluster_get_job_state_execs_into_the_job_s_pod(cs, monkeypatch):
    """The lane difference is the target and nothing else. ``/out`` is *this pod's* emptyDir, so
    naming it is exact even though a Kubernetes Job may pack several runs and its ``job_name`` is
    not a run key."""
    core, lane = _cluster_job_state(
        cs, monkeypatch, pods=[_Pod("scenario-abc-x9")],
        exec_result=(0, '{"findings": [], "state": {"sim_ts": 4.0}}', "", False))

    state = cs.get_job_state("camp-1", "scenario-abc")

    assert state.simulator == {"findings": [], "state": {"sim_ts": 4.0}}
    assert state.scenario["running"]["name"] == "drive_to"
    target, argv = [c for c in lane.calls if "tool --json" in " ".join(c[1])][0]
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
    _cluster_job_state(cs, monkeypatch, pods=[_Pod("scenario-abc-x9")])
    monkeypatch.setattr("robovast.common.simulators.health_command",
                        lambda execution, *, run_dir, base_dir="": None)

    state = cs.get_job_state("camp-1", "scenario-abc")

    assert state.simulator is None
    assert state.scenario["running"]["name"] == "drive_to"
    assert any("does not report its own state" in line for line in state.unavailable)


def test_the_health_pull_resolves_every_running_pod_on_the_cluster(cs, monkeypatch):
    """The lane that matters, so the pull is asserted here and not only locally: each running Job
    is asked in *its own* pod, over the Kubernetes exec API, and a job with no pod yet is skipped
    rather than crashing the sweep.

    The inherited resolver walks ``list_jobs`` and asks the lane hook for each running one, so this
    pins the composition rather than a second implementation of it.
    """
    # A ROS-shape pod: the simulator is a sidecar with its own image, which is the container the
    # health read has to reach -- `roqsim health` sent to the scenario container names a tool that
    # container does not have.
    pod = _Pod("scenario-abc-x9", sidecars=("simulation", "sut"))
    core, lane = _cluster_job_state(cs, monkeypatch, pods=[pod], execution=_ROS_EXECUTION)
    monkeypatch.setattr(cs, "list_jobs", lambda cid: types.SimpleNamespace(jobs=[
        types.SimpleNamespace(job_name="scenario-abc", status="running"),
        types.SimpleNamespace(job_name="scenario-def", status="completed"),
    ]))

    targets = cs._health_targets("camp-1")

    # ``/out`` and not a run key: this pod's own emptyDir holds only this job's runs, and a packed
    # Job has no single run dir to name even in principle.
    # Both paths, because the simulator's records and the job's artifacts are different subtrees:
    # the job dir first (where a LIVE run's clock record is), the run dir after it. The run dir is
    # the resolved one -- the fixture's lane returns no run key, so it falls back to the job root,
    # which is the documented behaviour for a job that has not written a record yet.
    assert targets == [("scenario-abc", "/out/_jobs/batch-0/job-0", "/out")]
    assert "job-name=scenario-abc" in core.selector
    # One exec, and only the run-dir resolution: the reads themselves are the caller's to make, so
    # a target that is merely being ENUMERATED must not trigger a health command.
    assert [" ".join(c[1]) for c in lane.calls if "tool --json" in " ".join(c[1])] == []
    # And the read itself lands in the simulator's own container, from the pod rather than from a
    # name built here: that is the difference between asking the simulator and asking a container
    # that has never heard of it.
    assert cs._job_state_target("camp-1", "scenario-abc", "simulation")[0] == (
        "scenario-abc-x9", "simulation")


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


def test_an_unpacked_job_is_located_too(cs, monkeypatch):
    """An unpacked Job is one run, but its NAME is not the run key -- so the run still has to be
    resolved, and it used to be left to the readers instead. Both of them can search a couple of
    levels down for their own file, which is two other components modelling this layout, answering
    with a heuristic ("the newest below here") where the service has the fact. Worse, searching
    around a directory MASKS a wrong one: pointed at ``_jobs/batch-0`` a reader looked past it and
    then blamed ``--bt-log``."""
    _core, lane = _cluster_job_state(cs, monkeypatch, pods=[_Pod("scenario-abc-x9")])
    monkeypatch.setattr(cs, "_exec_lane", lambda: types.SimpleNamespace(
        exec_in=lambda target, argv, limit_s, env=None: (0, "cfga/0\n", "", False)))

    assert cs._job_live_run("c", "scenario-abc", ("p", "c"), "/out") == ("/out/cfga/0", "cfga/0")
    del lane


def test_a_packed_job_names_the_run_it_is_on(cs, monkeypatch):
    """A packed Job runs its items one after another, so exactly one is live -- and every section
    of the reply must describe that one. Pointed at the Job's whole ``/out``, the three readers
    each picked a run for themselves and the caller could not tell which."""
    execution = {**_ROS_EXECUTION, "runs_per_job": 4}
    _core, lane = _cluster_job_state(cs, monkeypatch, pods=[_Pod("scenario-abc-x9")],
                                    execution=execution)
    monkeypatch.setattr(cs, "_exec_lane", lambda: types.SimpleNamespace(
        exec_in=lambda target, argv, limit_s, env=None: (0, "cfgb/2\n", "", False)))

    assert cs._job_live_run("c", "scenario-abc", ("p", "c"), "/out") == ("/out/cfgb/2", "cfgb/2")


def test_the_live_run_search_looks_for_run_dirs_and_not_for_the_newest_file(cs, monkeypatch):
    """A campaign root holds ``_jobs/`` beside its runs, and the job artifacts under it are the
    files most recently written -- so taking the newest file anywhere named the run ``_jobs/batch-0``
    and pointed every reader at a subtree with no run in it. The search is for the run LAYOUT."""
    execution = {**_ROS_EXECUTION, "runs_per_job": 4}
    seen = {}
    _cluster_job_state(cs, monkeypatch, pods=[_Pod("scenario-abc-x9")], execution=execution)
    monkeypatch.setattr(cs, "_exec_lane", lambda: types.SimpleNamespace(
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
    simulator-less -- config can outvote. The plan used to be asked first, so such a config
    resolved the role to the scenario container and the health read entered a container with no
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


def test_a_packed_job_that_has_written_nothing_keeps_the_job_root(cs, monkeypatch):
    """A run between starting and its first record is normal. The readers' own "nothing here yet"
    is a better answer than a failure from the step that was only trying to be more precise."""
    execution = {**_ROS_EXECUTION, "runs_per_job": 4}
    _cluster_job_state(cs, monkeypatch, pods=[_Pod("scenario-abc-x9")], execution=execution)
    monkeypatch.setattr(cs, "_exec_lane", lambda: types.SimpleNamespace(
        exec_in=lambda target, argv, limit_s, env=None: (0, "", "", False)))

    assert cs._job_live_run("c", "scenario-abc", ("p", "c"), "/out") == ("/out", None)


def test_a_job_between_scheduling_and_running_is_skipped_not_fatal(cs, monkeypatch):
    """Normal on this lane: a Job exists before its pod does. One unanswerable job must not cost
    the campaign's other jobs their check."""
    _cluster_job_state(cs, monkeypatch, pods=[])
    monkeypatch.setattr(cs, "list_jobs", lambda cid: types.SimpleNamespace(jobs=[
        types.SimpleNamespace(job_name="scenario-abc", status="running")]))

    assert cs._health_targets("camp-1") == []

def test_deleting_an_imported_campaign_does_not_ask_the_object_store(cs, monkeypatch):
    """An imported campaign has no bucket, so deletion must not go looking for one.

    Found live, against a real deployment. An import is extracted onto the service's own
    filesystem and registered from there, so it lists and queries perfectly well while the
    object store has never heard of it. Deletion went to the store first regardless; the
    provider raised whatever it raises for a name it cannot resolve, that escaped the
    ``NoSuchBucket`` tolerance as an unhandled error, and the campaign became undeletable
    through every client -- the web UI, the CLI and the MCP tool alike, with a bare 500.

    The store is asked only about campaigns it actually holds. Anything reaching
    ``bucket_ops`` here is the failure.
    """
    from robovast.execution.cluster_execution import bucket_ops

    monkeypatch.setattr(cs, "_durable_campaign_ids", lambda: set())
    monkeypatch.setattr(cs, "_ensure_deletable", lambda cid: None)
    monkeypatch.setattr(cs, "_unmark_campaign", lambda cid: None)
    monkeypatch.setattr(
        bucket_ops, "delete_campaign",
        lambda *a, **k: pytest.fail("the object store must not be asked about an "
                                    "imported campaign it never held"))
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.cluster_execution.cleanup_cluster_campaign",
        lambda **k: None)

    result = cs.delete_campaign("imported-2026-01-01-000000")
    assert result.ok


def test_deleting_a_cluster_campaign_still_clears_its_bucket(cs, monkeypatch):
    """The other half: a campaign the store does hold is still deleted from it.

    Guards the skip above from becoming "never delete anything", which would leak every
    finished cluster campaign's data while reporting success.
    """
    from robovast.execution.cluster_execution import bucket_ops

    asked = []
    monkeypatch.setattr(cs, "_durable_campaign_ids",
                        lambda: {"camp-2026-01-01-000000"})
    monkeypatch.setattr(cs, "_ensure_deletable", lambda cid: None)
    monkeypatch.setattr(cs, "_unmark_campaign", lambda cid: None)
    monkeypatch.setattr(cs, "_cluster_config", lambda: object())
    monkeypatch.setattr(bucket_ops, "delete_campaign",
                        lambda cid, cfg, **k: asked.append(cid))
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.cluster_execution.cleanup_cluster_campaign",
        lambda **k: None)

    assert cs.delete_campaign("camp-2026-01-01-000000").ok
    assert asked == ["camp-2026-01-01-000000"]
