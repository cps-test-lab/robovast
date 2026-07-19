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
from robovast.execution.cluster_execution.kubernetes_backend import (
    BatchJobRunner, KubernetesBackend)


class _FakeStorage:
    """Records download_prefix / upload_dir calls; no I/O."""

    def __init__(self):
        self.downloads = []

    def download_prefix(self, bucket, prefix, local_dir, force=False):
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
    r._write_job_param_files = lambda out_dir: None
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
        lambda out_dir, data, cluster=False: None)

    runner = _runner_for_download_test([{"name": "cfgA"}, {"name": "cfgB"}])
    runner.run_batch_in_pod(str(tmp_path))

    assert storage.downloads == ["cfgA", "cfgB", "_config", "_transient",
                                 "_jobs/batch-0"]


def test_run_batch_in_pod_materialises_job_symlinks(monkeypatch, tmp_path):
    """Each run's ``job`` symlink is created so metadata resolves sysinfo.yaml now.

    In the cluster flow the symlinks were only materialised at upload-to-share,
    after the driver's own metadata/postprocessing had already run.
    """
    storage = _FakeStorage()
    monkeypatch.setattr(in_pod_storage, "storage_client_for", lambda cfg: storage)
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.kubernetes_backend.prepare_campaign_configs",
        lambda out_dir, data, cluster=False: None)

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
        lambda out_dir, data, cluster=False: None)

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
        "robovast.execution.cluster_execution.kubernetes_backend.resolve_robovast_image",
        lambda **kw: "img:test")
    monkeypatch.setattr(
        BatchJobRunner, "for_batch",
        classmethod(lambda cls, **kw: types.SimpleNamespace(
            run_batch_in_pod=lambda campaign_root: None)))

    _backend().run_batch({"execution": {"image": "img:test"}},
                         campaign_root=str(tmp_path), batch_tag="b", runs=3,
                         options=RunOptions())

    assert len(calls) == 1
    runs, out, kw = calls[0]
    assert runs == 3 and out == str(tmp_path)
    assert kw["execution_params"] == {"image": "img:test"}


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
