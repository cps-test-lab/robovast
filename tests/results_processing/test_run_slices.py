# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The two partitions of a packed job's timeline, side by side.

Both exist because a packed job may run several *different configurations* in one container,
so another run's data is a different experiment rather than context. They divide the GAP
between two runs in opposite directions, and that is the whole subtlety: a run's ``test.xml``
duration closes when its scenario stops, but it keeps logging after that — its verdict lands
about a millisecond late, then its teardown.

The behaviour is pinned end-to-end in ``test_postprocessing_plugins_e2e``; this states the
difference directly, because reading it out of two similar functions is how it gets broken.
"""

import math

from robovast.results_processing.run_slices import (_claims_for_job,
                                                   _log_claims_for_job,
                                                   log_claims_from_markers)

#: Two runs of one job: run 0 executes 100..105, run 1 executes 110..115. The gap 105..110 is
#: run 0's verdict and teardown, and run 1's bring-up.
_ENDS = [("cfg/0", 105.0), ("cfg/1", 115.0)]
_STARTS = [("cfg/0", 100.0), ("cfg/1", 110.0)]


def test_a_measurement_gives_the_gap_to_the_run_starting_up():
    """A tick during the reset is the cost of starting the next run, so run 1 claims it."""
    claims = _claims_for_job(_ENDS)
    assert claims["cfg/0"] == (-math.inf, 105.0)
    assert claims["cfg/1"] == (105.0, math.inf)


def test_a_log_gives_the_gap_to_the_run_that_was_finishing():
    """What fills the gap is run 0's verdict and its shutdown lines, so run 0 claims it.

    Cut the other way -- at run 0's ``end_epoch`` -- and a failing run's own verdict, stamped
    ~1 ms after its window closes, is filed under its successor. That is the bug.
    """
    claims = _log_claims_for_job(_STARTS)
    assert claims["cfg/0"] == (-math.inf, 110.0)
    assert claims["cfg/1"] == (110.0, math.inf)
    assert claims["cfg/0"][0] <= 105.0011 < claims["cfg/0"][1]


def test_the_middle_run_of_three_is_bounded_on_both_sides():
    """The partition has no holes and no overlaps, so every line lands in exactly one run."""
    claims = _log_claims_for_job(
        [("cfg/0", 100.0), ("cfg/1", 110.0), ("cfg/2", 120.0)])
    assert claims["cfg/1"] == (110.0, 120.0)
    assert claims["cfg/0"] == (-math.inf, 110.0)
    assert claims["cfg/2"] == (120.0, math.inf)


def test_a_lone_run_claims_everything_even_without_a_window():
    """Its whole trace is its own -- there is nobody else in the job to confuse it with."""
    assert _log_claims_for_job([("cfg/0", None)])["cfg/0"] == (-math.inf, math.inf)


def test_the_markers_themselves_become_the_boundaries():
    """Measured on a real campaign: ``Executing scenario`` is logged 33-44 us BEFORE the run's
    ``test.xml`` start. So a start_epoch boundary leaves each run's own marker in its
    predecessor's share, and the markers have to be the boundaries instead."""
    claims = log_claims_from_markers(_STARTS, [99.999965, 109.999967])
    assert claims["cfg/0"] == (-math.inf, 109.999967)
    assert claims["cfg/1"] == (109.999967, math.inf)
    # each run's own marker is now inside its own share
    assert claims["cfg/0"][0] <= 99.999965 < claims["cfg/0"][1]
    assert claims["cfg/1"][0] <= 109.999967 < claims["cfg/1"][1]


def test_the_first_run_still_owns_the_containers_bring_up():
    """Its share opens at -inf, not at its own marker: everything the container said before
    the first scenario started is real output and belongs to somebody."""
    claims = log_claims_from_markers(_STARTS, [99.999965, 109.999967])
    assert claims["cfg/0"][0] == -math.inf


def test_a_missing_marker_falls_back_rather_than_shifting_every_run():
    """rosout is only recorded once subscribed, so a job's first marker can be absent. Mapping
    marker *i* to run *i* would then shift every boundary by one and silently hand each run its
    neighbour's trial -- far worse than the microseconds the start_epoch boundaries cost."""
    assert log_claims_from_markers(_STARTS, [109.999967]) is None
    assert log_claims_from_markers(_STARTS, []) is None
    assert log_claims_from_markers(_STARTS, [99.9, 109.9, 119.9]) is None


def test_an_unorderable_run_falls_back_too():
    """A run with no ``test.xml`` cannot be placed among the markers at all."""
    assert log_claims_from_markers([("cfg/0", 100.0), ("cfg/1", None)], [99.9, 109.9]) is None


def test_a_windowless_run_sharing_a_job_claims_nothing():
    """It cannot be ordered against the others, and giving it everything is what handed it
    another configuration's verdict. Its placeable neighbours still cover all of time."""
    claims = _log_claims_for_job([("cfg/0", 100.0), ("cfg/1", None)])
    assert math.isnan(claims["cfg/1"][0]) and math.isnan(claims["cfg/1"][1])
    assert claims["cfg/0"] == (-math.inf, math.inf)
