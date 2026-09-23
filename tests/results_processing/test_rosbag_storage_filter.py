# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The converter reads only the topics some handler declares.

``process_rosbag_worker`` filters the bag reader to the union of the handlers' ``topics()``, so a
recorded topic that no handler asked for -- a laser scan, a camera stream -- is skipped by the
storage plugin and never reaches Python. Two properties are pinned here:

* an undeclared topic is not read at all;
* the output for the declared topics is byte-identical to an unfiltered read, including for the
  handlers that read more than one topic (``tf_to_csv`` needs ``/tf_static`` beside ``/tf``: a
  static ``map -> odom`` it did not receive would leave ``base_link`` unresolvable).

Needs a ROS environment (``rosbag2_py``): skipped elsewhere, like the converter itself.
"""

import os
import sys

import pytest

rosbag2_py = pytest.importorskip("rosbag2_py")
pytest.importorskip("rclpy")
pytest.importorskip("tf2_msgs")

# pylint: disable=wrong-import-position
from geometry_msgs.msg import TransformStamped  # noqa: E402
from rclpy.serialization import serialize_message  # noqa: E402
from sensor_msgs.msg import LaserScan  # noqa: E402
from std_msgs.msg import String  # noqa: E402
from tf2_msgs.msg import TFMessage  # noqa: E402
# pylint: enable=wrong-import-position

DATA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "src", "robovast", "results_processing", "data"
)

_TOPICS = {
    "/chatter": "std_msgs/msg/String",
    "/tf": "tf2_msgs/msg/TFMessage",
    "/tf_static": "tf2_msgs/msg/TFMessage",
    "/scan": "sensor_msgs/msg/LaserScan",
}


def _import_converter():
    """The converter is a script beside its helpers, importable only from its own directory."""
    if DATA_DIR not in sys.path:
        sys.path.insert(0, DATA_DIR)
    import rosbags_process  # noqa: PLC0415  pylint: disable=import-outside-toplevel

    return rosbags_process


def _tf(parent, child, sec, x):
    t = TransformStamped()
    t.header.frame_id = parent
    t.header.stamp.sec = sec
    t.child_frame_id = child
    t.transform.translation.x = float(x)
    t.transform.rotation.w = 1.0
    return TFMessage(transforms=[t])


def _write_bag(path, topics=None):
    """A static ``map -> odom``, a moving ``odom -> base_link``, a String and a bulky scan."""
    topics = _TOPICS if topics is None else topics
    writer = rosbag2_py.SequentialWriter()
    writer.open(
        rosbag2_py.StorageOptions(uri=str(path), storage_id="mcap"),
        rosbag2_py.ConverterOptions(input_serialization_format="cdr",
                                    output_serialization_format="cdr"),
    )
    for i, (name, type_) in enumerate(topics.items()):
        writer.create_topic(rosbag2_py.TopicMetadata(
            id=i, name=name, type=type_, serialization_format="cdr"))
    if "/tf_static" in topics:
        writer.write("/tf_static", serialize_message(_tf("map", "odom", 0, 10)), 1)
    for i in range(1, 4):
        stamp = 1_000_000_000 * i
        if "/tf" in topics:
            writer.write("/tf", serialize_message(_tf("odom", "base_link", i, i)), stamp)
        if "/chatter" in topics:
            writer.write("/chatter", serialize_message(String(data=f"hi {i}")), stamp)
        if "/scan" in topics:
            writer.write("/scan", serialize_message(LaserScan(ranges=[1.0] * 360)), stamp)
    del writer


class _RecordingReader:
    """Wraps the real reader and records every topic it hands out.

    ``honour_filter=False`` drops the filter, which is the unfiltered read the output is compared
    against.
    """

    read = []

    def __init__(self, honour_filter=True):
        self._reader = _REAL_READER()
        self._honour_filter = honour_filter

    def set_filter(self, storage_filter):
        if self._honour_filter:
            self._reader.set_filter(storage_filter)

    def read_next(self):
        record = self._reader.read_next()
        type(self).read.append(record[0])
        return record

    def __getattr__(self, name):
        return getattr(self._reader, name)


_REAL_READER = rosbag2_py.SequentialReader


def _convert(monkeypatch, bag, out_dir, cfg, honour_filter=True):
    rp = _import_converter()
    _RecordingReader.read = []
    monkeypatch.setattr(rp.rosbag2_py, "SequentialReader",
                        lambda: _RecordingReader(honour_filter))
    os.makedirs(out_dir, exist_ok=True)
    result = rp.process_rosbag_worker((str(bag), cfg, False, True, "h", str(out_dir)))
    return result, list(_RecordingReader.read)


def _outputs(out_dir):
    return {name: (out_dir / name).read_bytes() for name in sorted(os.listdir(out_dir))}


_CFG = [
    {"type": "to_csv", "topics": ["/chatter"]},
    {"type": "tf_to_csv", "frames": ["base_link"]},
]


def test_an_undeclared_topic_is_not_read(tmp_path, monkeypatch):
    bag = tmp_path / "run" / "rosbag2"
    bag.parent.mkdir()
    _write_bag(bag)
    result, read = _convert(monkeypatch, bag, tmp_path / "out", _CFG)
    assert result.total > 0
    assert "/scan" not in read
    assert set(read) == {"/chatter", "/tf", "/tf_static"}


def test_declared_topics_convert_exactly_as_an_unfiltered_read(tmp_path, monkeypatch):
    """Same files, same bytes -- tf_to_csv's map-relative poses included, which need /tf_static."""
    bag = tmp_path / "run" / "rosbag2"
    bag.parent.mkdir()
    _write_bag(bag)
    filtered, _ = _convert(monkeypatch, bag, tmp_path / "filtered", _CFG)
    unfiltered, read_all = _convert(monkeypatch, bag, tmp_path / "unfiltered", _CFG,
                                    honour_filter=False)
    assert "/scan" in read_all  # the comparison is against a read that did see it
    assert filtered.total == unfiltered.total == 3 + 3
    out = _outputs(tmp_path / "filtered")
    assert out == _outputs(tmp_path / "unfiltered")
    assert out["poses.csv"].count(b"\n") == 1 + 3  # header + one pose per /tf message


