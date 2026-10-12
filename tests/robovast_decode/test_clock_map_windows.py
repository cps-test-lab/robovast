# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The realtime factor per simulated second, from a run's clock map."""

import pytest

from robovast_decode.clock_map import NO_CLOCK_MAP, ClockMap


def test_each_window_carries_the_factor_measured_inside_it():
    # 0..1 s of sim time in 1 s of wall time, then 1..2 s in 2 s of wall time.
    samples = [(0.0, 0.0), (0.5, 0.5), (0.99, 0.99), (1.0, 1.0), (2.0, 1.5), (2.98, 1.99)]
    windows = ClockMap(samples).windows(1.0)
    assert [(w[0], w[1]) for w in windows] == [(0.0, 1.0), (1.0, 2.0)]
    assert windows[0][3] / windows[0][2] == pytest.approx(1.0)
    assert windows[1][3] / windows[1][2] == pytest.approx(0.99 / 1.98)


def test_a_window_with_one_sample_has_no_factor():
    assert ClockMap([(0.0, 0.0), (0.5, 0.5), (3.0, 1.2)]).windows(1.0) == [(0.0, 1.0, 0.5, 0.5)]


def test_no_clock_map_has_no_windows():
    assert NO_CLOCK_MAP.windows(1.0) == []
