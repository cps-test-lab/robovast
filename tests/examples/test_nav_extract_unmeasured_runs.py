# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""A run that wrote a verdict but no metrics row is not scored as a boundary crossing.

``NavExtract`` refuses to invent a score for a cell with no ``test.xml`` -- its own comment
says why: *"0.0 would look like a cell sitting exactly on the boundary, which is the most
interesting thing a boundary search can find."* A run WITH a verdict and WITHOUT a metrics
row slipped past that guard and produced exactly the number the guard exists to prevent.

Every reader defaulted independently, and the defaults compose into a plausible-looking
disaster::

    duration = _f(row, 'duration_s', timeout)     # -> the timeout
    margins  = [(timeout - duration) / timeout]   # -> 0.0, precisely

so two clean, roomy crossings plus one unmeasured run scored ``robustness: 0.0``:
the failure boundary, which is what an adversarial search minimizes toward and literally
the ``level`` the ``boundary`` strategy traces. It also reported ``time_to_goal`` at the
timeout and a ``failure_mode`` of ``'timeout'`` -- and that one is a QD archive axis, so
the fabrication lands in a real archive cell and becomes an elite the search then chases.

Reachable without anything else being wrong: ``NavMetrics`` writes no row for a run whose
ground-truth pose track is missing, and counts them in its own note.
"""

import importlib.util
import pathlib

import pytest

EXAMPLE = pathlib.Path(__file__).resolve().parents[2] / "configs" / "examples" / "nav_search"


def _extractor_cls():
    if not (EXAMPLE / "search" / "nav_extract.py").is_file():
        pytest.skip("nav_search example not present")
    spec = importlib.util.spec_from_file_location(
        "nav_extract_unmeasured", EXAMPLE / "search" / "nav_extract.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.NavExtract


def _run(config_dir, index, *, metrics=True, clearance=0.9, duration=20.0, to_goal=0.1):
    run_dir = config_dir / str(index)
    run_dir.mkdir(parents=True)
    (run_dir / "test.xml").write_text(
        '<?xml version="1.0"?><testsuite tests="1" failures="0" errors="0">'
        '<testcase name="t"/></testsuite>', encoding="utf-8")
    if metrics:
        (run_dir / "nav_metrics.csv").write_text(
            "min_clearance,duration_s,final_distance_to_goal,collided,recovery_count\n"
            f"{clearance},{duration},{to_goal},0,0\n", encoding="utf-8")
    return run_dir


def _extract(config_dir):
    return _extractor_cls()(**{"timeout": 120.0}).extract(config_dir)


def test_an_unmeasured_run_does_not_drag_a_safe_cell_onto_the_boundary(tmp_path):
    """Two roomy crossings and one unmeasured run. The cell is safe, and must score so."""
    config_dir = tmp_path / "c0"
    _run(config_dir, 0)
    _run(config_dir, 1)
    _run(config_dir, 2, metrics=False)

    result = _extract(config_dir)

    assert result.objectives["robustness"] > 0.0          # was exactly 0.0
    assert result.measures["time_to_goal"] == 20.0        # was 120.0, the timeout
    assert result.measures["failure_mode"] == "none"      # was 'timeout', invented


def test_the_unmeasured_run_is_reported(tmp_path, caplog):
    """`n_samples` counts completed runs, so it is larger than what was aggregated --
    which has to be visible rather than inferred."""
    config_dir = tmp_path / "c0"
    _run(config_dir, 0)
    _run(config_dir, 1, metrics=False)

    with caplog.at_level("WARNING"):
        _extract(config_dir)

    assert "1 of 2 run(s) produced no 'nav_metrics.csv' row" in caplog.text


def test_a_cell_whose_every_run_is_unmeasured_produces_no_sample(tmp_path):
    """NoSampleError, not a score: the cell RAN and recorded nothing about the crossing,
    which is the case the framework records and carries on from."""
    from robovast.search.extractor import NoSampleError

    config_dir = tmp_path / "c0"
    _run(config_dir, 0, metrics=False)
    _run(config_dir, 1, metrics=False)

    with pytest.raises(NoSampleError) as excinfo:
        _extract(config_dir)

    message = str(excinfo.value)
    assert "no 'nav_metrics.csv' row" in message
    assert "ground-truth pose track" in message           # names the likely cause


def test_a_failing_run_is_still_scored_from_its_own_measurements(tmp_path):
    """The guard must not swallow a real failure: this run measured a collision."""
    config_dir = tmp_path / "c0"
    _run(config_dir, 0, clearance=0.0, duration=15.0, to_goal=3.0)

    result = _extract(config_dir)

    assert result.objectives["robustness"] < 0.0
    assert result.measures["failure_mode"] == "goal_miss"


def test_failure_rate_still_counts_every_completed_run(tmp_path):
    """It reads test.xml, which an unmeasured run still wrote, so its denominator is
    legitimately larger than what the margins aggregated over."""
    config_dir = tmp_path / "c0"
    _run(config_dir, 0)
    _run(config_dir, 1, metrics=False)

    result = _extract(config_dir)

    assert result.measures["failure_rate"] == 0.0         # both runs passed, over 2
