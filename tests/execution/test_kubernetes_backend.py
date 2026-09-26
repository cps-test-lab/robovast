# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for KubernetesBackend's campaign_root completion.

The auto-chained analysis postprocessing runs against ``campaign_root`` *before*
``finalize_campaign``, so the backend must leave it complete after ``run_batch``: the
run results each Job's uploader delivered, the campaign-level tree ``run_batch_in_pod``
prepared into it, and ``_execution/execution.yaml`` recorded — exactly what the local
(Docker) backend leaves via ``run.sh``.
"""

import types

import pytest

from robovast.execution.backends import CampaignConfigError, RunOptions
from robovast.execution.cluster_execution.registry_client import UNKNOWN
from robovast.execution.cluster_execution.kubernetes_backend import (BatchJobRunner,
                                                                     KubernetesBackend)
from robovast.execution.control_server import ControllerState
from robovast.execution.jobs import Job

#: What the service hands the backend for the campaign's pods to authenticate with.
_TOKEN = "scoped-token"


class _FakeCore:
    """The CoreV1Api calls a batch makes: the campaign's token Secret."""

    def __init__(self):
        self.secrets = []

    def create_namespaced_secret(self, namespace, body):
        self.secrets.append(body)


def _runner_for_batch_test(configs):
    """A BatchJobRunner stubbed down to the steps that bracket the wait loop."""
    r = BatchJobRunner()
    r.cluster_config = object()
    r.namespace = "ns"
    r.campaign = "camp-2026-07-17-120000"
    r.configs = configs
    r._batch_tag = "batch-0"
    r.campaign_data = {"execution": {}}
    # Stub every side-effecting step so only the batch's own bookkeeping runs.
    r._ensure_k8s_initialized = lambda: None
    r.k8s_client = _FakeCore()
    r.k8s_batch_client = _FakeBatchClient()
    r._write_job_param_files = lambda out_dir, campaign_root=None: None
    r._build_jobs = lambda: []          # no jobs → submission loop is empty
    r.get_remaining_jobs = lambda names: []  # wait loop breaks immediately
    r._write_job_links = lambda cr: None
    r.cleanup_jobs = lambda campaign=None: None
    r.cleanup_pods = lambda campaign=None: None
    return r


def _no_config_preparation(monkeypatch):
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.kubernetes_backend.prepare_campaign_configs",
        lambda out_dir, data, cluster=False, instance_type_command=None: None)


def test_the_campaign_root_is_where_the_configs_are_prepared(monkeypatch, tmp_path):
    """The Jobs' init containers fetch the campaign's inputs from the campaign, so the
    tree they fetch is written into the campaign root itself and nowhere else."""
    prepared = []
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.kubernetes_backend.prepare_campaign_configs",
        lambda out_dir, data, cluster=False, instance_type_command=None:
            prepared.append((out_dir, cluster)))

    runner = _runner_for_batch_test([{"name": "cfgA"}])
    runner.run_batch_in_pod(str(tmp_path), _TOKEN)

    assert prepared == [(str(tmp_path), True)]


def test_the_campaigns_token_secret_exists_before_its_first_job(monkeypatch, tmp_path):
    """A Job's pod reads the token out of a Secret, so the Secret has to be there before
    any Job is created -- a pod whose secretKeyRef does not resolve never starts."""
    _no_config_preparation(monkeypatch)

    runner = _runner_for_batch_test([{"name": "cfgA"}])
    runner.run_batch_in_pod(str(tmp_path), _TOKEN)

    secret, = runner.k8s_client.secrets
    assert secret["stringData"]["token"] == _TOKEN


