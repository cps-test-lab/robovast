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
attribute 'exists'``. ``NavMetrics`` runs as ``search.postprocessing``, before each batch is
scored, so on a campaign whose bags carry no behaviour topic that aborted every batch -- and
the traceback named ``NoneType``, nowhere near the missing table.

Each run here carries its tables as its own files and no recording, so ``poses.csv`` is its
``poses`` table and ``rosbag2_collision.csv`` its ``rosbag2_collision`` table.
"""

import csv
import importlib.util
import pathlib

import pytest

from tests.robovast_data.conftest import write_store

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
    campaign = tmp_path / "nav-2026-01-01-00000000"
    run = campaign / "c0" / "0"
    run.mkdir(parents=True)
    write_store(campaign, {"c0": {"runs": {0: "passed"}}})
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

    metrics = module._metrics_for_run(run, "poses", "_gt", (2.5, 0.0))

    assert metrics is not None
    assert metrics["recovery_count"] == 0
    assert metrics["min_clearance"] == 0.42
    assert metrics["duration_s"] == 9.0


def test_a_behaviours_table_is_still_counted_when_present(tmp_path):
    module = _module()
    run = _run_dir(tmp_path, behaviours=True)

    metrics = module._metrics_for_run(run, "poses", "_gt", (2.5, 0.0))

    assert metrics["recovery_count"] == 2      # the two RUNNING transitions, not the third


def test_the_plugin_writes_a_metrics_row_for_such_a_run(tmp_path):
    """End to end through the plugin, which is what the search loop calls."""
    module = _module()
    run = _run_dir(tmp_path, behaviours=False)

    ok, note = module.NavMetrics()(str(run.parent.parent), str(tmp_path))

    assert ok
    assert "1 run(s)" in note
    with open(run / "nav_metrics.csv", newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    assert row["recovery_count"] == "0"


def test_a_run_already_measured_is_left_unless_forced(tmp_path):
    module = _module()
    run = _run_dir(tmp_path)
    plugin = module.NavMetrics()
    plugin(str(run.parent.parent), str(tmp_path))

    _ok, note = plugin(str(run.parent.parent), str(tmp_path))
    assert "0 run(s) (1 up-to-date)" in note
    _ok, note = plugin(str(run.parent.parent), str(tmp_path), force=True)
    assert "for 1 run(s)" in note


def test_the_recorded_tables_are_read_from_the_recording(tmp_path):
    """The decoder's fixture recording: its ``/collision`` topic is the collision table."""
    from tests.robovast_data.conftest import nav_campaign

    module = _module()
    run = nav_campaign(tmp_path / "nav-2026-01-01-00000000") / "cfg" / "0"

    metrics = module._metrics_for_run(run, "poses", "robot_gt", (2.5, 0.0))

    assert metrics is not None and metrics["collided"] == 1  # it records True twice
    assert metrics["min_clearance"] == ""


def test_a_missing_collision_oracle_is_still_refused(tmp_path):
    """The distinction under test: optional is not the same as absent-and-fine. The
    collision table decides the verdict, so its absence must stay an error."""
    module = _module()
    run = _run_dir(tmp_path, collision=False)

    with pytest.raises(FileNotFoundError):
        module._metrics_for_run(run, "poses", "_gt", (2.5, 0.0))


def test_a_missing_clearance_table_still_leaves_an_empty_cell(tmp_path):
    module = _module()
    run = _run_dir(tmp_path, clearance=False)

    metrics = module._metrics_for_run(run, "poses", "_gt", (2.5, 0.0))

    assert metrics["min_clearance"] == ""
