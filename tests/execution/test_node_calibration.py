# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Per-node sizing learned from one discarded run per node."""

import pytest

from robovast.execution.cluster_execution import node_calibration as nc
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
    c.record("n1", "j0", {"sut": {"cores": 1.4, "samples": 200}})
    assert c.claim_probe("n1", "j9") is False
    assert c.calibrated("n1")["sut"]["cores"] == pytest.approx(1.4), "stored as measured"


def test_the_store_keeps_what_was_measured_not_what_will_be_asked_for():
    """Headroom and the floor are applied where the ALLOCATION is built, not here.

    Both are per-container settings now, and this store deliberately does not know what a
    container is for -- the same reason it is told each container's percentile rather than
    deciding it. Storing a padded figure would also make the log line a statement about
    settings rather than about the machine."""
    c = NodeCalibration()
    c.claim_probe("n1", "j0")
    c.record("n1", "j0", {"sut": {"cores": 1.4, "samples": 200},
                          "simulation": {"cores": 0.4, "samples": 200}})
    got = c.calibrated("n1")
    assert got["sut"]["cores"] == pytest.approx(1.4), "as measured, unpadded"
    assert got["simulation"]["cores"] == pytest.approx(0.4)


def test_a_container_that_did_almost_nothing_still_gets_a_floor():
    """A trial that failed early, or a simulator that never got past bring-up, would otherwise
    pin the node to a figure the next run cannot live in. Applied where the allocation is
    built, so the store still reports the measurement itself."""
    from robovast.execution.cluster_execution.kubernetes_backend import calibrated_resources

    sized = calibrated_resources(
        {}, "sut", {"sut": {"cores": 0.001, "samples": 200}}, roles=("sut",), bootstrap=True,
        settings={"size_on": 100, "limit": "request", "headroom": {"cpu": 1.25}})
    assert sized["cpu"] == MIN_CPU


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
    assert c.record("n1", "j-other", {"sut": {"cores": 9.0}}) is False
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
    assert container_cpu_profile(rows)["cores"] == pytest.approx(1.2), "0.5 + 0.7, not 0.7"


def test_the_percentile_decides_which_figure_comes_back():
    """One reading per call, at the percentile the caller asked for -- so the choice of
    statistic lives with the role rules rather than in the reader.

    The spread is the point: on the shipped example a simulator sustains ~1 core and peaks
    near 6, so which end is read is worth ~6x."""
    from robovast.execution.cluster_execution.node_calibration import container_cpu_profile

    rows = [{"timestamp": str(i), "cpu_percent": "100"} for i in range(150)]
    rows.append({"timestamp": "burst", "cpu_percent": "598"})
    assert container_cpu_profile(rows, percentile=95)["cores"] == pytest.approx(1.0)
    assert container_cpu_profile(rows, percentile=100)["cores"] == pytest.approx(5.98)


def test_nothing_to_read_is_not_a_measurement_of_zero():
    from robovast.execution.cluster_execution.node_calibration import container_cpu_profile

    assert container_cpu_profile([]) == {}
    assert container_cpu_profile([{"nonsense": "1"}]) == {}


# -- applying it, per role ----------------------------------------------------------------

def _settings_for(role):
    """The role's own rule, as the backend resolves it -- so a test states which role it is
    talking about and never restates the rule it is checking."""
    from robovast.execution.cluster_execution.node_calibration import calibration_defaults
    return calibration_defaults(role)


def _calibrated(declared, name, figures, role=None):
    from robovast.execution.cluster_execution.kubernetes_backend import calibrated_resources
    return calibrated_resources(declared, name, figures, roles=(role or name,),
                                settings=_settings_for(role or name))


def test_the_system_under_test_is_sized_on_its_peak_and_stays_pinned():
    """Request AND limit, both at the peak. Its budget has to be one it never throttles
    against: a run clipped mid-plan fails in a way that looks like the stack's fault rather
    than the allocation's, which is the confusion this separates."""
    got = _calibrated({"cpu": 3, "memory": "640Mi"}, "sut",
                      {"sut": {"cores": 1.4}})
    assert got["cpu"] == pytest.approx(1.75) and got["cpu_limit"] == pytest.approx(1.75)
    assert got["memory"] == "640Mi", "no memory measurement here, so the declaration stands"


