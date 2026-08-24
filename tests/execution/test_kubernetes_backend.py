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
    r._verify_admission_path = lambda: None  # no cluster to check the Kueue queues on
    r._ensure_priority_class = lambda: None  # nor to create the campaign's priority class
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
# whole campaign. One sidecar crash in one job of one batch ended a 50-batch search and
# orphaned the two batches that had already finished (rr-roqsim-full-2026-08-23-03124069).

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

    runner = _runner_for_download_test([{"name": "cfgA"}])
    runner.namespace = "ns"
    runner.k8s_client = object()
    runner.k8s_batch_client = _FakeBatchClient()
    runner._build_jobs = lambda: jobs
    runner.create_job_manifest = lambda job, total: {
        "metadata": {"name": f"rrroqs-x-{job.index}"}}
    polls = [list(remaining_after), []]
    runner.get_remaining_jobs = lambda names: polls.pop(0) if polls else []
    runner._report_suspended_jobs = lambda remaining: None
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
