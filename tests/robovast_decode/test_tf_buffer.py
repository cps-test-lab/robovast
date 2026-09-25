# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The transform buffer answers, and refuses, exactly where tf2 does.

Which lookups fail decides which rows a pose table has, so the refusals are tested as
carefully as the arithmetic.
"""

import math

import pytest

from robovast_decode.tf import (ConnectivityError, ExtrapolationError, TransformBuffer,
                                TransformError)

S = 1_000_000_000
IDENTITY = (0.0, 0.0, 0.0, 1.0)


def yaw(angle):
    return (0.0, 0.0, math.sin(angle / 2), math.cos(angle / 2))


@pytest.fixture
def tree():
    b = TransformBuffer()
    b.set_transform("odom", "map", 1 * S, (1.0, 0.0, 0.0), IDENTITY)
    b.set_transform("odom", "map", 3 * S, (3.0, 0.0, 0.0), IDENTITY)
    b.set_transform("base_link", "odom", 2 * S, (0.0, 1.0, 0.0), yaw(math.pi / 2))
    b.set_transform("laser", "base_link", 0, (0.1, 0.0, 0.0), IDENTITY, static=True)
    return b


def test_a_parent_is_interpolated_to_the_childs_stamp(tree):
    (x, y, z), q = tree.lookup("map", "base_link", 2 * S)
    assert (x, y, z) == pytest.approx((2.0, 1.0, 0.0))
    assert q == pytest.approx(yaw(math.pi / 2))


def test_a_static_edge_is_valid_at_every_time(tree):
    (x, y, _), _ = tree.lookup("map", "laser", 2 * S)
    assert (x, y) == pytest.approx((2.0, 1.1))


def test_a_time_past_the_newest_parent_sample_is_refused(tree):
    tree.set_transform("base_link", "odom", 4 * S, (0.0, 1.0, 0.0), IDENTITY)
    with pytest.raises(ExtrapolationError):
        tree.lookup("map", "base_link", 4 * S)


def test_a_single_sample_answers_only_its_own_stamp():
    b = TransformBuffer()
    b.set_transform("a", "map", 5 * S, (1.0, 0.0, 0.0), IDENTITY)
    assert b.lookup("map", "a", 5 * S)[0] == (1.0, 0.0, 0.0)
    with pytest.raises(ExtrapolationError):
        b.lookup("map", "a", 6 * S)


def test_a_repeated_stamp_replaces_the_stored_value():
    b = TransformBuffer()
    b.set_transform("a", "map", 5 * S, (1.0, 0.0, 0.0), IDENTITY)
    assert b.set_transform("a", "map", 5 * S, (9.0, 9.0, 0.0), IDENTITY)
    assert b.lookup("map", "a", 5 * S)[0] == (9.0, 9.0, 0.0)


def test_data_older_than_the_cache_is_dropped():
    b = TransformBuffer()
    b.set_transform("a", "map", 1 * S, (1.0, 0.0, 0.0), IDENTITY)
    b.set_transform("a", "map", 20 * S, (2.0, 0.0, 0.0), IDENTITY)
    with pytest.raises(ExtrapolationError):
        b.lookup("map", "a", 5 * S)                      # pruned: 10 s behind the newest
    assert not b.set_transform("a", "map", 2 * S, (1.0, 0.0, 0.0), IDENTITY)


def test_time_zero_is_the_latest_time_the_chain_has_in_common(tree):
    tree.set_transform("base_link", "odom", 2_500_000_000, (0.0, 2.0, 0.0), yaw(math.pi / 2))
    (x, y, _), _ = tree.lookup("map", "base_link", 0)
    assert (x, y) == pytest.approx((2.5, 2.0))


def test_frames_in_two_trees_do_not_connect(tree):
    tree.set_transform("island", "elsewhere", 2 * S, (0.0, 0.0, 0.0), IDENTITY)
    with pytest.raises(ConnectivityError):
        tree.lookup("map", "island", 2 * S)


def test_an_unknown_frame_is_refused(tree):
    with pytest.raises(TransformError):
        tree.lookup("map", "nowhere", 2 * S)


def test_the_answer_is_tf2s_canonical_quaternion():
    """tf2 returns a lookup through a rotation matrix, so ``w`` comes back non-negative for a
    rotation this small, even when the stored quaternion was its negative twin."""
    b = TransformBuffer()
    b.set_transform("gt", "map", 1 * S, (0.0, 0.0, 0.0), tuple(-c for c in yaw(0.2)))
    _, q = b.lookup("map", "gt", 1 * S)
    assert q[3] > 0
    assert q == pytest.approx(yaw(0.2))


def test_a_denormalised_quaternion_is_refused():
    b = TransformBuffer()
    assert not b.set_transform("a", "map", 1 * S, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 2.0))
