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
    c.record("n1", "j0", {"sut": {"sustained": 1.4, "peak": 2.0}})
    assert c.claim_probe("n1", "j9") is False
    assert c.calibrated("n1")["sut"]["peak"] == pytest.approx(2.0 * CALIBRATION_HEADROOM)


def test_headroom_is_applied_because_a_peak_is_one_sample():
    """Sizing exactly at what one run measured guarantees the next run is clipped."""
    c = NodeCalibration()
    c.claim_probe("n1", "j0")
    c.record("n1", "j0", {"sut": {"sustained": 1.4, "peak": 2.0},
                          "simulation": {"sustained": 0.4, "peak": 4.8}})
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
    c.record("n1", "j0", {"sut": {"sustained": 0.001, "peak": 0.001}})
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
    c.record("n1", "probe-1", {"sut": {"sustained": 1.0, "peak": 2.0}})
    assert c.accepts_work("n1") is True


def test_the_probe_directory_is_not_a_configuration():
    """The whole mechanism by which a probe is never a campaign run: it writes somewhere
    nothing walks looking for runs, so it is never ADDED rather than added and removed."""
    from robovast.common.campaign_data import RESERVED_CAMPAIGN_DIRS

    assert "_calibration" in RESERVED_CAMPAIGN_DIRS
