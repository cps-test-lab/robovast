# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""A run that recorded no recovery behaviours is still measurable.

``NavMetrics`` reads three recorded tables and treats them differently on purpose: the
collision oracle is refused when absent (a fabricated ``collided = 0`` is the single most
misleading value this plugin could write), clearance degrades to an empty cell so the
extractor drops that margin, and the behaviour transitions are simply optional -- a run that
needed no recovery records none, and ``recovery_count`` is a QD measure rather than part of
the verdict.

The third was written as a guard inside a generator expression::

    sum(1 for r in _rows(behaviour_csv) if behaviour_csv ...)

which guards nothing: the source expression is evaluated before any condition runs, so
``_rows(None)`` was called every time and raised ``AttributeError: 'NoneType' object has no
attribute 'exists'``. `NavMetrics` runs as ``search.postprocessing``, before each batch is
scored, so on a campaign whose bags carry no behaviour topic that aborted every batch -- and
the traceback named ``NoneType``, nowhere near the missing table.
"""

import csv
import importlib.util
import pathlib

import pytest

EXAMPLE = pathlib.Path(__file__).resolve().parents[2] / "configs" / "examples" / "nav_search"


def _module():
    if not (EXAMPLE / "search" / "nav_metrics.py").is_file():
        pytest.skip("nav_search example not present")
    spec = importlib.util.spec_from_file_location(
        "nav_metrics_under_test", EXAMPLE / "search" / "nav_metrics.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_dir(tmp_path, *, clearance=True, collision=True, behaviours=False):
    run = tmp_path / "c0" / "0"
    run.mkdir(parents=True)
    (run / "poses.csv").write_text(
        "timestamp,frame,position.x,position.y\n"
        "0.0,base_link_gt,-2.5,0.0\n"
        "9.0,base_link_gt,2.4,0.0\n", encoding="utf-8")
    if collision:
        (run / "rosbag2_collision.csv").write_text("data\nfalse\n", encoding="utf-8")
    if clearance:
        (run / "rosbag2_clearance.csv").write_text("data\n0.42\n", encoding="utf-8")
    if behaviours:
        with open(run / "nav2_behaviors.csv", "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["behavior_name", "status_name"])
            writer.writeheader()
            writer.writerow({"behavior_name": "spin", "status_name": "RUNNING"})
            writer.writerow({"behavior_name": "backup", "status_name": "RUNNING"})
            writer.writerow({"behavior_name": "spin", "status_name": "SUCCEEDED"})
    return run


def test_a_run_without_a_behaviours_table_is_measured_with_no_recoveries(tmp_path):
    module = _module()
    run = _run_dir(tmp_path, behaviours=False)

    metrics = module._metrics_for_run(run, "poses.csv", "_gt", (2.5, 0.0))

    assert metrics is not None
    assert metrics["recovery_count"] == 0
    assert metrics["min_clearance"] == 0.42
    assert metrics["duration_s"] == 9.0


def test_a_behaviours_table_is_still_counted_when_present(tmp_path):
    module = _module()
    run = _run_dir(tmp_path, behaviours=True)

    metrics = module._metrics_for_run(run, "poses.csv", "_gt", (2.5, 0.0))

    assert metrics["recovery_count"] == 2      # the two RUNNING transitions, not the third


def test_the_plugin_writes_a_metrics_row_for_such_a_run(tmp_path):
    """End to end through the plugin, which is what the search loop calls."""
    module = _module()
    run = _run_dir(tmp_path, behaviours=False)

    ok, note = module.NavMetrics()(str(tmp_path), str(tmp_path / "c0"))

    assert ok
    assert "1 run(s)" in note
    with open(run / "nav_metrics.csv", newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    assert row["recovery_count"] == "0"


def test_a_missing_collision_oracle_is_still_refused(tmp_path):
    """The distinction under test: optional is not the same as absent-and-fine. The
    collision table decides the verdict, so its absence must stay an error."""
    module = _module()
    run = _run_dir(tmp_path, collision=False)

    with pytest.raises(FileNotFoundError):
        module._metrics_for_run(run, "poses.csv", "_gt", (2.5, 0.0))


def test_a_missing_clearance_table_still_leaves_an_empty_cell(tmp_path):
    module = _module()
    run = _run_dir(tmp_path, clearance=False)

    metrics = module._metrics_for_run(run, "poses.csv", "_gt", (2.5, 0.0))

    assert metrics["min_clearance"] == ""