def test_run_batch_in_pod_materialises_job_symlinks(monkeypatch, tmp_path):
    """Each run's ``job`` symlink is created at the end of the batch, so the driver's own
    metadata and postprocessing resolve ``<run>/job/sysinfo.yaml`` -- as in a local run --
    rather than only the archive writer seeing them."""
    _no_config_preparation(monkeypatch)

    runner = _runner_for_batch_test([{"name": "cfgA"}])
    # Seed the job-links manifest create_job_links reads; the no-op _write_job_links
    # stub leaves it intact.
    transient = tmp_path / "_transient"
    transient.mkdir(parents=True)
    (transient / "job_links.yaml").write_text(
        "cfgA/0/job: ../../_jobs/batch-0/job-0\n")

    runner.run_batch_in_pod(str(tmp_path), _TOKEN)

    link = tmp_path / "cfgA" / "0" / "job"
    assert link.is_symlink()
    assert (tmp_path / "cfgA" / "0" / "job").readlink().name == "job-0"


def test_run_batch_in_pod_aborts_cleanly_on_stop(monkeypatch, tmp_path):
    """A cooperative stop abandons the batch with CampaignStopped rather than finishing
    it: the jobs are being torn down, and what they left is what the campaign has.

    Before anything is staged, too. A stop that reached the campaign while it was
    starting used to be read first in the wait loop, so the batch it was stopping was
    staged and submitted in full and only then abandoned -- creating exactly the Jobs the
    service had just torn down.
    """
    from robovast.execution.backends import CampaignStopped

    prepared = []
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.kubernetes_backend.prepare_campaign_configs",
        lambda out_dir, data, cluster=False, instance_type_command=None:
            prepared.append(out_dir))

    runner = _runner_for_batch_test([{"name": "cfgA"}])
    runner._state = ControllerState()
    runner._state.request_stop()

    with pytest.raises(CampaignStopped):
        runner.run_batch_in_pod(str(tmp_path), _TOKEN)
    assert prepared == [], "a stopped campaign staged its configs anyway"
    # The batch never reached its tail, so it linked nothing up.
    assert not (tmp_path / "cfgA").exists()


def test_a_stopped_batch_sweeps_up_jobs_created_after_the_teardown(monkeypatch, tmp_path):
    """The service tore this campaign's Jobs down when the stop landed; a drain between
    that and the loop's next read can have created more, and nothing else would.

    Scoped to the campaign, so another campaign's work and a shared image build -- which
    a sibling may still be waiting on -- are untouched.
    """
    from robovast.execution.backends import CampaignStopped

    swept = []
    backend = _backend()
    backend._state = ControllerState()
    backend._state.request_stop()

    class _Runner:
        _sidecar_image = None

        def run_batch_in_pod(self, campaign_root, token):
            raise CampaignStopped("stopped")

        def cleanup_jobs(self, campaign=None):
            swept.append(("jobs", campaign))

        def cleanup_pods(self, campaign=None):
            swept.append(("pods", campaign))

    _stub_runner(monkeypatch, backend, _Runner())

    with pytest.raises(CampaignStopped):
        backend.run_batch({}, campaign_root=str(tmp_path / "camp-1"),
                          batch_tag="batch-0", runs=1, options=RunOptions())

    assert swept == [("jobs", "camp-1"), ("pods", "camp-1")]


def test_a_batch_nobody_stopped_sweeps_nothing(monkeypatch, tmp_path):
    """The ordinary end of a batch already cleans up after itself; sweeping again here
    would delete a search's next generation out from under it."""
    swept = []
    backend = _backend()

    class _Runner:
        _sidecar_image = None

        def run_batch_in_pod(self, campaign_root, token):
            return None

        def cleanup_jobs(self, campaign=None):
            swept.append("jobs")

        def cleanup_pods(self, campaign=None):
            swept.append("pods")

    _stub_runner(monkeypatch, backend, _Runner())

    backend.run_batch({}, campaign_root=str(tmp_path / "camp-1"),
                      batch_tag="batch-0", runs=1, options=RunOptions())

    assert swept == []


def _backend():
    return KubernetesBackend(cluster_config=object(), namespace="ns",
                             kube_context=None, data_token=_TOKEN)


