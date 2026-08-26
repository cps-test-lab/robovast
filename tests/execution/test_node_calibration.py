# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Per-node sizing learned from one discarded run per node."""

import pytest

from robovast.execution.cluster_execution.node_calibration import (CALIBRATION_HEADROOM, MIN_CPU,
                                                                   NodeCalibration,
                                                                   calibration_applies)


def test_a_pilot_calibrates_nothing():
    """Calibration costs one run per node and pays only where a node runs a SECOND job. With
    no more jobs than nodes, a pilot would spend its entire result set on measurement."""
    assert calibration_applies(50, 4) is True
    assert calibration_applies(4, 4) is False, "no node would get a second run"
    assert calibration_applies(1, 4) is False
    assert calibration_applies(5, 4) is True
    assert calibration_applies(10, 0) is False, "no nodes, nothing to calibrate against"


def test_an_autoscaling_cluster_is_not_calibrated():
    """There a job that fits no current node is created UNPINNED, so it can land on an
    already-calibrated node at the declared size -- the mixed sizing this exists to prevent,
    and invisible afterwards. The node set is fluid anyway, so a probe may measure a machine
    that is about to be scaled away."""
    assert calibration_applies(50, 4, growable=True) is False
    assert calibration_applies(50, 4, growable=False) is True


def test_one_probe_per_node_at_a_time():
    """The trap that would cost a campaign a probe per job instead of a probe per node."""
    c = NodeCalibration()
    assert c.claim_probe("n1", "j0") is True
    assert c.claim_probe("n1", "j1") is False
    assert c.claim_probe("n2", "j2") is True, "a different node is a different probe"


def test_a_calibrated_node_never_probes_again():
    """Frozen. Continuing to adapt would mean run 5 and run 40 on the same node ran in
    different environments -- the same inconsistency this removes, in a slower form."""
    c = NodeCalibration()
    c.claim_probe("n1", "j0")
    c.record("n1", "j0", {"sut": {"sustained": 1.4, "peak": 2.0, "samples": 200}})
    assert c.claim_probe("n1", "j9") is False
    assert c.calibrated("n1")["sut"]["peak"] == pytest.approx(2.0 * CALIBRATION_HEADROOM)


def test_headroom_is_applied_because_a_peak_is_one_sample():
    """Sizing exactly at what one run measured guarantees the next run is clipped."""
    c = NodeCalibration()
    c.claim_probe("n1", "j0")
    c.record("n1", "j0", {"sut": {"sustained": 1.4, "peak": 2.0, "samples": 200},
                          "simulation": {"sustained": 0.4, "peak": 4.8, "samples": 200}})
    got = c.calibrated("n1")
    assert got["sut"]["peak"] == pytest.approx(2.5)
    assert got["simulation"]["sustained"] == pytest.approx(0.5)
    # Both statistics survive, because one number cannot serve both roles: the simulator
    # sustains 0.4 and peaks at 4.8, and sizing it at either alone is wrong by ~12x.
    assert got["simulation"]["peak"] == pytest.approx(6.0)


def test_a_container_that_did_almost_nothing_still_gets_a_floor():
    """A trial that failed early, or a simulator that never got past bring-up, would otherwise
    pin the node to a figure the next run cannot live in."""
    c = NodeCalibration()
    c.claim_probe("n1", "j0")
    c.record("n1", "j0", {"sut": {"sustained": 0.001, "peak": 0.001, "samples": 200}})
    assert c.calibrated("n1")["sut"]["peak"] == MIN_CPU


def test_a_probe_that_measured_nothing_leaves_the_node_uncalibrated():
    """Silence is not a measurement of zero. The node must stay on the declared sizing and let
    the next job try, rather than freeze on a figure derived from no data."""
    c = NodeCalibration()
    c.claim_probe("n1", "j0")
    assert c.record("n1", "j0", {}) is False
    assert c.calibrated("n1") is None
    assert c.accepts_work("n1") is True, "the node must not be left blocked"
    assert c.claim_probe("n1", "j1") is True, "the node is free to be probed again"


