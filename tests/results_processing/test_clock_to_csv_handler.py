# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Clock-map decimation: keep the shape of wall<->sim, not every ``/clock`` message.

/clock arrives at 100-1000 Hz, so storing every message would put tens of thousands of
rows per run into an artifact whose whole content is a straight line most of the time. A
sample is dropped only when linear interpolation reproduces it within tolerance, so what
survives is the *shape*: steady stretches cost almost nothing, and a pause or a change of
rate keeps exactly the samples that describe it.

Tested against :class:`ClockDecimator` rather than the rosbag handler wrapping it: that
handler's module imports ``rosbag2_py`` at load time and so only exists inside a ROS
image, while the promise being checked here is pure arithmetic.
"""

import csv

import pytest

from robovast.results_processing.clock_map import (DEFAULT_TOLERANCE_S, FIELDNAMES, Decimator,
                                                   load_clock_map)

DEFAULT = DEFAULT_TOLERANCE_S


def _run(tolerance, tmp_path, pairs) -> tuple[int, str]:
    """Decimate *pairs* at *tolerance*, write the CSV; return ``(kept, path)``."""
    kept = Decimator(tolerance).run(pairs)
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "clock_map.csv"
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        for wall, sim in kept:
            writer.writerow({"wall_ts": wall, "sim_ts": sim})
    return len(kept), str(path)


def test_a_steady_rate_collapses_to_its_endpoints(tmp_path):
    """1000 messages describing one straight line are two samples' worth of information."""
    pairs = [(100.0 + i * 0.01, i * 0.01 * 1.369) for i in range(1000)]
    kept, path = _run(DEFAULT, tmp_path, pairs)
    assert kept <= 4, "a constant real-time factor should not cost 1000 rows"
    # And the decimated map still answers correctly in the middle of the dropped stretch.
    assert load_clock_map(path).to_sim(105.0) == pytest.approx(5.0 * 1.369, abs=0.005)


def test_a_change_of_rate_keeps_the_corner(tmp_path):
    """Sim runs at 1x, then 2x. The corner is the one sample that must not be dropped, or
    the map reports the whole run at some average nobody ran at."""
    pairs = [(100.0 + i * 0.1, i * 0.1) for i in range(51)]            # 1x for 5 s
    pairs += [(105.0 + i * 0.1, 5.0 + i * 0.2) for i in range(1, 51)]  # then 2x
    kept, path = _run(DEFAULT, tmp_path, pairs)
    m = load_clock_map(path)
    assert m.to_sim(102.5) == pytest.approx(2.5, abs=0.01)
    assert m.to_sim(107.5) == pytest.approx(10.0, abs=0.01)
    assert kept < 15


def test_a_pause_survives_decimation(tmp_path):
    """Sim stops for 2 wall seconds. Interpolating across that would report the robot as
    having kept moving through it."""
    pairs = [(100.0 + i * 0.1, i * 0.1) for i in range(21)]            # 0 -> 2 s
    pairs += [(102.0 + i * 0.1, 2.0) for i in range(1, 21)]            # paused
    pairs += [(104.0 + i * 0.1, 2.0 + i * 0.1) for i in range(1, 21)]  # resumes
    _, path = _run(DEFAULT, tmp_path, pairs)
    m = load_clock_map(path)
    assert m.to_sim(103.0) == pytest.approx(2.0, abs=0.01)
    assert m.to_sim(105.0) == pytest.approx(3.0, abs=0.01)


def test_the_last_sample_is_always_written(tmp_path):
    """The map's right edge is where a run stopped; losing it would leave the final lines
    of the log outside the range and therefore unplaceable."""
    pairs = [(100.0, 0.0), (101.0, 1.0), (102.0, 2.0), (103.0, 3.0)]
    _, path = _run(DEFAULT, tmp_path, pairs)
    m = load_clock_map(path)
    assert m.to_sim(103.0) == pytest.approx(3.0)
    assert m.to_sim(103.5) is None


def test_no_samples_at_all_decimates_to_nothing(tmp_path):
    """A campaign may record only /rosout, and a non-ROS run has no /clock at all. The
    absence must stay an absence — no rows, and therefore no map, never a zero offset."""
    kept, path = _run(DEFAULT, tmp_path, [])
    assert kept == 0
    assert load_clock_map(path).info.source == "none"


def test_tolerance_is_configurable_and_bounds_the_error(tmp_path):
    """A tighter tolerance keeps more samples; the promise is that what was dropped is
    reproducible within it."""
    pairs = [(100.0 + i * 0.05, (i * 0.05) ** 1.5) for i in range(200)]  # curved
    loose, loose_path = _run(0.05, tmp_path / "a", pairs)
    tight, tight_path = _run(0.001, tmp_path / "b", pairs)
    assert tight > loose
    truth = dict(pairs)
    for wall, sim in list(truth.items())[1:-1]:
        assert load_clock_map(loose_path).to_sim(wall) == pytest.approx(sim, abs=0.05)
        assert load_clock_map(tight_path).to_sim(wall) == pytest.approx(sim, abs=0.001)