def _stub_runner(monkeypatch, backend, runner):
    """Put *runner* behind ``run_batch``, with the records it writes around it stubbed.

    The records are a batch's own bookkeeping -- the launch images and execution.yaml --
    and they need a real plan to write; a test about what the batch does on its way out
    has none and is not about them.
    """
    monkeypatch.setattr(BatchJobRunner, "for_batch",
                        classmethod(lambda cls, **kw: runner))
    for name in ("_record_launch_images", "_record_execution_yaml"):
        monkeypatch.setattr(type(backend), name, lambda *a, **k: None)


def test_run_batch_records_execution_yaml_before_finalize(monkeypatch, tmp_path):
    """execution.yaml is written in run_batch (so postprocess can read the image).

    Twice: once as soon as the plan is pinned and again after the jobs. The digests are known
    at the first point and this file is the only place they are recorded, so written only at
    the end a campaign that died during its first batch named none of the images it ran --
    which is the ordinary shape of a failure on a cluster. The writer is idempotent, which is
    what makes writing it twice free.
    """
    calls = []
    monkeypatch.setattr("robovast.common.execution.create_execution_yaml",
                        lambda runs, out, **kw: calls.append((runs, out, kw)))
    monkeypatch.setattr(
        BatchJobRunner, "for_batch",
        classmethod(lambda cls, **kw: types.SimpleNamespace(
            run_batch_in_pod=lambda campaign_root, token: None,
            # What the record asks a real runner for: which machines the campaign left out.
            skipped_nodes=dict, _sidecar_image=None)))

    # The declared image is enough: resolution needs no env when the campaign names one.
    _backend().run_batch(
        {"execution": {"containers": {"scenario": {"image": "img:test"}}}},
        campaign_root=str(tmp_path), batch_tag="b", runs=3, options=RunOptions())

    assert len(calls) == 2, "written once before the jobs and once after"
    for runs, out, kw in calls:
        assert runs == 3 and out == str(tmp_path)
        assert kw["execution_params"] == {"containers": {"scenario": {"image": "img:test"}}}


def test_finalize_no_longer_records_execution_yaml(monkeypatch, tmp_path):
    """finalize releases what the cluster held — execution.yaml was recorded earlier."""
    called = []
    monkeypatch.setattr("robovast.common.execution.create_execution_yaml",
                        lambda *a, **k: called.append(True))

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


def _job(index, config_name):
    """What `_build_jobs` hands the wait loop."""
    return Job(config={"name": config_name}, run_number=0, index=index)


def _restart_runner(monkeypatch, tmp_path, jobs, forensics, *, remaining_after=()):
    """A runner whose wait loop sees *forensics* on its first poll."""
    _no_config_preparation(monkeypatch)
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.kubernetes_backend.restarted_job_forensics",
        lambda core, ns, label, job_names=None: forensics)
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.kubernetes_backend.previous_container_log",
        lambda core, ns, pod, container, tail_lines=400: ("boom\ntraceback\n", "captured"))
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.admitted_jobs"
        ".blocked_and_contended_reasons", lambda core, ns, label: ({}, {}))
    # The wait loop derives a job's name rather than reading it back off a rendered manifest
    # (under admission the manifest does not exist until there is room). Patch the derivation
    # so these fixtures keep their short synthetic names.
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.kubernetes_backend._short_job_name",
        lambda campaign, tag, index: f"rrroqs-x-{index}")

    runner = _runner_for_batch_test([{"name": "cfgA"}])
    runner.k8s_batch_client = _FakeBatchClient()
    runner._build_jobs = lambda: jobs
    runner.create_job_manifest = lambda job, total, node_figures=None: {
        "metadata": {"name": f"rrroqs-x-{job.index}"}}
    polls = [list(remaining_after), []]
    runner.get_remaining_jobs = lambda names: polls.pop(0) if polls else []
    return runner


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
    runner = _restart_runner(
        monkeypatch, tmp_path, [_job(0, "cfgA"), _job(1, "cfgA")],
        {"rrroqs-x-0": _SUT_CRASH}, remaining_after=["rrroqs-x-0", "rrroqs-x-1"])

    runner.run_batch_in_pod(str(tmp_path), _TOKEN)  # must NOT raise

    assert runner.k8s_batch_client.deleted == ["rrroqs-x-0"]