def test_an_abandoned_probe_frees_the_node_rather_than_blocking_it():
    """A probe that dies must not leave its node refusing work for the rest of the campaign.

    The node stays uncalibrated and its runs use the declared sizing -- the same thing that
    happens where calibration is off entirely. A worse allocation, never a wrong result.
    """
    c = NodeCalibration()
    c.claim_probe("n1", "j0")
    assert c.accepts_work("n1") is False
    c.abandon("n1", "j0")
    assert c.accepts_work("n1") is True
    assert c.claim_probe("n1", "j1") is True


def test_recording_against_the_wrong_job_is_refused():
    """Only the job that claimed the node may calibrate it; otherwise a late finisher could
    overwrite the figures every later run was already sized with."""
    c = NodeCalibration()
    c.claim_probe("n1", "j0")
    assert c.record("n1", "j-other", {"sut": {"peak": 9.0}}) is False
    assert c.calibrated("n1") is None


def test_disabled_calibration_claims_nothing_and_blocks_nothing():
    """A pilot must behave exactly as it did before any of this existed."""
    c = NodeCalibration(enabled=False)
    assert c.claim_probe("n1", "j0") is False
    assert c.accepts_work("n1") is True


# -- the measurement ----------------------------------------------------------------------

def test_a_tick_is_summed_over_processes_before_it_is_aggregated():
    """A row is one PROCESS and a container is the whole stack of them. Taking the max over
    rows reports the busiest single process and sizes the container for a fraction of
    itself."""
    from robovast.execution.cluster_execution.node_calibration import container_cpu_profile

    rows = [{"timestamp": "1", "cpu_percent": "50"},
            {"timestamp": "1", "cpu_percent": "70"}]
    assert container_cpu_profile(rows)["peak"] == pytest.approx(1.2), "0.5 + 0.7, not 0.7"


def test_sustained_and_peak_are_both_reported():
    """The pair is the point. Measured on the shipped example a simulator sustains ~1 core
    and peaks near 6, so a single figure is wrong by ~6x whichever one is chosen."""
    from robovast.execution.cluster_execution.node_calibration import container_cpu_profile

    rows = [{"timestamp": str(i), "cpu_percent": "100"} for i in range(150)]
    rows.append({"timestamp": "burst", "cpu_percent": "598"})
    got = container_cpu_profile(rows)
    assert got["sustained"] == pytest.approx(1.0)
    assert got["peak"] == pytest.approx(5.98)


def test_nothing_to_read_is_not_a_measurement_of_zero():
    from robovast.execution.cluster_execution.node_calibration import container_cpu_profile

    assert container_cpu_profile([]) == {}
    assert container_cpu_profile([{"nonsense": "1"}]) == {}


# -- applying it, per role ----------------------------------------------------------------

def _calibrated(declared, name, figures):
    from robovast.execution.cluster_execution.kubernetes_backend import calibrated_resources
    return calibrated_resources(declared, name, figures)


def test_the_system_under_test_is_sized_on_its_peak_and_stays_pinned():
    """Request AND limit, both at the peak. Its budget has to be one it never throttles
    against: a run clipped mid-plan fails in a way that looks like the stack's fault rather
    than the allocation's, which is the confusion that cost 11 runs on 2026-08-26."""
    got = _calibrated({"cpu": 3, "memory": "640Mi"}, "sut",
                      {"sut": {"sustained": 1.4, "peak": 2.5}})
    assert got["cpu"] == 2.5 and got["cpu_limit"] == 2.5
    assert got["memory"] == "640Mi", "memory is never re-sized"


def test_an_infrastructure_container_is_sized_on_what_it_sustains():
    """And keeps its declared ceiling. Reserving the simulator's PEAK per node would cost more
    than the un-calibrated campaign did -- its peak-to-mean ratio is about 18 -- which is the
    opposite of the point."""
    got = _calibrated({"cpu": 0.5, "cpu_limit": 6, "memory": "2944Mi"}, "simulation",
                      {"simulation": {"sustained": 0.42, "peak": 6.0}})
    assert got["cpu"] == 0.42, "the reservation follows what it sustains"
    assert got["cpu_limit"] == 6, "the ceiling is the author's, and a burst still fits under it"


