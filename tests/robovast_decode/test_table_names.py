# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Two data files never share one table by accident.

A bag-derived name is the recording's directory plus the topic, so two long names agree in
their leading characters; a bound that cut them would merge two topics into one table. Two
filenames can also sanitise to one table (``run.clock_map.csv`` and ``run_clock_map.csv``),
which a guard keyed on the filename would let through.
"""

from robovast_decode.authored import MAX_TABLE_NAME_BYTES, run_files, table_name

BAG = "rosbag2_2026_09_07-11_22_33"
LONG_A = f"{BAG}_local_costmap_costmap_updates_footprint_alpha.csv"
LONG_B = f"{BAG}_local_costmap_costmap_updates_footprint_beta.csv"


def test_a_short_name_is_unchanged():
    assert table_name("behaviors.csv") == "behaviors"
    assert table_name("action-nav.csv") == "action_nav"
    assert table_name("1_metric.csv") == "t_1_metric"


def test_a_name_that_does_not_fit_is_shortened_to_something_that_does():
    for name in (LONG_A, LONG_B):
        assert len(name) > MAX_TABLE_NAME_BYTES + 1, "the fixture must exceed the bound"
        assert len(table_name(name).encode()) <= MAX_TABLE_NAME_BYTES


def test_two_over_long_names_that_agree_up_to_the_cut_stay_apart():
    cut = MAX_TABLE_NAME_BYTES
    assert LONG_A[:cut] == LONG_B[:cut], "the fixture must be two names a cut would merge"
    assert table_name(LONG_A) != table_name(LONG_B)


def test_the_shortened_name_is_stable():
    """It names a cached table, so it cannot move between builds."""
    assert table_name(LONG_A) == table_name(LONG_A)
    assert table_name(LONG_A) == table_name(LONG_A.replace(".csv", ".jsonl"))


def test_the_head_of_the_shortened_name_still_reads():
    assert table_name(LONG_A).startswith("rosbag2_2026_09_07_11_22_33_local_costmap")


def _run(tmp_path, names):
    run = tmp_path / "cfg" / "0"
    run.mkdir(parents=True)
    for name in names:
        (run / name).write_text("v\n1\n")
    return str(run)


def test_two_over_long_files_are_two_tables(tmp_path):
    found = run_files(_run(tmp_path, [LONG_A, LONG_B]))
    assert set(found.tables) == {table_name(LONG_A), table_name(LONG_B)}
    assert not found.refused


def test_two_filenames_that_sanitise_to_one_table_are_refused_with_both_named(tmp_path):
    found = run_files(_run(tmp_path, ["run.clock_map.csv", "run_clock_map.csv"]))
    assert "run_clock_map" not in found.tables
    reason = found.refused["run_clock_map"]
    assert "run.clock_map.csv" in reason and "run_clock_map.csv" in reason


def test_a_file_claiming_a_built_table_is_refused(tmp_path):
    found = run_files(_run(tmp_path, ["poses.csv"]), reserved={"poses"})
    assert "poses" not in found.tables and "rename" in found.refused["poses"]
