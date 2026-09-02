# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for KubernetesBackend's campaign_root completion.

The auto-chained analysis postprocessing runs against ``campaign_root`` *before*
``finalize_campaign``, so the backend must leave it complete after ``run_batch``:
the campaign-level snapshot (``_config``/``_transient``) projected from the object
store, and ``_execution/execution.yaml`` recorded — exactly what the local (Docker)
backend leaves via ``run.sh``.
"""

import types

import pytest

from robovast.execution.backends import RunOptions
from robovast.execution.cluster_execution import in_pod_storage
from robovast.execution.cluster_execution.kubernetes_backend import (BatchJobRunner,
                                                                     KubernetesBackend)


class _FakeStorage:
    """Records download_prefix / upload_dir calls; no I/O."""

    def __init__(self):
        self.downloads = []

    def download_prefix(self, bucket, prefix, local_dir, force=False, on_file=None):
        self.downloads.append(prefix)
        return 1

    def upload_dir(self, local_dir, bucket, prefix=""):
        return 7


def _runner_for_download_test(configs):
    """A BatchJobRunner stubbed down to just its download step."""
    r = BatchJobRunner()
    r.cluster_config = object()
    r.campaign = "camp-2026-07-17-120000"
    r.configs = configs
    r._batch_tag = "batch-0"
    r.campaign_data = {"execution": {}}
    # Stub every side-effecting step so only the download loop runs.
    r._ensure_k8s_initialized = lambda: None
    r._s3_settings = lambda: ("ep", "ak", "sk", "bkt", "")  # embedded: empty prefix
    r._write_job_param_files = lambda out_dir, campaign_root=None: None
    r._build_jobs = lambda: []          # no jobs → submission loop is empty
    r.get_remaining_jobs = lambda names: []  # wait loop breaks immediately
    r._write_job_links = lambda cr: None
    r.cleanup_jobs = lambda campaign=None: None
    r.cleanup_pods = lambda campaign=None: None
    return r


def test_run_batch_in_pod_projects_campaign_level_snapshot(monkeypatch, tmp_path):
    """The download must include the campaign-level _config/, _transient/ and the
    batch's job-artifact dir _jobs/<batch_tag>/ (holds sysinfo.yaml et al.).

    Without _config/*.vast the auto-chain's ``campaign_vast`` fails; without
    _jobs/ the per-run ``job`` symlink cannot resolve sysinfo.yaml for metadata.
    """
    storage = _FakeStorage()
    monkeypatch.setattr(in_pod_storage, "storage_client_for", lambda cfg: storage)
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.kubernetes_backend.prepare_campaign_configs",
        lambda out_dir, data, cluster=False, instance_type_command=None: None)

    runner = _runner_for_download_test([{"name": "cfgA"}, {"name": "cfgB"}])
    runner.run_batch_in_pod(str(tmp_path))

    assert storage.downloads == ["cfgA", "cfgB", "_config", "_transient",
                                 "_jobs/batch-0"]


def test_run_batch_in_pod_whole_campaign_single_prefix_download(monkeypatch, tmp_path):
    """Batch mode fetches the whole campaign in one prefix download, not per config.

    The per-config enumeration exists only to scope _jobs/ across a search's many
    batches; in batch mode this one batch *is* the campaign, so a single prefix
    download avoids the O(configs) sequential list calls that stall a large batch.
    """
    storage = _FakeStorage()
    monkeypatch.setattr(in_pod_storage, "storage_client_for", lambda cfg: storage)
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.kubernetes_backend.prepare_campaign_configs",
        lambda out_dir, data, cluster=False, instance_type_command=None: None)

    runner = _runner_for_download_test([{"name": "cfgA"}, {"name": "cfgB"}])
    runner.run_batch_in_pod(str(tmp_path), whole_campaign=True)

    # One download against the (here embedded/empty) campaign prefix — not one per
    # config plus the campaign-level dirs.
    assert storage.downloads == [""]


def test_run_batch_in_pod_materialises_job_symlinks(monkeypatch, tmp_path):
    """Each run's ``job`` symlink is created so metadata resolves sysinfo.yaml now.

    In the cluster flow the symlinks were only materialised at upload-to-share,
    after the driver's own metadata/postprocessing had already run.
    """
    storage = _FakeStorage()
    monkeypatch.setattr(in_pod_storage, "storage_client_for", lambda cfg: storage)
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.kubernetes_backend.prepare_campaign_configs",
        lambda out_dir, data, cluster=False, instance_type_command=None: None)

    runner = _runner_for_download_test([{"name": "cfgA"}])
    # Seed the job-links manifest create_job_links reads (the no-op _write_job_links
    # stub leaves it intact; the fake storage download writes no files).
    transient = tmp_path / "_transient"
    transient.mkdir(parents=True)
    (transient / "job_links.yaml").write_text(
        "cfgA/0/job: ../../_jobs/batch-0/job-0\n")

    runner.run_batch_in_pod(str(tmp_path))

    link = tmp_path / "cfgA" / "0" / "job"
    assert link.is_symlink()
    assert (tmp_path / "cfgA" / "0" / "job").readlink().name == "job-0"


def test_run_batch_in_pod_aborts_cleanly_on_stop(monkeypatch, tmp_path):
    """A cooperative stop abandons the batch with CampaignStopped, before download.

    On Ctrl+C the storage tunnel is gone; pressing on to download would only fail
    noisily. The runner must raise the clean stop signal and never touch storage.
    """
    from robovast.execution.backends import CampaignStopped

    storage = _FakeStorage()
    monkeypatch.setattr(in_pod_storage, "storage_client_for", lambda cfg: storage)
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.kubernetes_backend.prepare_campaign_configs",
        lambda out_dir, data, cluster=False, instance_type_command=None: None)

    runner = _runner_for_download_test([{"name": "cfgA"}])
    runner._state = types.SimpleNamespace(stop_requested=True)

    with pytest.raises(CampaignStopped):
        runner.run_batch_in_pod(str(tmp_path))
    assert storage.downloads == []  # never attempted a download against a dead tunnel


def _backend():
    return KubernetesBackend(cluster_config=object(), namespace="ns",
                             kube_context=None)


def test_run_batch_records_execution_yaml_before_finalize(monkeypatch, tmp_path):
    """execution.yaml is written in run_batch (so postprocess can read the image)."""
    calls = []
    monkeypatch.setattr("robovast.common.execution.create_execution_yaml",
                        lambda runs, out, **kw: calls.append((runs, out, kw)))
    monkeypatch.setattr(
        BatchJobRunner, "for_batch",
        classmethod(lambda cls, **kw: types.SimpleNamespace(
            run_batch_in_pod=lambda campaign_root, whole_campaign=False: None)))

    # The declared image is enough: resolution needs no env when the campaign names one.
    _backend().run_batch(
        {"execution": {"containers": {"scenario": {"image": "img:test"}}}},
        campaign_root=str(tmp_path), batch_tag="b", runs=3, options=RunOptions())

    assert len(calls) == 1
    runs, out, kw = calls[0]
    assert runs == 3 and out == str(tmp_path)
    assert kw["execution_params"] == {"containers": {"scenario": {"image": "img:test"}}}


def test_finalize_no_longer_records_execution_yaml(monkeypatch, tmp_path):
    """finalize is now pure upload — execution.yaml was already recorded earlier."""
    called = []
    monkeypatch.setattr("robovast.common.execution.create_execution_yaml",
                        lambda *a, **k: called.append(True))
    monkeypatch.setattr(in_pod_storage, "campaign_storage_location",
                        lambda cfg, cid: ("bkt", ""))
    storage = _FakeStorage()
    monkeypatch.setattr(in_pod_storage, "storage_client_for", lambda cfg: storage)

    be = _backend()
    be.finalize_campaign(str(tmp_path / "camp-2026-07-17-120000"))

    assert called == []  # finalize must not create execution.yaml anymore


# --- A restarted container invalidates its trial, not the campaign -------------------
#
# The guard this replaces raised CampaignConfigError out of the wait loop, which ended the
# whole campaign. One sidecar crash in one job of one batch ends a long search and
# orphaned the two batches that had already finished.

class _FakeBatchClient:
    """Records the jobs deleted; creation is a no-op."""

    def __init__(self):
        self.deleted = []

    def create_namespaced_job(self, namespace, body):
        return None

    def delete_namespaced_job(self, name, namespace, **kwargs):
        self.deleted.append(name)


def _job(index, config_name, runs=1):
    """A JobSpec-shaped stand-in: what `_build_jobs` hands the wait loop."""
    items = [types.SimpleNamespace(config_name=config_name, run_number=n)
             for n in range(runs)]
    return types.SimpleNamespace(index=index, items=items)


def _restart_runner(monkeypatch, tmp_path, jobs, forensics, *, remaining_after=()):
    """A runner whose wait loop sees *forensics* on its first poll."""
    storage = _FakeStorage()
    monkeypatch.setattr(in_pod_storage, "storage_client_for", lambda cfg: storage)
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.kubernetes_backend.prepare_campaign_configs",
        lambda out_dir, data, cluster=False, instance_type_command=None: None)
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.kubernetes_backend.restarted_job_forensics",
        lambda core, ns, label, job_names=None: forensics)
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.kubernetes_backend.previous_container_log",
        lambda core, ns, pod, container, tail_lines=400: ("boom\ntraceback\n", "captured"))
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.kubernetes_backend"
        ".blocked_and_contended_reasons", lambda core, ns, label: ({}, {}))
    # The wait loop derives a job's name rather than reading it back off a rendered manifest
    # (under admission the manifest does not exist until there is room). Patch the derivation
    # so these fixtures keep their short synthetic names.
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.kubernetes_backend._short_job_name",
        lambda campaign, tag, index: f"rrroqs-x-{index}")

    runner = _runner_for_download_test([{"name": "cfgA"}])
    runner.namespace = "ns"
    runner.k8s_client = object()
    runner.k8s_batch_client = _FakeBatchClient()
    runner._build_jobs = lambda: jobs
    runner.create_job_manifest = lambda job, total, node_figures=None: {
        "metadata": {"name": f"rrroqs-x-{job.index}"}}
    polls = [list(remaining_after), []]
    runner.get_remaining_jobs = lambda names: polls.pop(0) if polls else []
    return runner, storage


_SUT_CRASH = {
    "detail": "ContainerRestarted: container sut restarted 1x after Error "
              "(exit 135, SIGBUS)",
    "containers": [{
        "pod_name": "rrroqs-x-0-pod", "node_name": "a-node", "pod_phase": "Running",
        "container": "sut", "role": "sut", "image": "an-image",
        "image_id": "an-image@sha256:abc", "restart_count": 1, "reason": "Error",
        "exit_code": 135, "signal": 7, "signal_name": "SIGBUS", "message": None,
        "started_at": None, "finished_at": None,
        "cpu_limit": "3.25", "memory_limit": None, "invalidating": True,
        "detail": "container sut restarted 1x after Error (exit 135, SIGBUS)",
    }],
}


def test_a_restarted_job_is_deleted_and_the_batch_continues(monkeypatch, tmp_path):
    """The point of the whole change: one job goes, the batch drains around the hole.

    `get_remaining_jobs` treats a deleted Job as finished, which is the same seam
    `stop_job` uses -- so the siblings run to completion and the batch still projects its
    results, instead of the campaign ending here.
    """
    runner, _ = _restart_runner(
        monkeypatch, tmp_path, [_job(0, "cfgA"), _job(1, "cfgA")],
        {"rrroqs-x-0": _SUT_CRASH}, remaining_after=["rrroqs-x-0", "rrroqs-x-1"])

    runner.run_batch_in_pod(str(tmp_path))  # must NOT raise

    assert runner.k8s_batch_client.deleted == ["rrroqs-x-0"]


def test_the_invalidated_job_is_recorded_in_the_ledger(monkeypatch, tmp_path):
    """A discarded trial must be visible as discarded, not merely absent."""
    import json

    runner, _ = _restart_runner(
        monkeypatch, tmp_path, [_job(0, "cfgA"), _job(1, "cfgA")],
        {"rrroqs-x-0": _SUT_CRASH}, remaining_after=["rrroqs-x-0", "rrroqs-x-1"])
    runner.run_batch_in_pod(str(tmp_path))

    entry, = json.loads(
        (tmp_path / "_execution" / "interventions.json").read_text())
    assert entry["kind"] == "invalid"
    assert entry["source"] == "runner"
    assert entry["job_dir"] == "_jobs/batch-0/job-0"
    assert entry["runs"] == ["cfgA/0"]
    assert "SIGBUS" in entry["detail"]


def test_the_evidence_is_captured_before_the_pod_is_deleted(monkeypatch, tmp_path):
    """The dead container's log lives only as long as its pod, and the next thing this
    code does is delete the Job. Nothing in robovast read it before."""
    import json

    runner, _ = _restart_runner(
        monkeypatch, tmp_path, [_job(0, "cfgA"), _job(1, "cfgA")],
        {"rrroqs-x-0": _SUT_CRASH}, remaining_after=["rrroqs-x-0", "rrroqs-x-1"])
    runner.run_batch_in_pod(str(tmp_path))

    record, = json.loads(
        (tmp_path / "_execution" / "container_failures.json").read_text())
    assert record["signal_name"] == "SIGBUS"
    assert record["exit_code"] == 135
    assert record["node_name"] == "a-node"
    assert record["memory_limit"] is None      # the absence IS the finding
    assert record["log_status"] == "captured"
    assert "traceback" in record["log_tail"]
    assert record["runs"] == ["cfgA/0"]


def test_a_packed_jobs_runs_are_all_invalidated(monkeypatch, tmp_path):
    """One container death ruins every run the job was carrying, not just the current one:
    they shared the process that lost its state."""
    import json

    runner, _ = _restart_runner(
        monkeypatch, tmp_path, [_job(0, "cfgA", runs=3), _job(1, "cfgA")],
        {"rrroqs-x-0": _SUT_CRASH}, remaining_after=["rrroqs-x-0", "rrroqs-x-1"])
    runner.run_batch_in_pod(str(tmp_path))

    entry, = json.loads((tmp_path / "_execution" / "interventions.json").read_text())
    assert entry["runs"] == ["cfgA/0", "cfgA/1", "cfgA/2"]


def test_a_job_is_invalidated_only_once(monkeypatch, tmp_path):
    """A restart is reported on every poll until the pod is gone, and deleting a Job is
    asynchronous -- so without the guard one crash is recorded on every pass."""
    import json

    runner, _ = _restart_runner(
        monkeypatch, tmp_path, [_job(0, "cfgA"), _job(1, "cfgA")],
        {"rrroqs-x-0": _SUT_CRASH},
        remaining_after=["rrroqs-x-0", "rrroqs-x-1"])
    runner.run_batch_in_pod(str(tmp_path))

    assert runner.k8s_batch_client.deleted == ["rrroqs-x-0"]
    assert len(json.loads(
        (tmp_path / "_execution" / "interventions.json").read_text())) == 1


def test_a_restart_seen_after_the_last_job_finished_still_lands(monkeypatch, tmp_path):
    """The wait loop breaks on an empty `remaining` BEFORE it probes, so a crash in the
    last job's last seconds was never observed at all."""
    import json

    runner, _ = _restart_runner(
        monkeypatch, tmp_path, [_job(0, "cfgA")], {"rrroqs-x-0": _SUT_CRASH})
    runner.run_batch_in_pod(str(tmp_path))

    entry, = json.loads((tmp_path / "_execution" / "interventions.json").read_text())
    assert entry["runs"] == ["cfgA/0"]


def test_a_batch_whose_every_job_lost_a_container_still_fails(monkeypatch, tmp_path):
    """Not a flake but a fault they share -- a missing world file, an image that cannot run
    here. Carrying on would spend the rest of the budget producing cells with no sample."""
    from robovast.execution.backends import CampaignConfigError

    runner, _ = _restart_runner(
        monkeypatch, tmp_path, [_job(0, "cfgA"), _job(1, "cfgA")],
        {"rrroqs-x-0": _SUT_CRASH, "rrroqs-x-1": _SUT_CRASH})

    with pytest.raises(CampaignConfigError, match="every job in batch"):
        runner.run_batch_in_pod(str(tmp_path))


def test_a_single_job_batch_is_exempt_from_that(monkeypatch, tmp_path):
    """One flake is 100% of one job. A pilot must not be reclassified as a systematic
    fault by arithmetic."""
    runner, _ = _restart_runner(
        monkeypatch, tmp_path, [_job(0, "cfgA")], {"rrroqs-x-0": _SUT_CRASH})
    runner.run_batch_in_pod(str(tmp_path))  # must NOT raise
    assert runner.k8s_batch_client.deleted == ["rrroqs-x-0"]


# --- A pod that never started invalidates its trial, not the campaign ----------------
#
# The other way a job fails to deliver, and until this it was fatal: two jobs of
# thirty-five rate-limited on their image pull ends a long search mid-flight
#. Every job of a batch runs the same images
# with the same reservation, so a cause in the CONFIGURATION blocks all of them and still
# fails fast; a cause that blocks only some is the cluster, and those jobs are dropped.

def _blocked_runner(monkeypatch, tmp_path, jobs, blocked, *, contended=None,
                    remaining_after=(), blocked_grace=0.0, contended_grace=0.0):
    """A runner whose wait loop sees *blocked* on its first poll, with graces it can
    reach: zero means "already expired", so one poll decides."""
    storage = _FakeStorage()
    monkeypatch.setattr(in_pod_storage, "storage_client_for", lambda cfg: storage)
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.kubernetes_backend.prepare_campaign_configs",
        lambda out_dir, data, cluster=False, instance_type_command=None: None)
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.kubernetes_backend.restarted_job_forensics",
        lambda core, ns, label, job_names=None: {})
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.kubernetes_backend"
        ".blocked_and_contended_reasons",
        lambda core, ns, label: (dict(blocked), dict(contended or {})))
    # See the note in _restart_runner: names are derived, not read off the manifest.
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.kubernetes_backend._short_job_name",
        lambda campaign, tag, index: f"rrroqs-x-{index}")

    runner = _runner_for_download_test([{"name": "cfgA"}])
    runner.namespace = "ns"
    runner.k8s_client = object()
    runner.k8s_batch_client = _FakeBatchClient()
    runner._build_jobs = lambda: jobs
    runner.create_job_manifest = lambda job, total, node_figures=None: {
        "metadata": {"name": f"rrroqs-x-{job.index}"}}
    runner._BLOCKED_GRACE_SECONDS = blocked_grace
    runner._CONTENDED_GRACE_SECONDS = contended_grace
    polls = [list(remaining_after), []]
    runner.get_remaining_jobs = lambda names: polls.pop(0) if polls else []
    return runner, storage


_THROTTLED = "ErrImagePull: pull QPS exceeded"


def test_a_blocked_job_is_dropped_and_the_batch_continues(monkeypatch, tmp_path):
    """The point of the change: one job goes, the batch drains around the hole -- the
    same seam a restarted job leaves through."""
    runner, _ = _blocked_runner(
        monkeypatch, tmp_path, [_job(0, "cfgA"), _job(1, "cfgA"), _job(2, "cfgA")],
        {"rrroqs-x-0": _THROTTLED},
        remaining_after=["rrroqs-x-0", "rrroqs-x-1", "rrroqs-x-2"])

    runner.run_batch_in_pod(str(tmp_path))  # must NOT raise

    assert runner.k8s_batch_client.deleted == ["rrroqs-x-0"]


def test_a_dropped_blocked_job_is_recorded_with_kubernetes_own_reason(monkeypatch,
                                                                     tmp_path):
    """A discarded trial must be visible as discarded, and say what stopped it."""
    import json

    runner, _ = _blocked_runner(
        monkeypatch, tmp_path, [_job(0, "cfgA"), _job(1, "cfgA")],
        {"rrroqs-x-0": _THROTTLED}, remaining_after=["rrroqs-x-0", "rrroqs-x-1"])
    runner.run_batch_in_pod(str(tmp_path))

    entry, = json.loads((tmp_path / "_execution" / "interventions.json").read_text())
    assert entry["kind"] == "invalid"
    assert entry["source"] == "runner"
    assert entry["job_dir"] == "_jobs/batch-0/job-0"
    assert entry["runs"] == ["cfgA/0"]
    assert "never started" in entry["detail"] and "pull QPS exceeded" in entry["detail"]


def test_a_whole_batch_that_cannot_start_still_fails_fast(monkeypatch, tmp_path):
    """Every job of a batch runs the same images with the same reservation, so a whole
    batch blocked is the campaign, not the cluster -- and no batch of it will ever run."""
    from robovast.execution.backends import CampaignConfigError

    runner, _ = _blocked_runner(
        monkeypatch, tmp_path, [_job(0, "cfgA"), _job(1, "cfgA")],
        {"rrroqs-x-0": "ErrImagePull: manifest unknown",
         "rrroqs-x-1": "ErrImagePull: manifest unknown"},
        remaining_after=["rrroqs-x-0", "rrroqs-x-1"])

    with pytest.raises(CampaignConfigError, match="none of this batch"):
        runner.run_batch_in_pod(str(tmp_path))
    assert runner.k8s_batch_client.deleted == []


def test_each_blocked_job_gets_its_own_tolerance(monkeypatch, tmp_path):
    """Per job, not per batch. One shared timer had to pick the shortest, so a job merely
    waiting its turn was failed on the tolerance meant for a job that never will."""
    runner, _ = _blocked_runner(
        monkeypatch, tmp_path, [_job(0, "cfgA"), _job(1, "cfgA"), _job(2, "cfgA")],
        {"rrroqs-x-0": "ErrImagePull: manifest unknown", "rrroqs-x-1": _THROTTLED},
        contended={"rrroqs-x-1": _THROTTLED},
        remaining_after=["rrroqs-x-0", "rrroqs-x-1", "rrroqs-x-2"],
        blocked_grace=0.0, contended_grace=900.0)

    runner.run_batch_in_pod(str(tmp_path))

    assert runner.k8s_batch_client.deleted == ["rrroqs-x-0"]


def test_a_blocked_job_inside_its_grace_is_left_alone(monkeypatch, tmp_path):
    """A blip must cost nothing at all: nothing dropped, nothing raised."""
    runner, _ = _blocked_runner(
        monkeypatch, tmp_path, [_job(0, "cfgA"), _job(1, "cfgA")],
        {"rrroqs-x-0": _THROTTLED}, remaining_after=["rrroqs-x-0", "rrroqs-x-1"],
        blocked_grace=900.0, contended_grace=900.0)

    runner.run_batch_in_pod(str(tmp_path))

    assert runner.k8s_batch_client.deleted == []
    assert not (tmp_path / "_execution" / "interventions.json").exists()


def test_a_batch_whose_every_job_was_dropped_still_fails(monkeypatch, tmp_path):
    """The backstop for dropping one at a time: losing part of a batch is survivable,
    losing all of it is a verdict -- whatever mix of causes got it there."""
    from robovast.execution.backends import CampaignConfigError

    runner, _ = _blocked_runner(
        monkeypatch, tmp_path, [_job(0, "cfgA"), _job(1, "cfgA")],
        {"rrroqs-x-0": _THROTTLED},
        remaining_after=["rrroqs-x-0", "rrroqs-x-1"])
    # The second job is lost the other way, after the first was dropped for its pull.
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.kubernetes_backend.restarted_job_forensics",
        lambda core, ns, label, job_names=None: {"rrroqs-x-1": _SUT_CRASH})

    with pytest.raises(CampaignConfigError, match="every job in batch"):
        runner.run_batch_in_pod(str(tmp_path))


# --- Every container says which image bytes it wants, and how hard to look -----------
#
# Kubernetes defaults imagePullPolicy to IfNotPresent -- except for a `:latest` tag, where
# it silently becomes Always. The campaign image is a floating `:latest` in the ordinary
# case, so all four containers of all 35 pods of a batch re-contacted the registry on every
# start for an image the node already had: ~140 round trips in one instant against a
# kubelet limited to 5/s. `ErrImagePull: pull QPS exceeded` was arithmetic, not a blip.

_TAG = "repo.example.com/robovast:latest"
_DIGEST = "repo.example.com/robovast@sha256:" + "cd" * 32


def _fake_cluster_config():
    return types.SimpleNamespace(
        get_s3_endpoint=lambda: "http://s3.example.com",
        get_s3_credentials=lambda: ("ak", "sk"),
        get_host_aliases=lambda: [],
        get_registry_config=lambda: types.SimpleNamespace(
            pull_secret_name="", push_secret_name="", insecure=False,
            ca_configmap_name=""),
    )


def _pinning_runner(monkeypatch, digest, *, cache=None, calls=None):
    """A runner built the real way (`for_batch`), with the registry answering *digest*."""
    def _digest(ref, **kw):
        if calls is not None:
            calls.append(ref)
        return digest(ref) if callable(digest) else digest
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.registry_client.manifest_digest", _digest)
    # Unrelated to what these test, and it costs a list_node() that waits out its timeout
    # wherever there is no cluster to answer it.
    monkeypatch.setattr(BatchJobRunner, "_discover_gpu_support",
                        lambda self: (setattr(self, "_gpu_capacity", 0),
                                      setattr(self, "_gpu_runtime_class", None)))
    return BatchJobRunner.for_batch(
        campaign_data={"configs": [{"name": "cfgA"}], "execution": {}},
        campaign_id="camp-2026-08-24-000000", batch_tag="batch-0", runs=1,
        cluster_config=_fake_cluster_config(), namespace="ns", image=_TAG,
        image_digest_cache=cache)


def _containers_of(manifest):
    spec = manifest["spec"]["template"]["spec"]
    return list(spec.get("initContainers") or []) + list(spec.get("containers") or [])


def test_a_tagged_ref_is_pulled_always():
    """A name can be re-pushed under us, so it has to be re-checked."""
    from robovast.execution.cluster_execution.kubernetes_backend import pull_policy_for
    assert pull_policy_for(_TAG) == "Always"
    assert pull_policy_for("repo.example.com/robovast:v2") == "Always"
    assert pull_policy_for("") == "Always"


def test_a_digest_ref_is_pulled_only_when_absent():
    """A digest names the bytes, so a cached image cannot be the wrong one."""
    from robovast.execution.cluster_execution.kubernetes_backend import pull_policy_for
    assert pull_policy_for(_DIGEST) == "IfNotPresent"


def test_every_container_of_a_scenario_pod_states_its_pull_policy(monkeypatch):
    """Never left to the default: that default is what depends on the tag reading
    'latest', which is the whole defect."""
    runner = _pinning_runner(monkeypatch, "")     # registry silent: refs stay tags
    containers = _containers_of(runner.manifest)

    assert containers, "a scenario pod has containers"
    for container in containers:
        assert container.get("imagePullPolicy"), \
            f"container {container['name']} left the pull policy to Kubernetes"


def test_a_pinned_campaign_pulls_only_what_the_node_lacks(monkeypatch):
    """The fix end to end: with the registry answering, every container runs a digest ref
    and none of them re-contacts the registry for an image the node already has."""
    runner = _pinning_runner(monkeypatch, _DIGEST)

    for container in _containers_of(runner.manifest):
        assert "@sha256:" in container["image"], container["name"]
        assert container["imagePullPolicy"] == "IfNotPresent", container["name"]


def test_a_registry_that_will_not_answer_leaves_the_campaign_runnable(monkeypatch):
    """Fail-soft: a campaign must not fail to start because an optimisation could not be
    applied. The ref stays as it was -- which is what would have run anyway -- and keeps
    the policy that is correct for a name that may move."""
    runner = _pinning_runner(monkeypatch, "")

    assert runner.plan.main.image in (None, _TAG)
    for container in _containers_of(runner.manifest):
        assert "@sha256:" not in container["image"]
        assert container["imagePullPolicy"] == "Always"


def test_the_digest_is_asked_for_once_per_campaign_not_once_per_batch(monkeypatch):
    """A 50-batch search must not ask the registry 50 times for an answer that must not
    change between batches -- the cache is also what keeps the campaign on one image."""
    calls, cache = [], {}
    for _ in range(3):                      # three batches of one campaign
        _pinning_runner(monkeypatch, _DIGEST, cache=cache, calls=calls)

    assert calls, "the registry was asked at least once"
    assert len(calls) == len(set(calls)), f"asked the registry twice for one ref: {calls}"


# -- a job tag must stay flat, even when the batch tag is not -----------------

def _tag_for(batch_tag, index=0):
    """``_job_tag`` on a bare runner -- it reads only ``_batch_tag``."""
    runner = BatchJobRunner.__new__(BatchJobRunner)
    runner._batch_tag = batch_tag
    return runner._job_tag(index)


def test_a_batched_job_tag_is_flat():
    assert _tag_for("batch-3", 2) == "batch-3-job-2"


def test_an_unbatched_job_tag_is_just_the_index():
    assert _tag_for("", 2) == "job-2"


def test_a_repetitions_group_tag_does_not_leak_a_slash():
    """``_job_tag`` promises a "flat, slash-free" tag and did not enforce it.

    A batch whose parameter sets ask for different repetition counts is tagged
    ``batch-<n>/reps-<k>`` -- the grouping is real and the slash is deliberate there. But
    this tag names two things that cannot contain one: the ``<tag>.params.yaml`` file, where
    the slash became an unmade directory (`FileNotFoundError` on
    ``_transient/batch-1/reps-3-job-0.params.yaml``, which killed the campaign before its
    first run), and the Kubernetes Job name, where a slash is not a legal DNS-1123 label.
    One cause, two failures, and only reachable once repetitions stopped being uniform.
    """
    tag = _tag_for("batch-1/reps-3", 0)
    assert "/" not in tag, f"slash leaked into a job tag: {tag!r}"
    assert tag == "batch-1-reps-3-job-0"


def test_two_repetition_groups_in_one_batch_get_distinct_tags():
    """Flattening must not collapse them onto one name -- they are different jobs, and the
    params file and Job name are keyed on this."""
    assert _tag_for("batch-1/reps-3", 0) != _tag_for("batch-1/reps-5", 0)


def test_the_flattened_tag_is_a_legal_kubernetes_label():
    import re
    tag = _tag_for("batch-12/reps-5", 7)
    assert re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", tag), tag


def test_calibration_stated_in_the_vast_reaches_the_allocation():
    """A stated `calibration` block must actually size the container.

    It arrives from the plan as the mapping the `.vast` wrote, not as a model, so reading
    it with ``getattr`` alone resolved every field to ``None`` and fell back to the role
    defaults. The block validated, so the file read as configured while the allocation was
    the default one -- and nothing said so. Measured on a real campaign: a simulator whose
    peak was 181 MiB was held at 256 MiB (181 x the default 1.25) and OOM-killed on the
    heavier configurations, with `headroom.memory: 10.0` stated in the file.
    """
    from robovast.common.containers import plan_containers
    from robovast.execution.cluster_execution.kubernetes_backend import \
        calibrated_resources

    plan = plan_containers({"containers": {
        "scenario": {"image": "a", "resources": {"cpu": 2, "memory": "1Gi"},
                     "calibration": {"size_on": 99, "headroom": {"memory": 10.0}}},
        "simulation": {"image": "b", "resources": {"cpu": 2, "memory": "2Gi"},
                       "calibration": {"headroom": {"memory": 10.0}}},
    }})
    runner = object.__new__(BatchJobRunner)
    runner.plan = plan

    resolved = runner._calibration_by_container()  # noqa: SLF001 - the unit under test
    assert resolved["scenario"]["size_on"] == 99, "size_on fell back to the role default"
    assert resolved["simulation"]["headroom"]["memory"] == 10.0
    # Stating only memory keeps the cpu default rather than losing it.
    assert resolved["simulation"]["headroom"]["cpu"] == 1.25

    sized = calibrated_resources(
        {"cpu": 2, "memory": "2Gi"}, "simulation",
        {"simulation": {"memory_peak": 181 * 1024 * 1024}},
        roles=("simulation",), bootstrap=True, settings=resolved["simulation"])

    assert int(sized["memory"]) > 256 * 1024 * 1024, "still sized at the default headroom"
    # Never above what the author declared: calibration sizes down, it does not raise a ceiling.
    assert int(sized["memory"]) <= 2 * 1024 ** 3