def test_the_invalidated_job_is_recorded_in_the_ledger(monkeypatch, tmp_path):
    """A discarded trial must be visible as discarded, not merely absent."""
    import json

    runner = _restart_runner(
        monkeypatch, tmp_path, [_job(0, "cfgA"), _job(1, "cfgA")],
        {"rrroqs-x-0": _SUT_CRASH}, remaining_after=["rrroqs-x-0", "rrroqs-x-1"])
    runner.run_batch_in_pod(str(tmp_path), _TOKEN)

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

    runner = _restart_runner(
        monkeypatch, tmp_path, [_job(0, "cfgA"), _job(1, "cfgA")],
        {"rrroqs-x-0": _SUT_CRASH}, remaining_after=["rrroqs-x-0", "rrroqs-x-1"])
    runner.run_batch_in_pod(str(tmp_path), _TOKEN)

    record, = json.loads(
        (tmp_path / "_execution" / "container_failures.json").read_text())
    assert record["signal_name"] == "SIGBUS"
    assert record["exit_code"] == 135
    assert record["node_name"] == "a-node"
    assert record["memory_limit"] is None      # the absence IS the finding
    assert record["log_status"] == "captured"
    assert "traceback" in record["log_tail"]
    assert record["runs"] == ["cfgA/0"]


def test_a_job_is_invalidated_only_once(monkeypatch, tmp_path):
    """A restart is reported on every poll until the pod is gone, and deleting a Job is
    asynchronous -- so without the guard one crash is recorded on every pass."""
    import json

    runner = _restart_runner(
        monkeypatch, tmp_path, [_job(0, "cfgA"), _job(1, "cfgA")],
        {"rrroqs-x-0": _SUT_CRASH},
        remaining_after=["rrroqs-x-0", "rrroqs-x-1"])
    runner.run_batch_in_pod(str(tmp_path), _TOKEN)

    assert runner.k8s_batch_client.deleted == ["rrroqs-x-0"]
    assert len(json.loads(
        (tmp_path / "_execution" / "interventions.json").read_text())) == 1


def test_a_restart_seen_after_the_last_job_finished_still_lands(monkeypatch, tmp_path):
    """The wait loop breaks on an empty `remaining` BEFORE it probes, so a crash in the
    last job's last seconds was never observed at all."""
    import json

    runner = _restart_runner(
        monkeypatch, tmp_path, [_job(0, "cfgA")], {"rrroqs-x-0": _SUT_CRASH})
    runner.run_batch_in_pod(str(tmp_path), _TOKEN)

    entry, = json.loads((tmp_path / "_execution" / "interventions.json").read_text())
    assert entry["runs"] == ["cfgA/0"]


def test_a_batch_whose_every_job_lost_a_container_still_fails(monkeypatch, tmp_path):
    """Not a flake but a fault they share -- a missing world file, an image that cannot run
    here. Carrying on would spend the rest of the budget producing cells with no sample."""

    runner = _restart_runner(
        monkeypatch, tmp_path, [_job(0, "cfgA"), _job(1, "cfgA")],
        {"rrroqs-x-0": _SUT_CRASH, "rrroqs-x-1": _SUT_CRASH})

    with pytest.raises(CampaignConfigError, match="every job in batch"):
        runner.run_batch_in_pod(str(tmp_path), _TOKEN)