def test_an_uncalibrated_node_changes_nothing():
    """Every node starts here, and a campaign too small to calibrate stays here. It must be
    byte-identical to what the author declared."""
    declared = {"cpu": 3, "memory": "640Mi"}
    assert _calibrated(declared, "sut", None) == declared
    assert _calibrated(declared, "sut", {}) == declared
    assert _calibrated(declared, "sut", {"other": {"peak": 9}}) == declared


def test_a_node_takes_no_campaign_work_while_its_probe_is_out():
    """Otherwise jobs land at the declared size while the probe is still measuring, and those
    runs are the odd ones out on a node whose later runs are calibrated -- the inconsistency
    the probe exists to remove, reintroduced by the act of measuring."""
    c = NodeCalibration()
    assert c.accepts_work("n1") is True
    c.claim_probe("n1", "probe-1")
    assert c.accepts_work("n1") is False
    c.record("n1", "probe-1", {"sut": {"sustained": 1.0, "peak": 2.0, "samples": 200}})
    assert c.accepts_work("n1") is True


def test_the_probe_directory_is_not_a_configuration():
    """The whole mechanism by which a probe is never a campaign run: it writes somewhere
    nothing walks looking for runs, so it is never ADDED rather than added and removed."""
    from robovast.common.campaign_data import RESERVED_CAMPAIGN_DIRS

    assert "_calibration" in RESERVED_CAMPAIGN_DIRS


# -- the probe validity gate --------------------------------------------------------------

_GOOD = {"sut": {"sustained": 1.4, "peak": 2.0, "samples": 200}}


def test_a_probe_whose_scenario_never_finished_does_not_calibrate():
    """The sharp edge, and a correctness requirement rather than a refinement.

    The monitor writes its CSV whether or not the scenario succeeds, so a probe that died ten
    seconds in still produces a file -- with a peak near nothing. Believed, it floors the node
    to MIN_CPU and then EVERY campaign run placed there is starved by an allocation derived
    from a run that never happened: a measurement failure silently become degraded results.
    """
    c = NodeCalibration()
    c.claim_probe("n1", "p1")
    assert c.record("n1", "p1", _GOOD, completed=False) is False
    assert c.calibrated("n1") is None
    assert c.accepts_work("n1") is True, "the node falls back to declared sizing, not to a stall"


def test_a_probe_with_too_few_samples_does_not_calibrate():
    """A short probe is not a small container, and MIN_CPU cannot tell the two apart -- a
    floor is a floor whether the container idles or the run stopped before it started."""
    from robovast.execution.cluster_execution.node_calibration import MIN_PROBE_SAMPLES

    c = NodeCalibration()
    c.claim_probe("n1", "p1")
    thin = {"sut": {"sustained": 0.01, "peak": 0.02, "samples": MIN_PROBE_SAMPLES - 1}}
    assert c.record("n1", "p1", thin) is False
    assert c.calibrated("n1") is None


def test_one_thin_container_refuses_the_whole_measurement():
    """Sizing a pod from a mix of a real measurement and a fragment is worse than not sizing
    it: the containers would be scaled against different amounts of the same run."""
    c = NodeCalibration()
    c.claim_probe("n1", "p1")
    mixed = {"sut": {"sustained": 1.4, "peak": 2.0, "samples": 200},
             "simulation": {"sustained": 0.3, "peak": 5.0, "samples": 4}}
    assert c.record("n1", "p1", mixed) is False


def test_sample_count_never_becomes_a_cpu_figure():
    """It travels with the measurement to be judged on, not to be sized from."""
    c = NodeCalibration()
    c.claim_probe("n1", "p1")
    c.record("n1", "p1", _GOOD)
    assert set(c.calibrated("n1")["sut"]) == {"sustained", "peak"}


