# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Which pose-contract column notes a table earns, decided from its columns alone.

The contract has two clock shapes and the advice inverts between them, so annotating a table
means choosing -- and the failure mode of choosing wrong is silence, not an error: a table that
matches neither shape is simply left undocumented. These assert the choice without a database.
"""

from robovast.results_processing.campaign_ingest import pose_notes_for

#: The columns `tf_to_csv` writes: an arrival clock plus the producer's own measurement stamp.
TRANSPORT_COLUMNS = {
    "frame", "timestamp", "stamp",
    "position.x", "position.y", "position.z",
    "orientation.x", "orientation.y", "orientation.z", "orientation.w", "orientation.yaw",
    "twist.linear.x", "twist.angular.z",
}

#: The columns the simulator writes: one exact clock, a wall-clock bridge, and no `stamp`.
NATIVE_COLUMNS = (TRANSPORT_COLUMNS - {"stamp"}) | {"wall_time"}


def test_transport_table_is_told_which_clock_to_difference():
    notes = pose_notes_for(TRANSPORT_COLUMNS)
    assert "Do NOT difference it" in notes["timestamp"]
    assert "MEASUREMENT time" in notes["stamp"]


def test_simulator_table_is_annotated_rather_than_skipped():
    """It has no ``stamp``, which is exactly what used to leave it with no notes at all."""
    notes = pose_notes_for(NATIVE_COLUMNS)
    assert set(notes) == {"timestamp", "wall_time", "orientation.yaw"}


def test_simulator_table_is_never_pointed_at_a_stamp_it_does_not_have():
    """Its ``timestamp`` is the measurement clock, so the transport advice would invert the truth."""
    note = pose_notes_for(NATIVE_COLUMNS)["timestamp"]
    assert "Difference it freely" in note
    assert "Use `stamp` for that" not in note


def test_wall_time_is_marked_as_a_join_bridge_and_not_a_pose_clock():
    assert "Do NOT difference it" in pose_notes_for(NATIVE_COLUMNS)["wall_time"]


def test_both_clock_shapes_get_the_yaw_projection_warning():
    for columns in (TRANSPORT_COLUMNS, NATIVE_COLUMNS):
        assert "planar projection" in pose_notes_for(columns)["orientation.yaw"]


def test_a_table_with_a_stamp_and_no_position_is_not_a_pose_table():
    """rosout carries a ``stamp`` too, and must not collect notes that talk about poses."""
    assert pose_notes_for({"timestamp", "stamp", "level", "msg"}) == {}


def test_absent_columns_are_never_documented():
    """A note on a column the table does not hold documents a column nobody can select."""
    assert set(pose_notes_for({"timestamp", "position.x"})) == {"timestamp"}
