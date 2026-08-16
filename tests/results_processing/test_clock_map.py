# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Wall→sim mapping: drift is what a single offset gets wrong, so drift is what is tested.

The numbers come from a real recording, campaign
``tiago-stepped-parity-2026-08-08-23330809``'s ``run.npz``, where sim time advanced
1.369× wall time.
"""

import pytest

from robovast.results_processing.clock_map import (FIELDNAMES, NO_CLOCK_MAP, SOURCE_NONE, ClockMap,
                                                   load_clock_map)


def _at_rate(rate: float, n: int = 11, wall0: float = 1786224804.0) -> ClockMap:
    """A map where sim advances *rate* seconds per wall second, sampled every wall second."""
    return ClockMap([(wall0 + i, i * rate) for i in range(n)])


# -- the reason this is sampled and not an offset ------------------------------


def test_a_real_time_factor_above_one_is_followed_not_offset():
    """The measured case: 1.369x. A start-anchored offset would put a line logged 21 wall
    seconds in at sim 21; it belongs at sim ~28.7."""
    m = _at_rate(1.369, n=30)
    assert m.to_sim(1786224804.0 + 21) == pytest.approx(28.749, abs=1e-3)
    # Not 21: that is what anchoring an offset at the start would have produced.
    assert m.to_sim(1786224804.0 + 21) != pytest.approx(21.0, abs=0.1)


def test_a_real_time_factor_below_one_is_followed_too():
    m = _at_rate(0.5)
    assert m.to_sim(1786224804.0 + 10) == pytest.approx(5.0)


def test_a_pause_holds_sim_time_still():
    """A simulator that stopped for 5 wall seconds: every wall instant in the pause maps to
    the same sim time, and time resumes from there rather than from where it 'should' be."""
    m = ClockMap([(100.0, 0.0), (110.0, 10.0), (115.0, 10.0), (125.0, 20.0)])
    assert m.to_sim(110.0) == pytest.approx(10.0)
    assert m.to_sim(112.5) == pytest.approx(10.0)
    assert m.to_sim(115.0) == pytest.approx(10.0)
    assert m.to_sim(120.0) == pytest.approx(15.0)


def test_interpolation_is_exact_at_the_samples():
    m = _at_rate(1.369)
    for i in range(11):
        assert m.to_sim(1786224804.0 + i) == pytest.approx(i * 1.369)


# -- outside the range there is no answer -------------------------------------


def test_before_the_first_sample_there_is_no_sim_time():
    """The samples start when the simulator started publishing /clock. A line from image
    boot has no sim time — extrapolating backwards would invent one for a clock that was
    not running yet."""
    m = _at_rate(1.0)
    assert m.to_sim(1786224803.0) is None


def test_after_the_last_sample_there_is_no_sim_time():
    m = _at_rate(1.0)
    assert m.to_sim(1786224804.0 + 99) is None


def test_a_map_with_fewer_than_two_samples_answers_nothing():
    """One sample fixes an instant but no rate, and guessing the rate is the mistake this
    module exists to avoid."""
    assert ClockMap([(100.0, 1.0)]).to_sim(100.0) is None
    assert not ClockMap([(100.0, 1.0)])
    assert NO_CLOCK_MAP.to_sim(100.0) is None


def test_none_wall_stays_none():
    assert _at_rate(1.0).to_sim(None) is None


# -- provenance ---------------------------------------------------------------


def test_info_reports_what_the_map_covers():
    info = _at_rate(1.369).info
    assert info.samples == 11
    assert info.wall_span_s == pytest.approx(10.0)
    assert info.source == "ros_clock_bag"


def test_an_empty_map_reports_source_none_rather_than_claiming_a_source():
    assert ClockMap([], "ros_clock_bag").info.source == SOURCE_NONE


# -- loading ------------------------------------------------------------------


def test_a_missing_file_is_no_map_not_an_error(tmp_path):
    """The normal case for a non-ROS run, and for a campaign postprocessed before the
    clock handler existed."""
    assert load_clock_map(str(tmp_path / "absent.csv")) is NO_CLOCK_MAP


def test_a_csv_round_trips_and_is_sorted(tmp_path):
    path = tmp_path / "clock_map.csv"
    path.write_text("\n".join([",".join(FIELDNAMES), "110,10", "100,0"]) + "\n")
    m = load_clock_map(str(path))
    assert m.to_sim(105.0) == pytest.approx(5.0)


def test_unparsable_rows_are_dropped_rather_than_poisoning_the_map(tmp_path):
    path = tmp_path / "clock_map.csv"
    path.write_text("\n".join([",".join(FIELDNAMES), "100,0", "abc,def", "110,10"]) + "\n")
    assert load_clock_map(str(path)).to_sim(105.0) == pytest.approx(5.0)


def test_a_header_only_csv_is_no_map(tmp_path):
    path = tmp_path / "clock_map.csv"
    path.write_text(",".join(FIELDNAMES) + "\n")
    assert load_clock_map(str(path)) is NO_CLOCK_MAP