# -- reading a finished probe -------------------------------------------------------------

def _csv(ticks, cpu_percent=100):
    head = "timestamp,pid,name,cpu_percent,memory_rss_bytes\n"
    return (head + "".join(f"{i},1,proc,{cpu_percent},10\n"
                           for i in range(ticks))).encode()


def test_a_probe_is_read_from_its_own_files_not_from_postprocessing():
    """The monitor's CSV IS the measurement. Postprocessing only lifts it into data.db, and
    does so at the end of a campaign or batch -- far too late to size the job that comes
    next, which is why the probe's directory being skipped by postprocessing costs nothing.
    """
    from robovast.execution.cluster_execution.node_calibration import read_probe_measurement

    store = {"_calibration/node-a/resource_usage_sut.csv": _csv(50),
             "_calibration/node-a/resource_usage_simulation.csv": _csv(50, cpu_percent=40)}
    got = read_probe_measurement(
        store.get, "_calibration/node-a/",
        {"sut": "resource_usage_sut.csv", "simulation": "resource_usage_simulation.csv"})
    assert got["sut"]["peak"] == pytest.approx(1.0)
    assert got["simulation"]["peak"] == pytest.approx(0.4)
    assert got["sut"]["samples"] == 50


def test_a_missing_container_file_is_absent_rather_than_zero():
    """Which is what makes the whole probe unusable rather than most of it: a pod sized from
    a real measurement for one container and a guess for another is worse than one not sized
    at all."""
    from robovast.execution.cluster_execution.node_calibration import read_probe_measurement

    store = {"p/resource_usage_sut.csv": _csv(50)}
    got = read_probe_measurement(store.get, "p/",
                                 {"sut": "resource_usage_sut.csv",
                                  "simulation": "resource_usage_simulation.csv"})
    assert "simulation" not in got and "sut" in got


def test_an_unreadable_probe_file_does_not_raise():
    """A storage blip during calibration must cost an optimisation, never the campaign."""
    from robovast.execution.cluster_execution.node_calibration import read_probe_measurement

    def _boom(_key):
        raise OSError("object store said no")

    assert read_probe_measurement(_boom, "p/", {"sut": "resource_usage_sut.csv"}) == {}


# -- keeping a probe out of the run tree --------------------------------------------------

def test_the_scenario_results_are_redirected_too_not_only_the_job_artifacts():
    """The half that is easy to miss, and missing it is the whole hazard.

    A job's output arrives in two places by two mechanisms: job artifacts (the monitor's
    CSVs) follow OUTPUT_DIR, while scenario results (rosbags, test.xml, poses) follow the
    parameter document's _output_dir, normally "<config>/<run>". Override only the first and
    a probe writes its results into a REAL campaign run directory -- colliding with that run
    or manufacturing one that looks real, which is exactly the contamination this design
    exists to avoid.
    """
    from robovast.execution.cluster_execution.node_calibration import (
        probe_output_dir, probe_parameter_documents)

    docs = [{"test_scenario": {"_output_dir": "goal-1/0", "speed": 3}}]
    got = probe_parameter_documents(docs, "node-abc")
    assert got[0]["test_scenario"]["_output_dir"] == probe_output_dir("node-abc")
    assert probe_output_dir("node-abc").startswith("_calibration/")


def test_everything_else_about_the_configuration_is_left_alone():
    """A probe must run the same configuration a real job would, bags and all. Recording is
    not free, so a probe that skipped it would measure a lighter workload than the runs it is
    sizing and under-size the node."""
    from robovast.execution.cluster_execution.node_calibration import probe_parameter_documents

    docs = [{"test_scenario": {"_output_dir": "goal-1/0", "speed": 3, "map": "depot.yaml"}}]
    got = probe_parameter_documents(docs, "node-abc")
    assert got[0]["test_scenario"]["speed"] == 3
    assert got[0]["test_scenario"]["map"] == "depot.yaml"


