# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Every table is what ROS 2's own libraries make of the same recording, cell for cell.

The expected tables in ``fixtures/nav_run/expected`` were produced in a campaign image by the
converter that runs rclpy's deserialisation and ``tf2_ros``'s buffer (see
``fixtures/make_nav_run.py``). The recording is built to exercise what decides a table's rows,
not only its values: a child transform that arrives before the parent sample that would place
it (refused, as tf2 refuses it), interpolation between two parent samples, a static edge, a
repeated stamp, a quaternion with negative ``w``, a laser range of ``inf``, an action goal id.

A pure-Python table that differed would be a different measurement, which is why this
compares every cell rather than a summary.
"""

import csv
import math

import pyarrow.parquet as pq
import pytest

from robovast_decode.build import build
from robovast_decode.values import decode_numeric_array, is_numeric_array_cell

from .conftest import FIXTURE, NAV_CONFIG

EXPECTED = {
    "poses": "poses.csv",
    "nav2_behavior_tree": "nav2_behavior_tree.csv",
    "costmaps": "costmaps.csv",
    "rosbag2_collision": "rosbag2_collision.csv",
    "rosbag2_scan": "rosbag2_scan.csv",
    "action_navigate_to_pose_feedback": "action_navigate_to_pose_feedback.csv",
    "action_navigate_to_pose_status": "action_navigate_to_pose_status.csv",
    "rosout": "logs__rosout.csv",
    "clock_map": "logs__clock_map.csv",
}

CONTEXT = ("campaign_id", "config_name", "run_id")


def _norm(value):
    """A cell as a comparable value: a CSV writes None as "" and every number as text."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except ValueError:
        return value


def _same(expected, actual) -> bool:
    if is_numeric_array_cell(expected):
        a, b = decode_numeric_array(expected), decode_numeric_array(actual)
        return a.dtype == b.dtype and a.tobytes() == b.tobytes()
    x, y = _norm(expected), _norm(actual)
    if isinstance(x, float) and isinstance(y, float):
        if math.isnan(x) or math.isnan(y):
            return math.isnan(x) and math.isnan(y)
        # The float's text form round-trips exactly; allow the last bit of the arithmetic.
        return abs(x - y) <= 1e-12 * max(1.0, abs(x))
    return x == y


@pytest.mark.parametrize("table, csv_name", sorted(EXPECTED.items()))
def test_table_matches_the_ros_converter(campaign, table, csv_name):
    report = build(str(campaign), tables=[table], config=NAV_CONFIG)
    assert not report.failed and not report.unknown
    got = pq.read_table(campaign / ".cache" / "tables" / table / "cfg" / "0.parquet")
    with open(FIXTURE / "expected" / csv_name, encoding="utf-8") as fh:
        expected = list(csv.DictReader(fh))

    columns = list(expected[0])
    # A table with a quaternion also gets its heading, derived as the index always derived it.
    derived = ["orientation.yaw"] if "orientation.w" in columns else []
    assert got.column_names[:3] == list(CONTEXT)
    assert got.column_names[3:] == columns + derived, "column names and order"
    rows = got.to_pylist()
    assert len(rows) == len(expected), "row count"
    for i, (want, have) in enumerate(zip(expected, rows)):
        for column, value in want.items():
            assert _same(value, have[column]), f"row {i} {column}: {value!r} != {have[column]!r}"
        if derived:
            x, y, z, w = (float(want[f"orientation.{c}"]) for c in "xyzw")
            assert have["orientation.yaw"] == pytest.approx(
                math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z)))
    assert {r["campaign_id"] for r in rows} <= {campaign.name}