def test_a_single_job_batch_is_exempt_from_that(monkeypatch, tmp_path):
    """One flake is 100% of one job. A pilot must not be reclassified as a systematic
    fault by arithmetic."""
    runner = _restart_runner(
        monkeypatch, tmp_path, [_job(0, "cfgA")], {"rrroqs-x-0": _SUT_CRASH})
    runner.run_batch_in_pod(str(tmp_path), _TOKEN)  # must NOT raise
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
    _no_config_preparation(monkeypatch)
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.kubernetes_backend.restarted_job_forensics",
        lambda core, ns, label, job_names=None: {})
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.admitted_jobs"
        ".blocked_and_contended_reasons",
        lambda core, ns, label: (dict(blocked), dict(contended or {})))
    # See the note in _restart_runner: names are derived, not read off the manifest.
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.kubernetes_backend._short_job_name",
        lambda campaign, tag, index: f"rrroqs-x-{index}")

    runner = _runner_for_batch_test([{"name": "cfgA"}])
    runner.k8s_batch_client = _FakeBatchClient()
    runner._build_jobs = lambda: jobs
    runner.create_job_manifest = lambda job, total, node_figures=None: {
        "metadata": {"name": f"rrroqs-x-{job.index}"}}
    runner._BLOCKED_GRACE_SECONDS = blocked_grace
    runner._CONTENDED_GRACE_SECONDS = contended_grace
    polls = [list(remaining_after), []]
    runner.get_remaining_jobs = lambda names: polls.pop(0) if polls else []
    return runner


_THROTTLED = "ErrImagePull: pull QPS exceeded"


def test_a_blocked_job_is_dropped_and_the_batch_continues(monkeypatch, tmp_path):
    """The point of the change: one job goes, the batch drains around the hole -- the
    same seam a restarted job leaves through."""
    runner = _blocked_runner(
        monkeypatch, tmp_path, [_job(0, "cfgA"), _job(1, "cfgA"), _job(2, "cfgA")],
        {"rrroqs-x-0": _THROTTLED},
        remaining_after=["rrroqs-x-0", "rrroqs-x-1", "rrroqs-x-2"])

    runner.run_batch_in_pod(str(tmp_path), _TOKEN)  # must NOT raise

    assert runner.k8s_batch_client.deleted == ["rrroqs-x-0"]


def test_a_dropped_blocked_job_is_recorded_with_kubernetes_own_reason(monkeypatch,
                                                                     tmp_path):
    """A discarded trial must be visible as discarded, and say what stopped it."""
    import json

    runner = _blocked_runner(
        monkeypatch, tmp_path, [_job(0, "cfgA"), _job(1, "cfgA")],
        {"rrroqs-x-0": _THROTTLED}, remaining_after=["rrroqs-x-0", "rrroqs-x-1"])
    runner.run_batch_in_pod(str(tmp_path), _TOKEN)

    entry, = json.loads((tmp_path / "_execution" / "interventions.json").read_text())
    assert entry["kind"] == "invalid"
    assert entry["source"] == "runner"
    assert entry["job_dir"] == "_jobs/batch-0/job-0"
    assert entry["runs"] == ["cfgA/0"]
    assert "never started" in entry["detail"] and "pull QPS exceeded" in entry["detail"]


def test_a_whole_batch_that_cannot_start_still_fails_fast(monkeypatch, tmp_path):
    """Every job of a batch runs the same images with the same reservation, so a whole
    batch blocked is the campaign, not the cluster -- and no batch of it will ever run."""

    runner = _blocked_runner(
        monkeypatch, tmp_path, [_job(0, "cfgA"), _job(1, "cfgA")],
        {"rrroqs-x-0": "ErrImagePull: manifest unknown",
         "rrroqs-x-1": "ErrImagePull: manifest unknown"},
        remaining_after=["rrroqs-x-0", "rrroqs-x-1"])

    with pytest.raises(CampaignConfigError, match="none of this batch"):
        runner.run_batch_in_pod(str(tmp_path), _TOKEN)
    assert runner.k8s_batch_client.deleted == []


