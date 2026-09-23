# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Write the ``nav_run`` fixture: a small recording and what ROS 2's own libraries make of it.

Run inside a campaign image (rclpy, rosbag2_py, tf2_ros and nav2_msgs present), from the
repository root::

    docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/repo" -w /repo <campaign image> \\
        bash -c 'source /opt/ros/jazzy/setup.bash && \\
                 python3 tests/robovast_decode/fixtures/make_nav_run.py'

It writes ``nav_run/0/rosbag2/`` (the scenario recording) with its definitions sidecar,
``nav_run/0/logs/rosout_bag/`` (the wall-time /rosout + /clock recording), and
``nav_run/expected/*.csv``: the tables ``src/robovast/results_processing/data/rosbags_process.py``
-- rclpy's deserialisation and ``tf2_ros``'s buffer -- produces from exactly these bags. The
pure-Python decoder is held to them. The messages exercise what decides a
table's rows: a TF chain whose dynamic edge arrives after its child (extrapolation refused),
interpolation between two parent samples, a static edge, a quaternion with negative w (tf2's
canonical sign), a repeated stamp, a numeric array with ``inf``, an action with a goal id.
"""

import json
import math
import os
import shutil
import subprocess
import sys

import rosbag2_py
from action_msgs.msg import GoalStatus, GoalStatusArray
from builtin_interfaces.msg import Time
from geometry_msgs.msg import TransformStamped
from nav2_msgs.action import NavigateToPose
from nav2_msgs.msg import BehaviorTreeLog, BehaviorTreeStatusChange
from nav_msgs.msg import OccupancyGrid
from rcl_interfaces.msg import Log
from rclpy.serialization import serialize_message
from rosgraph_msgs.msg import Clock
from sensor_msgs.msg import LaserScan
from std_msgs.msg import Bool
from tf2_msgs.msg import TFMessage

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
CONVERTER = os.path.join(REPO, "src", "robovast", "results_processing", "data", "rosbags_process.py")
DEFINITIONS = os.path.join(REPO, "src", "robovast", "execution", "data", "dump_message_definitions.py")
OUT = os.path.join(HERE, "nav_run")
S = 1_000_000_000


def t(ns):
    return Time(sec=ns // S, nanosec=ns % S)


def tf(parent, child, ns, xyz, q):
    m = TransformStamped()
    m.header.frame_id, m.child_frame_id, m.header.stamp = parent, child, t(ns)
    m.transform.translation.x, m.transform.translation.y, m.transform.translation.z = xyz
    (m.transform.rotation.x, m.transform.rotation.y, m.transform.rotation.z,
     m.transform.rotation.w) = q
    return m


def yaw_q(yaw, flip=False):
    q = (0.0, 0.0, math.sin(yaw / 2), math.cos(yaw / 2))
    return tuple(-c for c in q) if flip else q


def writer(path, topics):
    if os.path.exists(path):
        shutil.rmtree(path)
    w = rosbag2_py.SequentialWriter()
    w.open(rosbag2_py.StorageOptions(uri=path, storage_id="mcap"),
           rosbag2_py.ConverterOptions("cdr", "cdr"))
    for i, (name, typ) in enumerate(topics):
        w.create_topic(rosbag2_py.TopicMetadata(i, name, typ, "cdr"))
    return w


def scenario_bag():
    path = os.path.join(OUT, "0", "rosbag2")
    w = writer(path, [
        ("/tf", "tf2_msgs/msg/TFMessage"), ("/tf_static", "tf2_msgs/msg/TFMessage"),
        ("/behavior_tree_log", "nav2_msgs/msg/BehaviorTreeLog"),
        ("/global_costmap/costmap", "nav_msgs/msg/OccupancyGrid"),
        ("/navigate_to_pose/_action/feedback", "nav2_msgs/action/NavigateToPose_FeedbackMessage"),
        ("/navigate_to_pose/_action/status", "action_msgs/msg/GoalStatusArray"),
        ("/collision", "std_msgs/msg/Bool"), ("/scan", "sensor_msgs/msg/LaserScan"),
    ])
    events = []
    # static base_link -> laser before anything else
    events.append((1 * S, "/tf_static", TFMessage(transforms=[
        tf("base_link", "laser", 1 * S, (0.1, 0.0, 0.2), (0.0, 0.0, 0.0, 1.0))])))
    for k in range(40):                      # 4 s at 10 Hz for map -> odom, 25 Hz odom -> base
        base = 2 * S + k * 100_000_000
        # future-dated, as AMCL's transform tolerance does: a child stamped inside the
        # previous and this sample interpolates; one before the first sample extrapolates
        events.append((base + 5_000_000, "/tf", TFMessage(transforms=[
            tf("map", "odom", base + 100_000_000, (0.5 + 0.01 * k, 0.0, 0.0),
               yaw_q(0.001 * k))])))
        for j in range(4):
            stamp = base + j * 25_000_000
            q = yaw_q(0.05 * k + 0.01 * j, flip=(k % 3 == 0))      # negative w on some
            events.append((stamp + 1_000_000, "/tf", TFMessage(transforms=[
                tf("odom", "base_link", stamp, (0.02 * k, 0.01 * j, 0.0), q)])))
        # ground truth straight from map, with a repeated stamp every so often
        gt_stamp = base + (0 if k % 5 == 0 else 50_000_000)
        events.append((base + 60_000_000, "/tf", TFMessage(transforms=[
            tf("map", "robot_gt", gt_stamp, (0.02 * k, 0.0, 0.0), yaw_q(-0.03 * k, flip=True))])))
        if k % 5 == 0:                       # the same stamp again: tf2 replaces it
            events.append((base + 61_000_000, "/tf", TFMessage(transforms=[
                tf("map", "robot_gt", gt_stamp, (9.0, 9.0, 0.0), yaw_q(1.0))])))
        events.append((base + 70_000_000, "/collision", Bool(data=(k % 7 == 0))))
        scan = LaserScan()
        scan.header.frame_id, scan.header.stamp = "laser", t(base)
        scan.angle_min, scan.angle_max, scan.range_max = -1.0, 1.0, 10.0
        scan.ranges = [1.0 + 0.1 * i if i % 4 else float("inf") for i in range(16)]
        events.append((base + 80_000_000, "/scan", scan))
        if k % 4 == 0:
            fb = NavigateToPose.Impl.FeedbackMessage()
            fb.goal_id.uuid = list(range(16))
            fb.feedback.current_pose.header.frame_id = "map"
            fb.feedback.current_pose.pose.position.x = 0.02 * k
            fb.feedback.distance_remaining = 5.0 - 0.1 * k
            fb.feedback.navigation_time.sec = k // 10
            fb.feedback.number_of_recoveries = k // 20
            events.append((base + 90_000_000, "/navigate_to_pose/_action/feedback", fb))
        if k % 8 == 0:
            log = BehaviorTreeLog()
            log.timestamp = t(1_700_000_000 * S + k)
            log.event_log = [BehaviorTreeStatusChange(
                timestamp=t(1_700_000_000 * S + k), node_name=f"node_{k % 3}", uid=k,
                previous_status="IDLE", current_status="RUNNING")]
            events.append((base + 95_000_000, "/behavior_tree_log", log))
    grid = OccupancyGrid()
    grid.header.frame_id = "map"
    grid.info.resolution, grid.info.width, grid.info.height = 0.05, 4, 3
    grid.info.origin.orientation.w = 1.0
    grid.data = [-1, 0, 100, 50, 0, 0, 0, 1, 2, 3, 99, -1]
    events.append((3 * S, "/global_costmap/costmap", grid))
    status = GoalStatusArray(status_list=[GoalStatus(status=2), GoalStatus(status=4)])
    status.status_list[0].goal_info.goal_id.uuid = list(range(16))
    events.append((5 * S, "/navigate_to_pose/_action/status", status))
    for stamp, topic, msg in sorted(events, key=lambda e: e[0]):
        w.write(topic, serialize_message(msg), stamp)
    del w


def infra_bag():
    path = os.path.join(OUT, "0", "logs", "rosout_bag")
    w = writer(path, [("/rosout", "rcl_interfaces/msg/Log"), ("/clock", "rosgraph_msgs/msg/Clock")])
    wall0 = 1_780_000_000 * S
    for k in range(300):                                  # sim runs at 1.25x, pauses at 150
        sim = int((min(k, 150) + max(0, k - 170) * 1.0) * 0.0125 * S)
        w.write("/clock", serialize_message(Clock(clock=t(sim))), wall0 + k * 10_000_000)
        if k % 50 == 0:
            log = Log(level=20 if k % 100 else 30, name="bt_navigator", msg=f"tick {k}",
                      file="x.cpp", function="f", line=k)
            log.stamp = t(wall0 + k * 10_000_000)
            w.write("/rosout", serialize_message(log), wall0 + k * 10_000_000 + 1000)
    del w


def expected():
    exp = os.path.join(OUT, "expected")
    shutil.rmtree(exp, ignore_errors=True)
    work = os.path.join(HERE, "_convert")
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(OUT, os.path.join(work, "cfg"), ignore=shutil.ignore_patterns("expected"))
    config = {"groups": [
        {"bag_dir": "rosbag2", "plugins": [
            {"type": "tf_to_csv", "frames": "all", "require": ["base_link", "robot_gt"]},
            {"type": "nav2_bt_to_csv"},
            {"type": "costmap_to_csv", "topics": ["/global_costmap/costmap"]},
            {"type": "to_csv", "topics": ["/collision", "/scan"]},
            {"type": "action_to_csv", "action": "navigate_to_pose"}]},
        {"bag_dir": "logs/rosout_bag", "plugins": [
            {"type": "rosout_to_csv"}, {"type": "clock_to_csv"}]}]}
    subprocess.run([sys.executable, CONVERTER, work,
                    "--config", json.dumps(config), "--workers", "1", "--force"], check=True)
    os.makedirs(exp)
    for root, _, files in os.walk(os.path.join(work, "cfg", "0")):
        for name in files:
            if name.endswith(".csv"):
                sub = os.path.relpath(root, os.path.join(work, "cfg", "0")).replace("/", "__")
                prefix = "" if sub == "." else sub + "__"
                shutil.copy(os.path.join(root, name), os.path.join(exp, prefix + name))
    shutil.rmtree(work)


if __name__ == "__main__":
    scenario_bag()
    infra_bag()
    subprocess.run([sys.executable, DEFINITIONS, os.path.join(OUT, "0", "rosbag2")], check=True)
    expected()
    print("wrote", OUT)