def test_an_infrastructure_container_is_sized_on_what_it_sustains():
    """And keeps its declared ceiling. Reserving the simulator's PEAK per node would cost more
    than the un-calibrated campaign did -- its peak-to-mean ratio is about 18 -- which is the
    opposite of the point."""
    got = _calibrated({"cpu": 0.5, "cpu_limit": 6, "memory": "2944Mi"}, "simulation",
                      {"simulation": {"cores": 0.42}})
    assert got["cpu"] == pytest.approx(0.42 * 1.25), "what it sustained, plus headroom"
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
    c.record("n1", "probe-1", {"sut": {"cores": 1.0, "samples": 200}})
    assert c.accepts_work("n1") is True


def test_the_probe_directory_is_not_a_configuration():
    """The whole mechanism by which a probe is never a campaign run: it writes somewhere
    nothing walks looking for runs, so it is never ADDED rather than added and removed."""
    from robovast.common.campaign_data import RESERVED_CAMPAIGN_DIRS

    assert "_calibration" in RESERVED_CAMPAIGN_DIRS


# -- the probe validity gate --------------------------------------------------------------

_GOOD = {"sut": {"cores": 1.4, "samples": 200}}


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
    thin = {"sut": {"cores": 0.01, "samples": MIN_PROBE_SAMPLES - 1}}
    assert c.record("n1", "p1", thin) is False
    assert c.calibrated("n1") is None


def test_one_thin_container_refuses_the_whole_measurement():
    """Sizing a pod from a mix of a real measurement and a fragment is worse than not sizing
    it: the containers would be scaled against different amounts of the same run."""
    c = NodeCalibration()
    c.claim_probe("n1", "p1")
    mixed = {"sut": {"cores": 1.4, "samples": 200},
             "simulation": {"cores": 0.3, "samples": 4}}
    assert c.record("n1", "p1", mixed) is False