def test_each_blocked_job_gets_its_own_tolerance(monkeypatch, tmp_path):
    """Per job, not per batch. One shared timer had to pick the shortest, so a job merely
    waiting its turn was failed on the tolerance meant for a job that never will."""
    runner = _blocked_runner(
        monkeypatch, tmp_path, [_job(0, "cfgA"), _job(1, "cfgA"), _job(2, "cfgA")],
        {"rrroqs-x-0": "ErrImagePull: manifest unknown", "rrroqs-x-1": _THROTTLED},
        contended={"rrroqs-x-1": _THROTTLED},
        remaining_after=["rrroqs-x-0", "rrroqs-x-1", "rrroqs-x-2"],
        blocked_grace=0.0, contended_grace=900.0)

    runner.run_batch_in_pod(str(tmp_path), _TOKEN)

    assert runner.k8s_batch_client.deleted == ["rrroqs-x-0"]


def test_a_blocked_job_inside_its_grace_is_left_alone(monkeypatch, tmp_path):
    """A blip must cost nothing at all: nothing dropped, nothing raised."""
    runner = _blocked_runner(
        monkeypatch, tmp_path, [_job(0, "cfgA"), _job(1, "cfgA")],
        {"rrroqs-x-0": _THROTTLED}, remaining_after=["rrroqs-x-0", "rrroqs-x-1"],
        blocked_grace=900.0, contended_grace=900.0)

    runner.run_batch_in_pod(str(tmp_path), _TOKEN)

    assert runner.k8s_batch_client.deleted == []
    assert not (tmp_path / "_execution" / "interventions.json").exists()


def test_a_batch_whose_every_job_was_dropped_still_fails(monkeypatch, tmp_path):
    """The backstop for dropping one at a time: losing part of a batch is survivable,
    losing all of it is a verdict -- whatever mix of causes got it there."""

    runner = _blocked_runner(
        monkeypatch, tmp_path, [_job(0, "cfgA"), _job(1, "cfgA")],
        {"rrroqs-x-0": _THROTTLED},
        remaining_after=["rrroqs-x-0", "rrroqs-x-1"])
    # The second job is lost the other way, after the first was dropped for its pull.
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.kubernetes_backend.restarted_job_forensics",
        lambda core, ns, label, job_names=None: {"rrroqs-x-1": _SUT_CRASH})

    with pytest.raises(CampaignConfigError, match="every job in batch"):
        runner.run_batch_in_pod(str(tmp_path), _TOKEN)


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
        get_host_aliases=lambda: [],
        get_registry_config=lambda: types.SimpleNamespace(
            pull_secret_name="", push_secret_name="", insecure=False,
            ca_configmap_name=""),
    )


def _pinning_runner(monkeypatch, digest, *, cache=None, calls=None, state=UNKNOWN,
                    sidecar_image=None, images_fixed=False, execution=None, image=_TAG):
    """A runner built the real way (`for_batch`), with the registry answering *digest*.

    *state* is what the registry says of a ref it gave no digest for, which is what a refusal
    reports.
    """
    def _digest(ref, **kw):
        if calls is not None:
            calls.append(ref)
        return digest(ref) if callable(digest) else digest
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.registry_client.manifest_digest", _digest)
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.registry_client.manifest_state",
        lambda ref, **kw: state)
    # Unrelated to what these test, and it costs a list_node() that waits out its timeout
    # wherever there is no cluster to answer it.
    monkeypatch.setattr(BatchJobRunner, "_discover_gpu_support",
                        lambda self: (setattr(self, "_gpu_capacity", 0),
                                      setattr(self, "_gpu_runtime_class", None)))
    return BatchJobRunner.for_batch(
        campaign_data={"configs": [{"name": "cfgA"}], "execution": execution or {}},
        campaign_id="camp-2026-08-24-000000", batch_tag="batch-0", runs=1,
        cluster_config=_fake_cluster_config(), namespace="ns", image=image,
        image_digest_cache=cache, sidecar_image=sidecar_image, images_fixed=images_fixed)


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
    runner = _pinning_runner(monkeypatch, _DIGEST)
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


