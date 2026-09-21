# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A run killed by a figure this campaign MEASURED is reported while the campaign runs on.

Calibration sizes every run of a campaign from one probe's peak, so a demand the probe did not
see is not met once: the same figure meets the next run, and the next. The campaign keeps going
-- a sweep that reaches its end having lost some runs is worth more than one held halfway -- so
the only thing that makes it actionable is saying so, early, where somebody watching already
looks. That is the health channel: ``vast campaign wait`` ends on an error-level finding, and
every client already renders them.

The fault cannot be self-reported (a container that was OOM-killed is not there to say so, and
its Job is deleted moments later), so the runner records it in the campaign's ledger and the
service reads it back as a finding.
"""

from pathlib import Path

from robovast.common.campaign_data import KIND_SIZING, read_interventions, record_intervention
from robovast.execution.cluster_execution import kubernetes_backend as kb
from robovast.service.local_transport import LocalTransport
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore


def _entry(container="sut", node="n1", limit="128Mi", reason="OOMKilled"):
    """What ``restarted_job_forensics`` hands back for one killed run."""
    return {"detail": "restarted", "node": node,
            "containers": [{"container": container, "reason": reason, "exit_code": 137,
                            "memory_limit": limit, "invalidating": True}]}


def _runner(tmp_path, *, calibrated=True, measured="sut"):
    from robovast.execution.cluster_execution.node_calibration import NodeCalibration

    calibration = NodeCalibration()
    if calibrated:
        calibration.claim_probe("n1", "probe-1")
        calibration.record("n1", "probe-1",
                           {measured: {"cores": 2.0, "memory_peak": 100 * 1024 ** 2,
                                       "samples": 90}})
    runner = kb.BatchJobRunner.__new__(kb.BatchJobRunner)
    runner._calibration = calibration
    runner._batch_tag = "batch-0"
    (tmp_path / "_execution").mkdir(parents=True, exist_ok=True)
    return runner


def test_a_kill_at_a_measured_figure_is_recorded_where_it_outlives_the_job(tmp_path):
    """The Job carrying the evidence is deleted moments later, so a fact kept only in it is a
    fact nobody can be told."""
    runner = _runner(tmp_path)
    runner._record_a_kill_at_a_measured_figure("job-1", _entry(), str(tmp_path))
    entries = read_interventions(tmp_path, KIND_SIZING)
    assert len(entries) == 1
    assert "MEASURED" in entries[0]["detail"]
    assert "0.12GiB" in entries[0]["detail"], "what it died at"
    assert "0.10GiB" in entries[0]["detail"], "and what was measured"


def test_a_kill_at_a_figure_nobody_measured_records_nothing(tmp_path):
    """A container killed at what its AUTHOR declared is a campaign asking for too little --
    theirs to fix, and not a measurement to report."""
    runner = _runner(tmp_path, calibrated=False)
    runner._record_a_kill_at_a_measured_figure("job-1", _entry(), str(tmp_path))
    assert read_interventions(tmp_path, KIND_SIZING) == []


def test_a_crash_that_is_not_an_oom_says_nothing_about_memory(tmp_path):
    runner = _runner(tmp_path)
    runner._record_a_kill_at_a_measured_figure("job-1", _entry(reason="Error"), str(tmp_path))
    assert read_interventions(tmp_path, KIND_SIZING) == []


def _ended_pod(job="job-1", container="scenario", reason="OOMKilled", limit="128Mi"):
    """A pod whose container is NOT restarted and died: ``state``, not ``last_state``."""
    from types import SimpleNamespace as NS
    terminated = NS(reason=reason, exit_code=137)
    return NS(
        metadata=NS(name=f"{job}-pod", labels={"job-name": job}),
        spec=NS(node_name="n1", init_containers=[],
                containers=[NS(name=container, resources=NS(limits={"memory": limit}))]),
        status=NS(phase="Failed", init_container_statuses=[], container_statuses=[
            NS(name=container, state=NS(terminated=terminated, running=None, waiting=None),
               last_state=NS(terminated=None), restart_count=0)]))


class _Pods:
    def __init__(self, *pods):
        self.pods, self.lists = list(pods), 0

    def list_namespaced_pod(self, namespace, label_selector=None):
        from types import SimpleNamespace as NS
        self.lists += 1
        return NS(items=self.pods)


def test_a_container_the_pod_does_not_restart_is_read_where_it_died():
    """Under ``restartPolicy: Never`` the scenario container dies into ``state`` and takes the
    pod with it, so the restart path -- which reads ``last_state`` -- never sees it."""
    from robovast.execution.cluster_execution.cluster_execution import oom_killed_job_forensics
    killed = oom_killed_job_forensics(_Pods(_ended_pod(), _ended_pod("job-2", reason="Error")),
                                      "ns", "sel")
    assert list(killed) == ["job-1"]
    assert killed["job-1"]["node"] == "n1"
    assert killed["job-1"]["containers"] == [
        {"container": "scenario", "reason": "OOMKilled", "memory_limit": "128Mi"}]


def test_a_pod_that_ended_on_a_kill_at_a_measured_figure_is_recorded_once(tmp_path):
    """Its pod is listed on every poll until the batch cleans up, so the same kill is seen
    again and again -- and a count of lost runs that grows with the poll rate is no count."""
    runner = _runner(tmp_path, measured="scenario")
    runner.k8s_client, runner.namespace = _Pods(_ended_pod()), "ns"
    for _ in range(3):
        runner._record_oom_kills_at_measured_figures("sel", ["job-1"], str(tmp_path))
    entries = read_interventions(tmp_path, KIND_SIZING)
    assert len(entries) == 1 and "scenario" in entries[0]["detail"]


def test_nothing_measured_means_no_pod_is_listed_for_it(tmp_path):
    """A campaign with no figures cannot lose a run to one, and a pod list per poll is not free."""
    runner = _runner(tmp_path, calibrated=False)
    pods = _Pods(_ended_pod())
    runner.k8s_client, runner.namespace = pods, "ns"
    runner._record_oom_kills_at_measured_figures("sel", ["job-1"], str(tmp_path))
    assert pods.lists == 0 and read_interventions(tmp_path, KIND_SIZING) == []


def _transport(tmp_path):
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    transport = LocalTransport(store=store)
    transport._campaigns_root = lambda: tmp_path / "results"
    return transport


def _record(campaign_dir: Path, n: int) -> None:
    campaign_dir.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        record_intervention(campaign_dir, kind=KIND_SIZING, job_dir="", job_name=f"job-{i}",
                            source="runner",
                            detail="container sut was OOM-killed at 0.12GiB -- memory this "
                                   "campaign MEASURED on its node (0.10GiB peak)")


def test_the_campaign_reports_it_as_an_error_level_finding(tmp_path):
    """``vast campaign wait`` ends on an error-level finding, which is how an agent that is
    watching gets told without watching a log."""
    transport = _transport(tmp_path)
    _record(tmp_path / "results" / "camp-1", 3)
    findings = transport._findings_from_record("camp-1")
    assert len(findings) == 1
    assert findings[0].level == "error"
    assert findings[0].check == "calibrated-memory-oom"


def test_one_finding_per_fault_rather_than_one_per_lost_run(tmp_path):
    """Forty findings saying the same thing would hide whatever else the campaign reports --
    and the count is what tells a fault from a flake, so it is in the sentence."""
    transport = _transport(tmp_path)
    _record(tmp_path / "results" / "camp-1", 40)
    findings = transport._findings_from_record("camp-1")
    assert len(findings) == 1
    assert findings[0].detail.startswith("40 run(s) lost")
    assert "calibration.min.memory" in findings[0].detail, "and what to state"


def test_a_campaign_that_lost_nothing_reports_nothing(tmp_path):
    transport = _transport(tmp_path)
    (tmp_path / "results" / "camp-1" / "_execution").mkdir(parents=True)
    assert transport._findings_from_record("camp-1") == []
