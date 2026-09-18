# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A recorded topic whose message package is missing fails the bag, loudly.

A campaign that records a vendor's message -- a wheel-velocity or hazard topic from a robot's
own stack -- and converts it in an execution image without that vendor's package would
otherwise come back green and short a table: the topic was silently skipped, and nothing
downstream can tell a topic that was never recorded from one that could not be decoded.
The bag fails, the failure names the topic, the type and the fix, and every other topic
of a bag whose types all resolve converts as before.

Needs a ROS environment (``rosbag2_py``): skipped elsewhere, like the converter itself.
"""

import os
import sys

import pytest

rosbag2_py = pytest.importorskip("rosbag2_py")
pytest.importorskip("rclpy")

from rclpy.serialization import serialize_message  # noqa: E402  pylint: disable=wrong-import-position
from std_msgs.msg import String  # noqa: E402  pylint: disable=wrong-import-position

DATA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "src", "robovast", "results_processing", "data"
)


def _import_converter():
    """The converter is a script beside its helpers, importable only from its own directory."""
    if DATA_DIR not in sys.path:
        sys.path.insert(0, DATA_DIR)
    import rosbags_process  # noqa: PLC0415

    return rosbags_process


def _write_bag(path, with_ghost):
    """One String topic, and optionally one whose type no installed package defines."""
    writer = rosbag2_py.SequentialWriter()
    writer.open(
        rosbag2_py.StorageOptions(uri=str(path), storage_id="mcap"),
        rosbag2_py.ConverterOptions(input_serialization_format="cdr",
                                    output_serialization_format="cdr"),
    )
    writer.create_topic(rosbag2_py.TopicMetadata(
        id=0, name="/chatter", type="std_msgs/msg/String", serialization_format="cdr"))
    if with_ghost:
        writer.create_topic(rosbag2_py.TopicMetadata(
            id=1, name="/ghost", type="vendor_msgs/msg/Ghost", serialization_format="cdr"))
    for i in range(3):
        writer.write("/chatter", serialize_message(String(data=f"hi {i}")), 1_000_000 * i)
        if with_ghost:
            writer.write("/ghost", b"\x00\x01\x00\x00", 1_000_000 * i)
    del writer


def _convert(bag, topics):
    rp = _import_converter()
    cfg = [{"type": "to_csv", "topics": topics}]
    return rp.process_rosbag_worker((str(bag), cfg, False, True, "h", str(bag.parent)))


def test_an_unloadable_type_fails_the_bag_and_names_the_fix(tmp_path):
    bag = tmp_path / "run" / "rosbag2"
    bag.parent.mkdir()
    _write_bag(bag, with_ghost=True)
    result = _convert(bag, ["/chatter", "/ghost"])
    rp = _import_converter()
    assert result.total == rp.FAILED
    assert "/ghost (vendor_msgs/msg/Ghost)" in result.output
    assert "system_packages" in result.output


def test_a_bag_whose_asked_for_types_all_resolve_converts(tmp_path):
    """The ghost topic recorded but not asked for is nobody's business."""
    bag = tmp_path / "run" / "rosbag2"
    bag.parent.mkdir()
    _write_bag(bag, with_ghost=True)
    result = _convert(bag, ["/chatter"])
    assert result.total == 3
    assert (bag.parent / "rosbag2_chatter.csv").is_file()
