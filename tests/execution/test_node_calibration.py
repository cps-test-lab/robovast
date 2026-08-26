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


def test_one_probe_per_node_at_a_time():
    """The trap that would cost a campaign several runs instead of one.

    A batch lands several jobs on a node before any has finished. Without the claim every one
    of them is a probe and every one is discarded, so the campaign pays for its calibration
    over and over and loses the runs it paid with.
    """
    c = NodeCalibration()
    assert c.claim_probe("n1", "j0") is True
    assert c.claim_probe("n1", "j1") is False
    assert c.claim_probe("n2", "j2") is True, "a different node is a different probe"
    assert c.should_discard("j0") and not c.should_discard("j1")


def test_a_calibrated_node_never_probes_again():
    """Frozen. Continuing to adapt would mean run 5 and run 40 on the same node ran in
    different environments -- the same inconsistency this removes, in a slower form."""
    c = NodeCalibration()
    c.claim_probe("n1", "j0")
    c.record("n1", "j0", {"sut": 2.0})
    assert c.claim_probe("n1", "j9") is False
    assert c.calibrated("n1") == {"sut": pytest.approx(2.0 * CALIBRATION_HEADROOM)}


def test_headroom_is_applied_because_a_peak_is_one_sample():
    """Sizing exactly at what one run measured guarantees the next run is clipped."""
    c = NodeCalibration()
    c.claim_probe("n1", "j0")
    c.record("n1", "j0", {"sut": 2.0, "simulation": 0.4})
    assert c.calibrated("n1")["sut"] == pytest.approx(2.5)
    assert c.calibrated("n1")["simulation"] == pytest.approx(0.5)


def test_a_container_that_did_almost_nothing_still_gets_a_floor():
    """A trial that failed early, or a simulator that never got past bring-up, would otherwise
    pin the node to a figure the next run cannot live in."""
    c = NodeCalibration()
    c.claim_probe("n1", "j0")
    c.record("n1", "j0", {"sut": 0.001})
    assert c.calibrated("n1")["sut"] == MIN_CPU


def test_a_probe_that_measured_nothing_leaves_the_node_uncalibrated():
    """Silence is not a measurement of zero. The node must stay on the declared sizing and let
    the next job try, rather than freeze on a figure derived from no data."""
    c = NodeCalibration()
    c.claim_probe("n1", "j0")
    assert c.record("n1", "j0", {}) is False
    assert c.calibrated("n1") is None
    assert not c.should_discard("j0"), "a run whose data was unusable is still a real run"
    assert c.claim_probe("n1", "j1") is True, "the node is free to be probed again"


def test_an_abandoned_probe_keeps_its_results_and_frees_the_node():
    """A probe that dies, or is dropped for a restart, never reports. Its run happened at the
    declared sizing -- the same one every other run on an uncalibrated node used -- so it is
    exactly as comparable as they are and there is no reason to throw it away. Only a probe
    whose figures were ADOPTED has to be dropped."""
    c = NodeCalibration()
    c.claim_probe("n1", "j0")
    c.abandon("n1", "j0")
    assert not c.should_discard("j0")
    assert c.claim_probe("n1", "j1") is True


def test_recording_against_the_wrong_job_is_refused():
    """Only the job that claimed the node may calibrate it; otherwise a late finisher could
    overwrite the figures every later run was already sized with."""
    c = NodeCalibration()
    c.claim_probe("n1", "j0")
    assert c.record("n1", "j-other", {"sut": 9.0}) is False
    assert c.calibrated("n1") is None


def test_disabled_calibration_claims_nothing():
    c = NodeCalibration(enabled=False)
    assert c.claim_probe("n1", "j0") is False
    assert not c.should_discard("j0")