def test_a_registry_that_will_not_answer_refuses_the_launch(monkeypatch):
    """A tag run in place of a digest is bytes nothing recorded, and a campaign no replay can
    repeat -- so an unreadable digest refuses the launch, before any pod, naming each ref and
    what runs it rather than logging a warning and running the tag."""
    with pytest.raises(CampaignConfigError) as e:
        _pinning_runner(monkeypatch, "")

    message = str(e.value)
    # The scenario container is a planned one, so it is named as the plan names it.
    assert f"{_TAG} (container 'scenario')" in message
    assert "the sidecar" in message
    assert "did not answer" in message
    assert "before any pod was created" in message


def test_an_unreadable_digest_is_not_waived_by_the_compat_escape_hatch(monkeypatch):
    """``ROBOVAST_SKIP_IMAGE_COMPAT_CHECK`` waives the protocol label read, and nothing else."""
    monkeypatch.setenv("ROBOVAST_SKIP_IMAGE_COMPAT_CHECK", "1")
    with pytest.raises(CampaignConfigError):
        _pinning_runner(monkeypatch, "")


def test_every_planned_container_and_the_sidecar_are_fixed(monkeypatch):
    """Scenario, sut and the data-plane containers of the pod -- every image."""
    runner = _pinning_runner(
        monkeypatch, lambda ref: f"{ref.rsplit(':', 1)[0]}@sha256:{'ab' * 32}",
        execution={"containers": {"scenario": {"image": _TAG},
                                  "sut": {"image": "repo.example.com/sut:1"}}})

    assert runner.image.endswith("@sha256:" + "ab" * 32)
    assert all("@sha256:" in c.image for c in runner.plan.containers if c.image)
    assert runner._sidecar_image.endswith("@sha256:" + "ab" * 32)
    for container in _containers_of(runner.manifest):
        assert "@sha256:" in container["image"], container["name"]


def test_the_campaigns_own_sidecar_is_the_one_fixed(monkeypatch):
    """The service fixes the sidecar once per campaign, before its first pod; a batch keeps
    that one rather than the deployment's own, and asks the registry nothing about it. Which
    containers of a Job run it is ``test_job_manifest``'s: the data-plane containers are added
    per Job, so the base manifest here carries none of them."""
    calls = []
    sidecar = "repo.example.com/dev/robovast-sidecar@sha256:" + "ee" * 32
    runner = _pinning_runner(monkeypatch, _DIGEST, calls=calls, sidecar_image=sidecar)

    assert runner._sidecar_image == sidecar
    assert sidecar not in calls


def test_a_replay_asks_the_registry_nothing(monkeypatch):
    """Every ref of a replay is a recorded digest: it is its own answer."""
    calls = []
    sidecar = "repo.example.com/robovast-sidecar@sha256:" + "ee" * 32
    runner = _pinning_runner(monkeypatch, "", calls=calls, sidecar_image=sidecar,
                             images_fixed=True, image=_DIGEST)

    assert calls == []
    assert runner.image == _DIGEST and runner._sidecar_image == sidecar


def test_a_replay_refuses_a_ref_its_record_does_not_fix(monkeypatch):
    """A tag on a replay is a gap in the launch record, and resolving it now could run bytes
    the source never ran -- refused, naming it, without asking the registry."""
    calls = []
    sidecar = "repo.example.com/robovast-sidecar@sha256:" + "ee" * 32
    with pytest.raises(CampaignConfigError) as e:
        _pinning_runner(monkeypatch, _DIGEST, calls=calls, sidecar_image=sidecar,
                        images_fixed=True, image=_DIGEST,
                        execution={"containers": {"scenario": {"image": _DIGEST},
                                                  "sut": {"image": "repo.example.com/sut:1"}}})

    assert "container 'sut'" in str(e.value)
    assert "repo.example.com/sut:1" in str(e.value)
    assert calls == []


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