def test_a_bag_with_none_of_the_declared_topics_is_not_read(tmp_path, monkeypatch):
    """rosbag2 reads an empty filter as "every topic", so the worker must not read at all."""
    bag = tmp_path / "run" / "rosbag2"
    bag.parent.mkdir()
    _write_bag(bag, topics={"/scan": _TOPICS["/scan"]})
    _, read = _convert(monkeypatch, bag, tmp_path / "out",
                       [{"type": "to_csv", "topics": ["/chatter"]}])
    assert not read


def test_every_handler_declares_the_topics_it_reads():
    """The topics each handler compares against in ``on_message``, stated once per handler.

    A handler whose ``topics()`` omits one of these would silently lose that topic's messages to
    the filter, so a change to either side has to change this table too.
    """
    rp = _import_converter()
    expected = {
        "to_csv": ({"topics": ["/a", "/b"]}, {"/a", "/b"}),
        "tf_to_csv": ({}, {"/tf", "/tf_static"}),
        "nav2_bt_to_csv": ({}, {"/behavior_tree_log"}),
        "action_to_csv": ({"action": "navigate_to_pose"},
                          {"/navigate_to_pose/_action/feedback",
                           "/navigate_to_pose/_action/status"}),
        "rosout_to_csv": ({}, {"/rosout"}),
        "clock_to_csv": ({}, {"/clock"}),
        "costmap_to_csv": ({"topics": ["/global_costmap/costmap"]}, {"/global_costmap/costmap"}),
        "to_webm": ({"topic": "/camera/image/compressed"}, {"/camera/image/compressed"}),
    }
    assert set(expected) == set(rp.HANDLER_REGISTRY), "a new handler needs a row here"
    for name, (cfg, topics) in expected.items():
        handler = rp.HANDLER_REGISTRY[name].from_config({"type": name, **cfg})
        assert set(handler.topics()) == topics, name
