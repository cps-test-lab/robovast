# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A topic's columns follow its message definition, never what a run recorded."""

import numpy as np
import pyarrow as pa
import pytest
from rosbags.typesys import Stores, get_typestore
from rosbags.typesys.msg import Nodetype

from robovast_decode.handlers import TopicTable
from robovast_decode.values import column_values, table_columns

STORE = get_typestore(Stores.ROS2_JAZZY)


def fields_of(typename):
    return STORE.fielddefs[typename][1]


def _types(typename):
    return dict(table_columns(fields_of, typename))


def _msg(typename, **fields):
    return STORE.types[typename](**fields)


def _header(frame_id="map"):
    return _msg("std_msgs/msg/Header", stamp=_msg("builtin_interfaces/msg/Time", sec=1, nanosec=2),
                frame_id=frame_id)


def _pose_stamped(x, y):
    return _msg("geometry_msgs/msg/PoseStamped", header=_header(), pose=_msg(
        "geometry_msgs/msg/Pose",
        position=_msg("geometry_msgs/msg/Point", x=x, y=y, z=0.0),
        orientation=_msg("geometry_msgs/msg/Quaternion", x=0.0, y=0.0, z=0.0, w=1.0)))


def _path(*points):
    return _msg("nav_msgs/msg/Path", header=_header(), poses=[_pose_stamped(x, y) for x, y in points])


def test_a_scalar_field_is_one_column_typed_by_its_base_type():
    types = _types("geometry_msgs/msg/Point")
    assert types == {"x": pa.float64(), "y": pa.float64(), "z": pa.float64()}


def test_a_numeric_array_is_one_list_column():
    types = _types("sensor_msgs/msg/LaserScan")
    assert types["ranges"] == pa.list_(pa.float32())
    assert types["intensities"] == pa.list_(pa.float32())
    assert types["header.frame_id"] == pa.string()
    scan = _msg("sensor_msgs/msg/LaserScan", header=_header("laser"), angle_min=-1.0, angle_max=1.0,
                angle_increment=0.1, time_increment=0.0, scan_time=0.0, range_min=0.0,
                range_max=10.0, ranges=np.array([1.0, np.inf, 2.5], dtype=np.float32),
                intensities=np.array([], dtype=np.float32))
    values = dict(column_values(fields_of, scan, "sensor_msgs/msg/LaserScan"))
    assert values["ranges"].tolist() == [1.0, np.inf, 2.5]
    assert values["intensities"].tolist() == []


def test_a_byte_array_is_one_binary_column():
    types = _types("sensor_msgs/msg/PointCloud2")
    assert types["data"] == pa.binary()
    assert types["fields.name"] == pa.list_(pa.string())      # a sequence of sub-messages
    assert types["fields.offset"] == pa.list_(pa.uint32())


def test_a_sequence_of_messages_is_one_list_column_per_leaf_field():
    types = _types("nav_msgs/msg/Path")
    assert types["poses.pose.position.x"] == pa.list_(pa.float64())
    assert types["poses.header.frame_id"] == pa.list_(pa.string())
    assert types["poses.header.stamp.sec"] == pa.list_(pa.int32())
    assert "poses[0].pose.position.x" not in types
    values = dict(column_values(fields_of, _path((1.0, 2.0), (3.0, 4.0)), "nav_msgs/msg/Path"))
    assert values["poses.pose.position.x"] == [1.0, 3.0]
    assert values["poses.pose.position.y"] == [2.0, 4.0]
    assert values["poses.header.frame_id"] == ["map", "map"]
    assert values["header.frame_id"] == "map"


def test_an_empty_sequence_still_has_its_columns():
    values = dict(column_values(fields_of, _path(), "nav_msgs/msg/Path"))
    assert values["poses.pose.position.x"] == []
    assert set(values) == set(_types("nav_msgs/msg/Path"))


def test_a_sequence_inside_a_sequence_nests_the_lists():
    types = _types("visualization_msgs/msg/MarkerArray")
    assert types["markers.points.x"] == pa.list_(pa.list_(pa.float64()))
    assert types["markers.pose.position.x"] == pa.list_(pa.float64())
    assert types["markers.text"] == pa.list_(pa.string())


def test_a_topic_table_has_one_schema_however_much_each_run_recorded():
    tables = []
    for points in (((1.0, 2.0),), ((1.0, 2.0), (3.0, 4.0), (5.0, 6.0)), ()):
        handler = TopicTable(["/plan"])
        handler.fields_of = fields_of
        handler.message("/plan", _path(*points), "nav_msgs/msg/Path", log_time=10 ** 9)
        tables.append(handler.flush()["rosbag2_plan"])
    assert tables[0].schema == tables[1].schema == tables[2].schema
    assert tables[0].column_names[:2] == ["timestamp", "type"]
    assert tables[0].schema.field("poses.pose.position.x").type == pa.list_(pa.float64())
    assert tables[1].column("poses.pose.position.x").to_pylist() == [[1.0, 3.0, 5.0]]
    assert tables[2].column("poses.pose.position.x").to_pylist() == [[]]
    assert tables[0].column("type").to_pylist() == ["Path"]


def test_a_base_type_without_a_column_type_is_refused_by_name():
    def odd_fields(typename):
        return [("thing", (Nodetype.BASE, ("quaternion128", 0)))]
    with pytest.raises(ValueError, match="thing.*quaternion128"):
        table_columns(odd_fields, "made/up")
