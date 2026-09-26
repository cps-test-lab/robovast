# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A batch whose Jobs cannot be created fails the campaign; it does not finish it.

The queue gives up on a create that keeps raising -- a webhook, an RBAC change, a quota, a
manifest the API server rejects -- and drops the item. Read only as "nothing planned, nothing
running", that batch was over, and the campaign finished with every run recorded as having
produced nothing. What was given up on is a verdict the runner raises with the cause.
"""

import pytest

from robovast.execution.backends import CampaignConfigError
from robovast.execution.cluster_execution import admitted_jobs, node_admission
from robovast.execution.cluster_execution.admitted_jobs import AdmittedJobs
from robovast.execution.cluster_execution.node_admission import (AdmissionController,
                                                                 JobSizing)
from tests.execution.test_admission_controller import MIB, FakeProvider
from tests.execution.test_kubernetes_backend import (_FakeBatchClient, _job,
                                                     _no_config_preparation,
                                                     _runner_for_batch_test)

_TOKEN = "tok"


def _boom(_node=None):
    raise RuntimeError("admission webhook denied the request")


def test_the_queue_keeps_what_it_gave_up_on_until_the_owner_is_cancelled():
    c = AdmissionController(FakeProvider(), clock=lambda: 0.0)
    c.submit("a", [("a-0", JobSizing(1.0, MIB), _boom)], started_at=0.0)
    for _ in range(node_admission.CREATE_ATTEMPT_LIMIT):
        c.drain()

    assert list(c.given_up("a")) == ["a-0"]
    assert "admission webhook denied" in c.given_up("a")["a-0"]
    c.cancel("a")
    assert c.given_up("a") == {}


def test_a_round_reports_what_the_queue_gave_up_on():
    c = AdmissionController(FakeProvider(), clock=lambda: 0.0)
    tracker = AdmittedJobs(admission=c, owner="a", batch_api=None, core_api=None,
                           namespace="ns", label_selector="x",
                           list_remaining=lambda names: [])
    tracker._read_blocked = lambda: ({}, {})
    tracker.submit([("a-0", JobSizing(1.0, MIB), _boom)], started_at=0.0)
    for _ in range(node_admission.CREATE_ATTEMPT_LIMIT - 1):
        rnd = tracker.poll()
        assert rnd.given_up == {} and not rnd.over

    rnd = tracker.poll()
    assert list(rnd.given_up) == ["a-0"]


def test_a_batch_that_could_create_nothing_fails_naming_the_cause(monkeypatch, tmp_path):
    _no_config_preparation(monkeypatch)
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.kubernetes_backend._short_job_name",
        lambda campaign, tag, index: f"j-{index}")
    monkeypatch.setattr(admitted_jobs, "blocked_and_contended_reasons",
                        lambda core, ns, label: ({}, {}))

    class _Refusing(_FakeBatchClient):
        def create_namespaced_job(self, namespace, body):
            raise RuntimeError("admission webhook denied the request")

    runner = _runner_for_batch_test([{"name": "cfgA"}])
    runner.k8s_batch_client = _Refusing()
    runner._build_jobs = lambda: [_job(0, "cfgA"), _job(1, "cfgA")]
    runner.create_job_manifest = lambda job, total, node_figures=None: {
        "metadata": {"name": f"j-{job.index}"}}
    runner._job_sizing = lambda job, total, node_figures=None: JobSizing(1.0, MIB)
    runner._start_probes = lambda jobs, total: None
    runner._campaign_node_id = lambda: None
    runner.admission = AdmissionController(FakeProvider(), clock=lambda: 0.0)
    runner._poll_wait = lambda seconds: None
    # The probe and forensics steps of the wait loop read pods this test has none of.
    runner._probes = {}
    runner._calibration = None
    for step in ("_fail_on_crashed_probes", "_collect_probes", "_publish_space_wait",
                 "_publish_capacity_wait", "drop_probes_with_no_work_left_to_size",
                 "_invalidate_restarted_jobs", "_record_oom_kills_at_measured_figures"):
        monkeypatch.setattr(runner, step, lambda *a, **k: None)

    with pytest.raises(CampaignConfigError, match="admission webhook denied"):
        runner.run_batch_in_pod(str(tmp_path), _TOKEN)