def test_sample_count_never_becomes_a_cpu_figure():
    """It travels with the measurement to be judged on, not to be sized from."""
    c = NodeCalibration()
    c.claim_probe("n1", "p1")
    c.record("n1", "p1", _GOOD)
    assert set(c.calibrated("n1")["sut"]) == {"cores"}


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
    assert got["sut"]["cores"] == pytest.approx(1.0)
    assert got["simulation"]["cores"] == pytest.approx(0.4)
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
    def env():
        return [{"name": "OUTPUT_DIR", "value": "/out/_jobs/batch-0/job-0"},
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

    figures = {"sut": {"cores": 1.4},
               "simulation": {"cores": 0.4}}
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
    assert calibrated.cpu == pytest.approx(2.8)
    assert calibrated.cpu < declared.cpu, "a calibrated node holds more of them"


def test_the_declared_role_decides_not_the_container_name():
    """A stack that bundles its own simulator serves the simulation role from its sut
    container. It is still the thing under test, so it is still sized on peak -- and that is
    a case where role and name already differ, which is why the rule rests on the role."""
    figures = {"my_stack": {"cores": 1.4}}
    got = _calibrated_with_roles({"cpu": 3}, "my_stack", figures, roles=("sut", "simulation"))
    assert got["cpu"] == pytest.approx(1.75) and got["cpu_limit"] == pytest.approx(1.75)


def test_a_container_with_no_role_is_treated_as_infrastructure():
    """An ad-hoc container is not under test, so it reserves what it sustains."""
    figures = {"helper": {"cores": 0.3}}
    got = _calibrated_with_roles({"cpu": 1, "cpu_limit": 4}, "helper", figures, roles=())
    assert got["cpu"] == pytest.approx(0.3 * 1.25), "what it sustained, plus headroom"
    assert got["cpu_limit"] == 4, "and the ceiling its author set"


def _calibrated_with_roles(declared, name, figures, roles):
    from robovast.execution.cluster_execution.kubernetes_backend import calibrated_resources
    from robovast.execution.cluster_execution.node_calibration import calibration_defaults
    role = next((r for r in (roles or ())), name)
    return calibrated_resources(declared, name, figures, roles=roles,
                                settings=calibration_defaults(role))


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

    cal = NodeCalibration()
    cal.claim_probe("fast", "p")
    cal.record("fast", "p", {"sut": {"cores": 1.0, "samples": 200}})

    r = kb.BatchJobRunner()
    r.campaign = "camp-2026-07-17-120000"
    # Held on the runner, NOT fetched from the queue per lookup: one caller of this is the
    # sizing callback, which the queue invokes while holding its own lock.
    r._calibration = cal

    assert r._node_figures("fast"), "the calibrated node has figures"
    assert r._node_figures("other") is None, "an uncalibrated one has none"
    assert r._node_figures(None) is None, "and an unpinned job has no node to look up"
    r._calibration = None
    assert r._node_figures("fast") is None, "no calibration yet means declared sizing"


# -- readings a container could not have produced ------------------------------------------

def test_a_sample_above_the_containers_own_quota_is_discarded():
    """A cgroup cannot exceed its quota: CFS enforces it per ~100ms period and these are
    one-second samples, so a sample above the limit is measurement error, not a peak.

    They are real and they are large. The monitor's CSV covers the container's whole life
    including bring-up, where psutil reports a newly-seen process's average since it STARTED
    rather than since the last sample -- and a ROS stack spawns dozens at once. Measured on a
    3-core container: 10.4 "cores" outside the trial window against 2.82 inside it.

    Every other consumer filters on in_window, which postprocessing adds and the raw file does
    not carry, so calibration is the one reader that meets the artifact -- and it takes the
    MAX. Unclamped it sized a node at 14.4 cores for a 3-core container, and 35 on another.
    """
    from robovast.execution.cluster_execution.node_calibration import container_cpu_profile

    rows = [{"timestamp": "boot", "cpu_percent": "1040"}]
    rows += [{"timestamp": str(i), "cpu_percent": "150"} for i in range(150)]

    assert container_cpu_profile(rows, percentile=100)["cores"] == pytest.approx(10.4), \
        "unfiltered, as found"
    clamped = container_cpu_profile(rows, limit_cores=3.0)
    assert clamped["cores"] == pytest.approx(1.5), "the impossible sample is gone"
    assert clamped["samples"] == 150, "and it is not counted as a sample either"


def test_a_probe_that_is_nothing_but_impossible_samples_measures_nothing():
    """Better no figure than one drawn entirely from artifacts: the node stays on declared
    sizing, which is merely un-optimised."""
    from robovast.execution.cluster_execution.node_calibration import container_cpu_profile

    rows = [{"timestamp": str(i), "cpu_percent": "900"} for i in range(50)]
    assert container_cpu_profile(rows, limit_cores=3.0) == {}


def test_without_a_limit_nothing_is_discarded():
    """The limit is what makes the peak usable, so it is passed wherever it is known -- but
    an unknown limit must not silently drop real data."""
    from robovast.execution.cluster_execution.node_calibration import container_cpu_profile

    rows = [{"timestamp": str(i), "cpu_percent": "150"} for i in range(40)]
    assert container_cpu_profile(rows)["samples"] == 40


def test_the_completion_gate_reads_the_scenarios_verdict_not_its_own_output():
    """The gate was wired to a tautology and so caught nothing.

    ``record`` took ``completed`` and was handed ``bool(measured)`` -- "completed if we
    measured something" -- which is true of every probe that produced a CSV at all. The
    monitor writes that CSV whether or not the run got anywhere, so a probe whose scenario
    died ten seconds in passed the check it was built to fail.

    test.xml is the honest signal: a run writes it when its scenario reaches a verdict, and
    only then. Pass or fail is not the question -- a run that failed after doing its work
    still measured the resources that work needed.
    """
    from robovast.execution.cluster_execution.node_calibration import probe_completed

    store = {"_calibration/node-a/test.xml": b"<testsuite/>"}
    assert probe_completed(store.get, "_calibration/node-a/") is True
    assert probe_completed(store.get, "_calibration/node-b/") is False


def test_an_unreadable_verdict_is_not_completed():
    """A storage blip must cost the optimisation, never let a fragment size a node."""
    from robovast.execution.cluster_execution.node_calibration import probe_completed

    def _boom(_key):
        raise OSError("object store said no")

    assert probe_completed(_boom, "p/") is False


# -- a probe that never reports -------------------------------------------------------------

def test_abandoning_an_outstanding_probe_frees_its_node_for_later_batches():
    """The leak that excluded a node from a campaign for the rest of its life.

    A batch's loop exits when the CAMPAIGN's jobs are done. A probe pinned to a node another
    campaign has filled may still be queued at that moment -- nothing is wrong, it simply
    never got its turn. Without a hand-off the calibration still listed it as outstanding, so
    `accepts_work` answered False for that node in every later batch, and `claim_probe`
    refused to re-issue it. The node was measured by nothing and used by nothing.
    """
    cal = NodeCalibration()
    assert cal.claim_probe("n1", "probe-n1") is True
    assert cal.accepts_work("n1") is False

    cal.abandon("n1", "probe-n1")

    assert cal.accepts_work("n1") is True, "the node takes work again"
    assert cal.claim_probe("n1", "probe-n1-retry") is True, "and can be measured next batch"


def test_a_batch_releases_both_of_its_owners():
    """Probes queue under a second owner, and `cancel` matches an owner exactly.

    So cancelling only the campaign left every uncreated probe in the GLOBAL queue for the
    life of the process -- at priority 1, to be created later by another campaign's drain
    through a callback bound to a runner whose batch was over.
    """
    from robovast.execution.cluster_execution.kubernetes_backend import BatchJobRunner
    from robovast.execution.cluster_execution.node_admission import (AdmissionController,
                                                                     Budget, JobSizing,
                                                                     NodeBudget)

    class _Provider:
        def budget(self):
            return Budget(nodes=(NodeBudget(node_id="n1", free_cpu=0.0, free_memory=0),))

        def capacities(self):
            return []

    queue = AdmissionController(_Provider())
    campaign = "camp-2026-08-27-12000000"
    probes = f"{campaign}{BatchJobRunner._PROBE_OWNER_SUFFIX}"
    sizing = JobSizing(cpu=4.0, memory=1024)

    queue.submit(campaign, [("job-0", sizing, lambda n=None: None)], started_at=0.0)
    queue.submit(probes, [("probe-n1", sizing, lambda n=None: None)],
                 started_at=0.0, priority=1, pin="n1")
    assert queue.drain() == 0, "the node is full; neither can be created"

    queue.cancel(campaign)
    assert queue.states(probes), "the campaign's own owner does not cover its probes"

    queue.cancel(probes)
    assert not queue.states(probes), "and cancelling both leaves nothing behind"


def test_calibration_never_asks_for_more_than_the_author_declared():
    """A measured peak times the 1.25 headroom can exceed the ceiling it was capped at.

    Nothing downstream catches it: `preflight` runs once, on the DECLARED sizing, and is never
    re-asked per node -- so a calibrated figure no node can hold is not an error but an
    ordinary "no room now", forever, with the campaign reporting itself queued for capacity.
    Calibration exists to size a node's jobs DOWN to what they need; raising a ceiling the
    author set is not something it is for.
    """
    from robovast.execution.cluster_execution.kubernetes_backend import calibrated_resources
    from robovast.common.config import SUT_CONTAINER

    declared = {"cpu": 3.0}
    # A container that genuinely ran at its ceiling: 3.0 measured * 1.25 headroom = 3.75.
    figures = {SUT_CONTAINER: {"cores": 3.0}}

    out = calibrated_resources(declared, SUT_CONTAINER, figures, roles=(SUT_CONTAINER,),
                               settings=_settings_for(SUT_CONTAINER))

    assert out["cpu"] == 3.0, "clamped to the declared ceiling"
    assert out["cpu_limit"] == 3.0, "and the SUT keeps request == limit"


def test_a_measured_figure_below_the_ceiling_is_still_used():
    """The clamp must not become a floor: the reduction is the whole point of calibrating."""
    from robovast.execution.cluster_execution.kubernetes_backend import calibrated_resources
    from robovast.common.config import SUT_CONTAINER

    out = calibrated_resources({"cpu": 3.0}, SUT_CONTAINER,
                               {SUT_CONTAINER: {"cores": 1.1}}, roles=(SUT_CONTAINER,),
                               settings=_settings_for(SUT_CONTAINER))
    assert out["cpu"] == pytest.approx(1.375) and out["cpu_limit"] == pytest.approx(1.375)


def test_the_clamp_reads_the_split_limit_when_there_is_one():
    """With request and limit split, the ceiling is the limit -- not the reservation."""
    from robovast.execution.cluster_execution.kubernetes_backend import calibrated_resources

    out = calibrated_resources({"cpu": 0.5, "cpu_limit": 6}, "simulation",
                               {"simulation": {"cores": 9.0}}, roles=("simulation",),
                               settings=_settings_for("simulation"))
    assert out["cpu"] == 6, "clamped at the ceiling, not at the 0.5 reservation"


# -- sizing from the kernel's billing rather than a sum over processes ----------------


def test_cores_come_from_the_billing_counter_delta():
    """``cpu_usage_usec`` is monotonic CPU time, so cores is delta over elapsed wall time."""
    rows = [{"timestamp": "1000.0", "cpu_usage_usec": "0"},
            {"timestamp": "1001.0", "cpu_usage_usec": "2000000"},    # 2.0 cores over 1s
            {"timestamp": "1002.0", "cpu_usage_usec": "2500000"}]    # 0.5 cores over 1s
    got = nc.container_cpu_profile_from_billing(rows)
    assert got["cores"] == pytest.approx(2.0)
    assert got["samples"] == 2, "N rows give N-1 intervals"


def test_the_billing_reader_needs_no_ceiling_to_be_trusted():
    """The reason to prefer it: there is no impossible sample to discard.

    The per-process reader must be told the container's limit so it can throw away psutil's
    bring-up artifact -- a newly-seen process reports its average since it started, and a ROS
    stack spawning dozens at once reports totals no quota could have produced. A cgroup
    counter cannot exceed the CPU actually billed, so a value above the ceiling is a real
    measurement and is kept.
    """
    rows = [{"timestamp": "0.0", "cpu_usage_usec": "0"},
            {"timestamp": "1.0", "cpu_usage_usec": "9000000"}]       # 9 cores, no clamp
    assert nc.container_cpu_profile_from_billing(rows)["cores"] == pytest.approx(9.0)


def test_a_counter_reset_is_dropped_rather_than_read_as_idle():
    """A cgroup replaced mid-probe restarts the counter. A negative delta is not zero load."""
    rows = [{"timestamp": "0.0", "cpu_usage_usec": "5000000"},
            {"timestamp": "1.0", "cpu_usage_usec": "0"},             # reset
            {"timestamp": "2.0", "cpu_usage_usec": "1000000"}]
    got = nc.container_cpu_profile_from_billing(rows)
    assert got["samples"] == 1 and got["cores"] == pytest.approx(1.0)


def test_too_few_rows_is_not_measured_rather_than_zero():
    for rows in ([], [{"timestamp": "0.0", "cpu_usage_usec": "0"}]):
        assert nc.container_cpu_profile_from_billing(rows) == {}


def test_the_billing_file_wins_over_the_per_process_one():
    """Both present: the counter decides, and the per-process clamp never runs."""
    files = {
        "probe/resource_usage_sut.csv":
            b"timestamp,pid,name,cpu_percent\n1.0,1,a,900.0\n1.0,2,b,900.0\n",
        "probe/system_usage_sut.csv":
            b"timestamp,cpu_usage_usec\n1.0,0\n2.0,1500000\n",
    }
    got = nc.read_probe_measurement(lambda k: files.get(k), "probe/",
                                    {"sut": "resource_usage_sut.csv"}, limits={"sut": 3.0})
    assert got["sut"]["cores"] == pytest.approx(1.5), "the counter, not the summed processes"


def test_it_falls_back_where_the_node_could_not_answer():
    """No cpu.stat on the host means no sibling file -- and a calibratable node anyway."""
    files = {"probe/resource_usage_sut.csv":
             b"timestamp,pid,name,cpu_percent\n1.0,1,a,100.0\n2.0,1,a,200.0\n"}
    got = nc.read_probe_measurement(lambda k: files.get(k), "probe/",
                                    {"sut": "resource_usage_sut.csv"}, limits={"sut": 3.0})
    assert got["sut"]["cores"] == pytest.approx(2.0), "the per-process file still answers"


# -- a probe that measured its own ceiling ---------------------------------------------


def test_a_throttled_probe_is_refused():
    """The measurement would be of the limit, not of the demand.

    The probe runs at the declared sizing, so a ceiling that binds during it caps the peak it
    reports. Storing that writes the cap in as though it were what the container needed, and
    every later run on the node inherits it -- with nothing downstream able to tell a figure
    derived from a limit from one derived from a workload.
    """
    c = NodeCalibration()
    c.claim_probe("n1", "probe-1")
    stored = c.record("n1", "probe-1",
                      {"sut": {"cores": 1.0, "samples": 60,
                               "throttled_ratio": 0.05}})
    assert stored is False, "a probe that hit its own ceiling must not size the node"
    assert not c.calibrated("n1"), "the node keeps whatever it started on"


def test_throttling_under_the_threshold_is_kept():
    """Zero is the wrong bar: a container is briefly throttled during bring-up anywhere, and
    refusing on that would leave a cluster permanently uncalibrated."""
    c = NodeCalibration()
    c.claim_probe("n1", "probe-1")
    assert c.record("n1", "probe-1",
                    {"sut": {"cores": 1.0, "samples": 60,
                             "throttled_ratio": 0.001}}) is True


def test_a_node_that_cannot_report_throttling_is_still_calibrated():
    """Absent is not zero -- but refusing on absence would disable calibration on any host
    whose cgroup exposes no CPU accounting, which is the case the per-process reader exists
    to keep working."""
    c = NodeCalibration()
    c.claim_probe("n1", "probe-1")
    assert c.record("n1", "probe-1",
                    {"sut": {"cores": 1.0, "samples": 60}}) is True


def test_the_throttle_ratio_never_reaches_the_stored_figures():
    """It is evidence about the measurement, not a resource to size from."""
    c = NodeCalibration()
    c.claim_probe("n1", "probe-1")
    c.record("n1", "probe-1", {"sut": {"cores": 1.0, "samples": 60,
                                       "throttled_ratio": 0.001}})
    assert set(c.calibrated("n1")["sut"]) == {"cores"}


def test_the_billing_reader_reports_the_throttle_span():
    """Read from the same rows as the cores: monotonic counters, so last minus first."""
    rows = [{"timestamp": "0.0", "cpu_usage_usec": "0",
             "nr_periods": "100", "nr_throttled": "0"},
            {"timestamp": "1.0", "cpu_usage_usec": "1000000",
             "nr_periods": "200", "nr_throttled": "10"}]
    assert nc.container_cpu_profile_from_billing(rows)["throttled_ratio"] == pytest.approx(0.1)


def test_a_reader_with_no_throttle_columns_omits_the_ratio():
    """Rather than reporting zero, which would read as "measured, and clean"."""
    rows = [{"timestamp": "0.0", "cpu_usage_usec": "0"},
            {"timestamp": "1.0", "cpu_usage_usec": "1000000"}]
    assert "throttled_ratio" not in nc.container_cpu_profile_from_billing(rows)


def test_an_oom_killed_probe_is_refused():
    """A memory ceiling that binds kills rather than slows, so there is no ratio to weigh
    and no bring-up allowance: one kill means the container did not run to the end, and the
    file holds a fragment of a run that died rather than a measurement of one that
    finished."""
    c = NodeCalibration()
    c.claim_probe("n1", "probe-1")
    stored = c.record("n1", "probe-1",
                      {"simulation": {"cores": 1.0, "samples": 60,
                                      "oom_kills": 1}})
    assert stored is False
    assert not c.calibrated("n1"), "the node keeps whatever it started on"


def test_a_node_that_cannot_report_ooms_is_still_calibrated():
    """Absent is not zero, and refusing on absence would leave such a cluster permanently
    uncalibrated -- the same rule the throttle counter follows."""
    c = NodeCalibration()
    c.claim_probe("n1", "probe-1")
    assert c.record("n1", "probe-1",
                    {"sut": {"cores": 1.0, "samples": 60}}) is True


def test_the_oom_count_never_reaches_the_stored_figures():
    c = NodeCalibration()
    c.claim_probe("n1", "probe-1")
    c.record("n1", "probe-1", {"sut": {"cores": 1.0, "samples": 60,
                                       "oom_kills": 0}})
    assert set(c.calibrated("n1")["sut"]) == {"cores"}


def test_the_billing_reader_reports_oom_kills():
    rows = [{"timestamp": "0.0", "cpu_usage_usec": "0", "memory_events_oom_kill": "0"},
            {"timestamp": "1.0", "cpu_usage_usec": "1000000", "memory_events_oom_kill": "2"}]
    assert nc.container_cpu_profile_from_billing(rows)["oom_kills"] == 2


# -- what a probe's throttling costs depends on which statistic is read from it ----------


def test_a_sustained_sized_container_survives_clipping_the_percentile_discards():
    """Clipping removes the top of the distribution, so what it destroys depends on where the
    figure is taken from. A p95 already throws away the top 5%; ticks clipped inside that band
    cannot move it. Refusing such a probe leaves the node unmeasured to protect a number the
    distortion could not have reached."""
    c = NodeCalibration()
    c.claim_probe("n1", "probe-1")
    stored = c.record("n1", "probe-1",
                      {"simulation": {"cores": 1.0, "samples": 60,
                                      "throttled_ratio": 0.02}},
                      percentiles={"simulation": 95.0})
    assert stored is True, "a p95 is unmoved by clipping inside the tail it discards"
    assert c.calibrated("n1")


def test_a_peak_sized_container_does_not_get_that_tolerance():
    """The max is destroyed by the first clipped tick, so the same ratio that a p95 shrugs off
    makes a peak unusable -- which is why one threshold could not serve both."""
    c = NodeCalibration()
    c.claim_probe("n1", "probe-1")
    stored = c.record("n1", "probe-1",
                      {"sut": {"cores": 1.0, "samples": 60,
                               "throttled_ratio": 0.02}},
                      percentiles={"sut": 100.0})
    assert stored is False
    assert not c.calibrated("n1")


def test_a_caller_that_names_nothing_is_judged_strictly():
    """`None` is not "nothing is peak-sized": it is "the caller does not know". Accepting a
    distorted peak writes a wrong figure in silently; refusing only leaves the node
    unmeasured, and the next job there probes again."""
    c = NodeCalibration()
    c.claim_probe("n1", "probe-1")
    assert c.record("n1", "probe-1",
                    {"anything": {"cores": 1.0, "samples": 60,
                                  "throttled_ratio": 0.02}}) is False


def test_the_tolerance_is_tied_to_the_percentile_it_protects():
    """Not two numbers that happen to agree: the tolerance IS what the percentile discards, so
    moving the percentile moves it. Pinned so they cannot drift apart."""
    from robovast.execution.cluster_execution.node_calibration import (
        probe_refuse_ratio)

    assert probe_refuse_ratio(95.0) == pytest.approx(0.05)
    assert probe_refuse_ratio(100.0) < probe_refuse_ratio(95.0)


# -- the scenario runner's own report, on probes only -----------------------------------


def _tick_csv(rows):
    head = "tick,wall_ts,timestamp,interval_s,duration_s,period_s,driver\n"
    body = "".join(f"{i},0,0,{iv},0.01,{pd},ros_timer\n" for i, (iv, pd) in enumerate(rows, 1))
    return (head + body).encode()


def test_a_probe_that_held_its_tick_rate_is_calibrated():
    """The healthy case: achieved matches intended, so the measurement is of a container that
    had the CPU it needed."""
    from robovast.execution.cluster_execution.node_calibration import read_probe_tick_ratio

    ratio = read_probe_tick_ratio(lambda k: _tick_csv([(0.1, 0.1)] * 20), "")
    c = NodeCalibration()
    c.claim_probe("n1", "p1")
    assert c.record("n1", "p1", _GOOD, tick_ratio=ratio) is True


def test_a_probe_whose_scenario_could_not_keep_up_is_refused():
    """The scenario container is sized on a percentile with a ceiling it may burst into, so it
    can be starved for a whole run without ever hitting its quota -- invisible to the throttle
    counter that catches this for every other role. Its own tick rate is the only signal, and
    a measurement taken while it was starved would size every later run on that node."""
    from robovast.execution.cluster_execution.node_calibration import read_probe_tick_ratio

    ratio = read_probe_tick_ratio(lambda k: _tick_csv([(0.5, 0.1)] * 20), "")
    c = NodeCalibration()
    c.claim_probe("n1", "p1")
    assert c.record("n1", "p1", _GOOD, tick_ratio=ratio) is False
    assert "ticked at" in c.outcome()["refused"]["n1"]


def test_no_tick_log_is_not_a_healthy_tick_rate():
    """Absence is not a pass, the same rule the resource counters follow: a scenario runner
    that wrote no tick log was not measured, and reading that as "held its rate" would grade a
    campaign on a file that never existed."""
    from robovast.execution.cluster_execution.node_calibration import read_probe_tick_ratio

    assert read_probe_tick_ratio(lambda k: None, "") is None
    c = NodeCalibration()
    c.claim_probe("n1", "p1")
    assert c.record("n1", "p1", _GOOD, tick_ratio=None) is True


def test_one_slow_tick_is_not_a_starved_container():
    """A behaviour tree stalls for a moment on any machine -- a slow action, a blocking call.
    What starvation looks like is the whole distribution shifting, so the median decides."""
    from robovast.execution.cluster_execution.node_calibration import read_probe_tick_ratio

    rows = [(0.1, 0.1)] * 30 + [(9.0, 0.1)]
    assert read_probe_tick_ratio(lambda k: _tick_csv(rows), "") == pytest.approx(1.0)


# -- memory, measured by the same probe --------------------------------------------------


def test_memory_is_read_from_the_kernels_high_water_mark():
    """`memory_peak` rather than a sample of `memory_current`: the limit has to clear what the
    container actually reached, and a 1 Hz sample misses whatever happened between ticks."""
    from robovast.execution.cluster_execution.node_calibration import \
        container_cpu_profile_from_billing

    rows = [{"timestamp": str(i), "cpu_usage_usec": str(i * 100000),
             "memory_peak": str(500 * 1024 ** 2)} for i in range(40)]
    rows[20]["memory_peak"] = str(900 * 1024 ** 2)
    got = container_cpu_profile_from_billing(rows)
    assert got["memory_peak"] == 900 * 1024 ** 2


def test_memory_is_taken_at_the_maximum_for_every_role():
    """The CPU percentile has no meaning here. Exceeding a CPU reservation slows a container;
    exceeding a memory one kills it, so no role may be sized on a figure most of its samples
    sat below."""
    from robovast.execution.cluster_execution.node_calibration import \
        container_cpu_profile_from_billing

    rows = [{"timestamp": str(i), "cpu_usage_usec": str(i * 100000),
             "memory_peak": str(100 * 1024 ** 2)} for i in range(40)]
    rows[-1]["memory_peak"] = str(800 * 1024 ** 2)
    for percentile in (50, 95, 100):
        got = container_cpu_profile_from_billing(rows, percentile=percentile)
        assert got["memory_peak"] == 800 * 1024 ** 2, "the max, whatever the CPU percentile"