def test_the_original_documents_are_not_mutated():
    """They belong to the real job that is about to run with them."""
    from robovast.execution.cluster_execution.node_calibration import probe_parameter_documents

    docs = [{"test_scenario": {"_output_dir": "goal-1/0"}}]
    probe_parameter_documents(docs, "node-abc")
    assert docs[0]["test_scenario"]["_output_dir"] == "goal-1/0"


def test_the_probe_directory_is_the_reserved_one():
    """Both halves must point at a name nothing walks looking for runs."""
    from robovast.common.campaign_data import RESERVED_CAMPAIGN_DIRS
    from robovast.execution.cluster_execution.node_calibration import PROBE_DIR

    assert PROBE_DIR in RESERVED_CAMPAIGN_DIRS


# -- the probe's manifest -----------------------------------------------------------------

def _base_manifest():
    env = lambda: [{"name": "OUTPUT_DIR", "value": "/out/_jobs/batch-0/job-0"},
                   {"name": "SCENARIO_PARAMETER_FILE", "value": "/config/job-0.params.yaml"},
                   {"name": "SCENARIO_FILE", "value": "scenario.osc"}]
    return {"metadata": {"name": "camp-batch0-job-0"},
            "spec": {"template": {"spec": {
                "containers": [{"name": "robovast", "env": env()}],
                "initContainers": [{"name": "sut", "env": env()},
                                   {"name": "simulation", "env": env()}]}}}}


def test_a_probe_runs_what_the_campaign_runs():
    """Derived from a real job's manifest, because the measurement is worth nothing unless
    the probe runs the same image, containers, scenario and declared resources."""
    from robovast.execution.cluster_execution.kubernetes_backend import probe_manifest

    m = probe_manifest(_base_manifest(), job_name="probe-n1",
                       params_file="/config/probe-n1.params.yaml",
                       output_dir="/out/_calibration/node-a")
    names = [c["name"] for c in m["spec"]["template"]["spec"]["initContainers"]]
    assert names == ["sut", "simulation"], "the same containers, unchanged"
    assert m["metadata"]["name"] == "probe-n1"


def test_every_container_is_redirected_not_only_the_main_one():
    """The sidecars are handed the same extra env, and they are where the simulator and the
    system under test write the CSVs that matter most here. A sidecar left on the old
    OUTPUT_DIR puts exactly those back into the run tree."""
    from robovast.execution.cluster_execution.kubernetes_backend import probe_manifest

    m = probe_manifest(_base_manifest(), job_name="p", params_file="/config/p.yaml",
                       output_dir="/out/_calibration/node-a")
    spec = m["spec"]["template"]["spec"]
    for container in spec["containers"] + spec["initContainers"]:
        env = {e["name"]: e["value"] for e in container["env"]}
        assert env["OUTPUT_DIR"] == "/out/_calibration/node-a", container["name"]
        assert env["SCENARIO_PARAMETER_FILE"] == "/config/p.yaml", container["name"]
        assert env["SCENARIO_FILE"] == "scenario.osc", "unrelated env is untouched"


def test_the_real_jobs_manifest_is_not_mutated():
    """It belongs to a job that is about to run with it."""
    from robovast.execution.cluster_execution.kubernetes_backend import probe_manifest

    base = _base_manifest()
    probe_manifest(base, job_name="p", params_file="/config/p.yaml", output_dir="/out/x")
    assert base["metadata"]["name"] == "camp-batch0-job-0"
    assert base["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"] \
        == "/out/_jobs/batch-0/job-0"


# -- the manifest and the queue must agree ------------------------------------------------

