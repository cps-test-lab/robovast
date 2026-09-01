# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""The quadrotor example invents nothing it could not measure, and aggregates pessimistically.

Three defects in one 53-line shipped example, each of them something the rest of RoboVAST
documents as forbidden, and each pointing the search the wrong way:

* ``failure_rate = failures / len(runs) if runs else 0.0``. The objective is MAXIMIZED, so
  0.0 is the least interesting score there is -- a cell whose every run died of
  infrastructure looked like a cell where nothing failed, and the search steered away from
  exactly the region it hunts. The built-in ``failure_rate`` extractor raises
  ``NoSampleError`` here and its docstring says why at length; this one did not.
* zero-valued measures when no run wrote a metrics row. Those are ARCHIVE COORDINATES, so
  ``(0, 0, 0, 0)`` is a real cell -- the calmest corner of the behaviour space -- and an
  unmeasurable configuration became an elite the search then chased. This is exactly what
  ``QDStrategy._tell_incomplete`` refuses to do for the same reason.
* the mean of each metric column. ``robovast.search.aggregate`` exists because of a
  measurement taken on THIS campaign: behaviour measures averaged over five runs filled 3
  of 512 archive cells, because averaging pulls every cell toward the middle of the
  behaviour space before the archive ever sees it. The example the measurement came from
  still averaged.
"""

import csv
import importlib.util
import pathlib

import pytest

from robovast.search.extractor import NoSampleError

EXAMPLE = (pathlib.Path(__file__).resolve().parents[2]
           / "configs" / "examples" / "quadrotor_landing")


def _extractor_cls():
    if not (EXAMPLE / "search" / "extract.py").is_file():
        pytest.skip("quadrotor_landing example not present")
    spec = importlib.util.spec_from_file_location(
        "quad_extract_under_test", EXAMPLE / "search" / "extract.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.QuadExtract


def _run(config_dir, index, *, passed=True, metrics=None, verdict=True):
    run_dir = config_dir / str(index)
    run_dir.mkdir(parents=True)
    if verdict:
        (run_dir / "test.xml").write_text(
            '<?xml version="1.0"?>'
            f'<testsuite tests="1" failures="{0 if passed else 1}" errors="0">'
            f'<testcase name="t">{"" if passed else "<failure message=\'x\'/>"}</testcase>'
            '</testsuite>', encoding="utf-8")
    if metrics is not None:
        with open(run_dir / "metrics.csv", "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(metrics))
            writer.writeheader()
            writer.writerow(metrics)
    return run_dir


def _m(max_tilt, drift_dist, landing_speed, control_effort):
    return {"max_tilt": max_tilt, "drift_dist": drift_dist,
            "landing_speed": landing_speed, "control_effort": control_effort}


def _extract(config_dir, **params):
    return _extractor_cls()(**params).extract(config_dir)


def test_a_cell_with_no_result_produces_no_sample(tmp_path):
    """Not a maximized objective's least interesting value."""
    config_dir = tmp_path / "c0"
    config_dir.mkdir()

    with pytest.raises(NoSampleError) as excinfo:
        _extract(config_dir)

    assert "nothing failed" in str(excinfo.value)


def test_a_cell_with_verdicts_but_no_metrics_produces_no_sample(tmp_path):
    """Not the archive's calmest corner."""
    config_dir = tmp_path / "c0"
    _run(config_dir, 0)
    _run(config_dir, 1)

    with pytest.raises(NoSampleError) as excinfo:
        _extract(config_dir)

    message = str(excinfo.value)
    assert "no behaviour to place in the archive" in message
    assert "trajectory" in message              # names what to check


def test_the_measures_take_the_worst_run_not_the_mean(tmp_path):
    """Every axis is a cost, so the pessimistic end is the maximum. The mean is what
    collapsed a 512-cell archive onto 3 cells."""
    config_dir = tmp_path / "c0"
    _run(config_dir, 0, metrics=_m(0.10, 0.5, 1.0, 2.0))
    _run(config_dir, 1, metrics=_m(0.70, 3.0, 4.0, 18.0))

    result = _extract(config_dir)

    assert result.measures == {"max_tilt": 0.70, "drift_dist": 3.0,
                               "landing_speed": 4.0, "control_effort": 18.0}
    # The mean would have put every one of them mid-range -- 0.40, 1.75, 2.5, 10.0.
    assert result.measures["max_tilt"] != pytest.approx(0.40)


def test_the_mean_is_still_available_by_name(tmp_path):
    config_dir = tmp_path / "c0"
    _run(config_dir, 0, metrics=_m(0.10, 0.5, 1.0, 2.0))
    _run(config_dir, 1, metrics=_m(0.70, 3.0, 4.0, 18.0))

    result = _extract(config_dir, aggregate="mean")

    assert result.measures["max_tilt"] == pytest.approx(0.40)


def test_the_objective_counts_every_completed_run(tmp_path):
    config_dir = tmp_path / "c0"
    _run(config_dir, 0, passed=True, metrics=_m(0.1, 0.5, 1.0, 2.0))
    _run(config_dir, 1, passed=False, metrics=_m(0.2, 0.6, 1.1, 2.1))
    _run(config_dir, 2, passed=False, metrics=_m(0.3, 0.7, 1.2, 2.2))

    result = _extract(config_dir)

    assert result.objectives["failure_rate"] == pytest.approx(2 / 3)


def test_a_run_without_metrics_is_left_out_and_reported(tmp_path, caplog):
    config_dir = tmp_path / "c0"
    _run(config_dir, 0, metrics=_m(0.10, 0.5, 1.0, 2.0))
    _run(config_dir, 1)                                   # verdict, no metrics

    with caplog.at_level("WARNING"):
        result = _extract(config_dir)

    assert result.measures["max_tilt"] == 0.10            # scored over the one it had
    assert result.objectives["failure_rate"] == 0.0       # but both runs count here
    assert "1 of 2 run(s) produced no 'metrics.csv'" in caplog.text