def test_the_sizing_the_queue_uses_is_the_sizing_the_manifest_asks_for():
    """The drift that would be invisible and expensive.

    Admission decides how many pods fit a node from JobSizing; Kubernetes reserves what the
    manifest says. If calibration changed one and not the other, the queue would over- or
    under-fill every calibrated node by exactly the difference -- and nothing would report
    it, because both halves are individually self-consistent. They go through one function
    for that reason, and this pins that they still do.
    """
    from robovast.execution.cluster_execution import kubernetes_backend as kb

    figures = {"sut": {"sustained": 1.4, "peak": 2.0},
               "simulation": {"sustained": 0.4, "peak": 5.0}}
    r = kb.BatchJobRunner()

    def _manifest(job, total, node_figures=None):
        sut = kb.calibrated_resources({"cpu": 3, "memory": "640Mi"}, "sut", node_figures)
        sim = kb.calibrated_resources({"cpu": 0.75, "cpu_limit": 6, "memory": "640Mi"},
                                      "simulation", node_figures)
        spec = {"containers": [{"resources": {"requests": {"cpu": "1", "memory": "512Mi"}}}],
                "initContainers": []}
        for res in (sut, sim):
            container = {"restartPolicy": "Always", "resources": {}}
            kb.stamp_resources(container, res)
            spec["initContainers"].append(container)
        return {"spec": {"template": {"spec": spec}}}

    r.create_job_manifest = _manifest
    r.manifest = {}

    declared = r._job_sizing(object(), 1)
    calibrated = r._job_sizing(object(), 1, node_figures=figures)

    # 1 (main) + 3 (sut) + 0.75 (sim) declared; 1 + 2.0 (sut peak) + 0.4 (sim sustained).
    assert declared.cpu == pytest.approx(4.75)
    assert calibrated.cpu == pytest.approx(3.4)
    assert calibrated.cpu < declared.cpu, "a calibrated node holds more of them"


def test_the_declared_role_decides_not_the_container_name():
    """A stack that bundles its own simulator serves the simulation role from its sut
    container. It is still the thing under test, so it is still sized on peak -- and that is
    a case where role and name already differ, which is why the rule rests on the role."""
    figures = {"my_stack": {"sustained": 1.4, "peak": 2.5}}
    got = _calibrated_with_roles({"cpu": 3}, "my_stack", figures, roles=("sut", "simulation"))
    assert got["cpu"] == 2.5 and got["cpu_limit"] == 2.5, "peak, and pinned"


def test_a_container_with_no_role_is_treated_as_infrastructure():
    """An ad-hoc container is not under test, so it reserves what it sustains."""
    figures = {"helper": {"sustained": 0.3, "peak": 4.0}}
    got = _calibrated_with_roles({"cpu": 1, "cpu_limit": 4}, "helper", figures, roles=())
    assert got["cpu"] == pytest.approx(0.3)
    assert got["cpu_limit"] == 4


def _calibrated_with_roles(declared, name, figures, roles):
    from robovast.execution.cluster_execution.kubernetes_backend import calibrated_resources
    return calibrated_resources(declared, name, figures, roles=roles)


def test_the_created_manifest_uses_the_same_figures_the_queue_admitted_against():
    """The seam where this actually broke, which an earlier test missed by one function.

    That test compared _job_sizing against calibrated_resources and passed -- while the job
    CREATION path built its manifest without the node's figures at all. So the queue counted
    a calibrated node as holding the smaller job and Kubernetes was asked to reserve the
    declared one: over-admission, silently, on exactly the nodes calibration was meant to
    help. Seen on a live cluster, where every job on a calibrated node still asked for the
    declared 3 cores.

    Both paths now go through one lookup, and this pins the CREATION half.
    """
    from robovast.execution.cluster_execution import kubernetes_backend as kb
    from robovast.execution.cluster_execution.node_calibration import NodeCalibration

    class _Admission:
        def __init__(self):
            self._cal = NodeCalibration()
            self._cal.claim_probe("fast", "p")
            self._cal.record("fast", "p", {"sut": {"sustained": 1.0, "peak": 2.0,
                                                   "samples": 200}})

        def calibration(self, owner, factory=None):
            return self._cal

    r = kb.BatchJobRunner()
    r.campaign = "camp-2026-07-17-120000"
    r.admission = _Admission()

    assert r._node_figures("fast"), "the calibrated node has figures"
    assert r._node_figures("other") is None, "an uncalibrated one has none"
    assert r._node_figures(None) is None, "and an unpinned job has no node to look up"
