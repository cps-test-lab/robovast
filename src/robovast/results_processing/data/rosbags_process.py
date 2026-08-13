#!/usr/bin/env python3
# Copyright (C) 2026 Frederik Pasch
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

"""Unified rosbag processing script with internal plugin system.

Reads each rosbag exactly once and dispatches messages to multiple handler
plugins in a single pass. This is significantly faster than running separate
rosbags_*.py scripts, each of which reads all rosbags from scratch.

Handler types (specified via --config JSON):
  to_csv          Extract arbitrary ROS topics to CSV
  tf_to_csv       Extract TF transforms to CSV
  nav2_bt_to_csv  Extract nav2 /behavior_tree_log status transitions to CSV
  action_to_csv   Extract ROS2 action feedback/status to CSV
  rosout_to_csv   Extract /rosout log messages to CSV
  clock_to_csv    Extract the wall<->sim mapping from /clock (wall-time bags only)

Usage::

    rosbags_process.py INPUT_DIR \\
        --config '{"plugins": [{"type": "rosout_to_csv"}, {"type": "tf_to_csv"}]}' \\
        --workers 4 \\
        --provenance-file /provenance/process_provenance.json
"""

import argparse
import base64
import contextlib
import csv
import hashlib
import io
import json
import math
import zlib
import os
import re
import subprocess
import sys
import tempfile
import time
import yaml
from abc import ABC, abstractmethod
from multiprocessing import Pool, cpu_count
from typing import Any, Dict, List, Optional, Tuple

import rosbag2_py
from tf2_ros import Buffer
from rclpy.serialization import deserialize_message
from tf2_py import ConnectivityException, ExtrapolationException, LookupException
import numpy as np

from rosbags_common import (CLOCK_MAP_FIELDNAMES, CLOCK_MAP_FILENAME,
                            DEFAULT_CLOCK_TOLERANCE_S, ClockDecimator,
                            find_rosbags, gen_msg_values, is_under_tolerated_root,
                            register_video, resolve_tolerated_roots,
                            write_provenance_entry)
from rosidl_runtime_py.utilities import get_message


# ---------------------------------------------------------------------------
# Base handler class
# ---------------------------------------------------------------------------

class RosbagHandler(ABC):
    """Base class for single-pass rosbag message handlers.

    Each handler is responsible for processing messages from a specific set of
    topics. Handlers are instantiated fresh for each rosbag inside the worker
    subprocess (to avoid pickling issues with TF buffers, open file handles, etc.).
    """

    #: Where this bag's outputs go. ``None`` (the default) means "beside the bag" —
    #: what a local/bind-mounted run wants, since the container writes straight into
    #: the real campaign dir. The worker sets it when ``--output-root`` is given, so
    #: outputs land in a separate tree (a pod cannot write into the caller's
    #: filesystem, so the cluster Job keeps inputs and outputs in different dirs).
    output_dir: Optional[str] = None

    def _out_dir(self, bag_path: str) -> str:
        """Absolute output directory for *bag_path* (created if needed)."""
        out = self.output_dir or os.path.dirname(bag_path)
        out = os.path.abspath(out)
        os.makedirs(out, exist_ok=True)
        return out

    @abstractmethod
    def topics(self) -> List[str]:
        """Return list of topic names this handler wants to receive."""

    def on_begin(self, bag_path: str, topic_type_map: Dict[str, str]) -> None:
        """Called once before reading begins.

        Open output files, initialize state, check that required topics are
        present. Raise an exception to abort this handler for this bag
        (other handlers continue normally).

        Args:
            bag_path: Absolute path to the rosbag directory.
            topic_type_map: Dict mapping topic name → ROS type string for all
                topics present in this bag.
        """

    @abstractmethod
    def on_message(self, topic: str, msg: Any, timestamp: int) -> None:
        """Called for each relevant message (already deserialized).

        Args:
            topic: Topic name.
            msg: Deserialized ROS message object.
            timestamp: Bag receive timestamp in nanoseconds.
        """

    @abstractmethod
    def on_end(self) -> Tuple[int, List[str]]:
        """Called after all messages. Flush and close files.

        Returns:
            Tuple of (record_count, output_file_paths).
            record_count == -2 signals an unrecoverable error.
        """

    @classmethod
    @abstractmethod
    def from_config(cls, config: dict) -> "RosbagHandler":
        """Construct handler from a config dict (called inside worker subprocess)."""


# ---------------------------------------------------------------------------
# ToCsvHandler
# ---------------------------------------------------------------------------

def topic_to_filename(topic: str) -> str:
    """Convert a topic like /foo/bar to foo_bar."""
    return topic.strip("/").replace("/", "_")


class ToCsvHandler(RosbagHandler):
    """Extract arbitrary ROS topics to CSV files (one file per topic per bag)."""

    def __init__(self, topics_list: List[str]) -> None:
        self._topics = list(dict.fromkeys(topics_list))  # dedup, preserve order
        self._records_by_topic: Dict[str, List[dict]] = {}
        self._parent_folder: str = ""
        self._bag_name: str = ""

    def topics(self) -> List[str]:
        return self._topics

    def on_begin(self, bag_path: str, topic_type_map: Dict[str, str]) -> None:
        self._parent_folder = self._out_dir(bag_path)
        self._bag_name = os.path.basename(bag_path)
        self._records_by_topic = {t: [] for t in self._topics}
        missing = [t for t in self._topics if t not in topic_type_map]
        if missing:
            print(f"  ℹ {bag_path}: topics not in bag: {missing}")

    def on_message(self, topic: str, msg: Any, timestamp: int) -> None:
        if topic in self._records_by_topic:
            fields = dict(gen_msg_values(msg))
            self._records_by_topic[topic].append(
                {"timestamp": timestamp, "type": type(msg).__name__, **fields}
            )

    def on_end(self) -> Tuple[int, List[str]]:
        base_fields = ["timestamp", "type"]
        total = 0
        output_files = []
        for topic, records in self._records_by_topic.items():
            if not records:
                print(f"  ✗ {self._bag_name} [{topic}]: no messages")
                continue
            fieldnames_set: set = set()
            for r in records:
                fieldnames_set.update(r.keys())
            other_fields = sorted(fieldnames_set - set(base_fields))
            fieldnames = base_fields + other_fields
            output_file = os.path.join(
                self._parent_folder,
                f"{self._bag_name}_{topic_to_filename(topic)}.csv",
            )
            with open(output_file, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                for r in records:
                    writer.writerow(r)
            print(f"  ✓ {output_file}: {len(records)} messages")
            total += len(records)
            output_files.append(output_file)
        return total, output_files

    @classmethod
    def from_config(cls, config: dict) -> "ToCsvHandler":
        topics = config.get("topics") or []
        if not topics:
            raise ValueError("to_csv handler requires 'topics' list")
        return cls(topics)


# ---------------------------------------------------------------------------
# TfToCsvHandler
# ---------------------------------------------------------------------------

def quat_to_rpy(x: float, y: float, z: float, w: float) -> Tuple[float, float, float]:
    """Convert quaternion (x, y, z, w) to roll, pitch, yaw in radians."""
    roll = math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y))
    pitch = math.asin(max(-1.0, min(1.0, 2 * (w * y - z * x))))
    yaw = math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))
    return roll, pitch, yaw


#: RoboVAST's pose-table contract (see ``docs/results_processing.rst``). ``tf_to_csv`` is one
#: producer of it; ``sim_poses``, written by the simulator itself, is another; a stack on some other
#: middleware becomes one by emitting these columns into the run directory.
#:
#: **Two clocks, on purpose.** ``timestamp`` is the bag receive time, like every other table here,
#: and is the join key the whole run view scrubs on -- never re-key it. But a receive time is only
#: as fine as the ``/clock`` grid the recorder's own clock advances on, and is jittered by delivery
#: on top, so DIFFERENCING it does not measure the robot: it measures the transport. ``stamp`` is
#: the publisher's own header stamp -- when the pose was true. Derive speeds from ``stamp``, join on
#: ``timestamp``. The same shape ``rosout_to_csv`` and ``nav2_bt_to_csv`` already use.
#:
#: **Quaternion, never Euler.** roll/pitch/yaw is lossy the moment a body leaves the plane -- a
#: drone, a tilting arm, a robot on a ramp -- and this was the one place the quaternion the bag
#: already carries got thrown away. ``orientation.yaw`` still exists downstream for the 2D
#: consumers: it is derived at ingest and labelled as the projection it is.
#:
#: Twist columns are declared and left empty here, because TF carries no velocity. A column present
#: for one producer and absent for another would make every query producer-specific.
POSE_FIELDNAMES = [
    "frame", "timestamp", "stamp",
    "position.x", "position.y", "position.z",
    "orientation.x", "orientation.y", "orientation.z", "orientation.w",
    "twist.linear.x", "twist.linear.y", "twist.linear.z",
    "twist.angular.x", "twist.angular.y", "twist.angular.z",
]


def _pose_row(frame: str, timestamp: int, transform, *, static: bool) -> dict:
    """One contract row from a resolved ``map -> frame`` transform.

    Free of every ROS import so it is unit-testable on a host with no ROS: it reads attributes off
    whatever it is handed. That matters because this is where the two clocks and the quaternion
    ordering are decided, and the enclosing handler cannot be imported without ``rosbag2_py`` and
    ``tf2_ros``.

    ``stamp`` is NULL for a transform that came from ``/tf_static``: a latched transform's header
    stamp is the single moment it was published, usually bag start and sometimes zero, so carrying
    it as a measurement time would be worse than admitting there is none.
    """
    t = transform.transform.translation
    r = transform.transform.rotation
    header = transform.header.stamp
    row = dict.fromkeys(POSE_FIELDNAMES, "")
    row.update({
        "frame": frame,
        "timestamp": timestamp / 1_000_000_000.0,
        "stamp": "" if static else header.sec + header.nanosec / 1_000_000_000.0,
        "position.x": t.x, "position.y": t.y, "position.z": t.z,
        "orientation.x": r.x, "orientation.y": r.y, "orientation.z": r.z, "orientation.w": r.w,
    })
    return row


class TfToCsvHandler(RosbagHandler):
    """Extract TF transforms to CSV (one file per bag).

    ``frames`` names the child frames to resolve against ``map``, or ``all`` for every child frame the
    bag carries. ``all`` exists for the viewer: a 3D scene animates a body per TF frame, and a world
    with people and movable props has one frame per skeleton bone and per prop -- listing them is
    per-world busywork that a new prop silently invalidates.

    ``require`` is what keeps ``all`` from costing anything. A named frame that yields nothing is a
    hard error (see :meth:`on_end`), and that assertion is load-bearing -- it is how a ground-truth
    frame missing from one simulator's bags was caught rather than silently analysed as absent. ``all``
    has no such expectation to check, so it takes the frames to assert on from ``require``; an explicit
    ``frames`` list implies ``require: <the same list>``, exactly as before.

    What neither mode captures: a frame published **once** on ``/tf_static`` before the dynamic chain
    to ``map`` exists. A transform is resolved when it arrives, and a robot description's static links
    are latched at bag start, while ``map -> odom`` only appears once localization begins -- so the
    lookup fails and is never retried. This is why ``all`` over a nav2 bag yields the moving frames
    rather than every link in the URDF. It costs a viewer nothing (a body welded in the scene is
    already baked at that pose), but asking for such a frame by name is a hard error, correctly.
    """

    _FIELDNAMES = POSE_FIELDNAMES

    #: ``frames`` value selecting every child frame in the bag instead of a fixed list.
    ALL = "all"

    def __init__(
        self,
        frames: Optional[List[str] | str] = None,
        csv_filename: str = "poses.csv",
        require: Optional[List[str]] = None,
    ) -> None:
        self._all_frames = isinstance(frames, str) and frames.lower() == self.ALL
        self._frames = [] if self._all_frames else (frames or ["base_link"])
        # Without `require`, an explicit list asserts on itself -- the pre-existing contract.
        self._require = list(require) if require else list(self._frames)
        self._csv_filename = csv_filename
        self._tf_buffer = None
        self._csvfile = None
        self._writer = None
        self._output_file: str = ""
        self._record_counts: Dict[str, int] = {}
        self._found_tfs: set = set()

    def topics(self) -> List[str]:
        return ["/tf", "/tf_static"]

    def on_begin(self, bag_path: str, topic_type_map: Dict[str, str]) -> None:
        # Import lazily — tf2_ros may not be available in all environments
        self._tf_buffer = Buffer()
        self._record_counts = {f: 0 for f in self._frames}
        self._found_tfs = set()
        self._output_file = os.path.join(
            self._out_dir(bag_path), self._csv_filename
        )
        self._csvfile = None
        self._writer = None

    def on_message(self, topic: str, msg: Any, timestamp: int) -> None:
        if topic not in ("/tf", "/tf_static"):
            return
        if not hasattr(msg, "transforms"):
            return
        # Feed /tf_static as static so it is valid for all time (tf2's own contract): otherwise a
        # latched transform like a static map->odom is stored at its single publish stamp, and any
        # later lookup_transform through it (e.g. map->base_link at a dynamic odom->base_link stamp)
        # raises ExtrapolationException and silently drops every sample. Setups whose whole chain is
        # dynamic (e.g. AMCL publishing map->odom on /tf) were unaffected; a static map->odom is.
        is_static = topic == "/tf_static"
        for transform in msg.transforms:
            if is_static:
                self._tf_buffer.set_transform_static(transform, "default_authority")
            else:
                self._tf_buffer.set_transform(transform, "default_authority")
            self._found_tfs.add(
                f"{transform.header.frame_id} -> {transform.child_frame_id}"
            )
            # In `all` mode the frame set is whatever the bag turns out to carry, discovered here as
            # transforms arrive rather than declared up front.
            if self._all_frames:
                self._record_counts.setdefault(transform.child_frame_id, 0)
                candidates = [transform.child_frame_id]
            else:
                candidates = self._frames
            for frame in candidates:
                if transform.child_frame_id != frame:
                    continue
                try:
                    map_to_frame = self._tf_buffer.lookup_transform(
                        "map", frame, transform.header.stamp
                    )
                    if self._csvfile is None:
                        self._csvfile = open(self._output_file, "w", newline="")
                        self._writer = csv.DictWriter(
                            self._csvfile, fieldnames=self._FIELDNAMES
                        )
                        self._writer.writeheader()
                    self._writer.writerow(
                        _pose_row(frame, timestamp, map_to_frame, static=is_static))
                    self._record_counts[frame] += 1
                except (LookupException, ConnectivityException, ExtrapolationException):
                    pass

    def on_end(self) -> Tuple[int, List[str]]:
        if self._csvfile is not None:
            self._csvfile.close()
        total = sum(self._record_counts.values())
        # A *required* frame yielding nothing is a defect in the run, not an empty result: the frame
        # was never published, or it is not connected to `map` in the TF tree. Reporting success would
        # hand the analysis a CSV that is silently missing a whole trajectory -- e.g. the ground-truth
        # frame a sim-vs-sim comparison is measured against. An explicit `frames` list requires itself,
        # so this is the pre-existing check; `all` mode requires only what `require` names, because
        # "every frame in the bag" states no expectation that could be violated.
        missing = [f for f in self._require if not self._record_counts.get(f)]
        if missing:
            found = "\n".join(f"    - {t}" for t in sorted(self._found_tfs)) or "    (none)"
            got = ", ".join(
                f"{f}: {c}" for f, c in self._record_counts.items() if c
            ) or "nothing"
            raise RuntimeError(
                f"no map-relative poses for required TF frame(s) {', '.join(missing)} "
                f"(extracted {got}). Either the frame is not published, or it does not connect to "
                f"'map'. Transforms present in the bag:\n{found}")
        # In `all` mode the counts include frames that were seen but never resolved against `map`
        # (a frame in another tree, or `map`'s own ancestors); those wrote nothing, so listing them
        # would bury the frames that did.
        counts = self._record_counts.items()
        summary = ", ".join(f"{f}: {c}" for f, c in counts if c or not self._all_frames)
        print(f"  ✓ {self._output_file}: {total} records ({summary})")
        return total, [self._output_file]

    @classmethod
    def from_config(cls, config: dict) -> "TfToCsvHandler":
        return cls(
            frames=config.get("frames"),
            csv_filename=config.get("csv_filename", "poses.csv"),
            require=config.get("require"),
        )


# ---------------------------------------------------------------------------
# Nav2BtLogToCsvHandler
# ---------------------------------------------------------------------------

class Nav2BtLogToCsvHandler(RosbagHandler):
    """Extract nav2's behavior-tree status transitions to CSV (one file per bag).

    nav2's ``bt_navigator`` publishes ``/behavior_tree_log``
    (``nav2_msgs/msg/BehaviorTreeLog``): each message carries a ``timestamp`` and a
    repeated ``event_log[]`` of status changes ``{timestamp, node_name, uid,
    previous_status, current_status}``. We explode that array to one CSV row per
    transition — the generic ``to_csv`` handler can't: it writes one row per published
    message and smears the variable-length ``event_log`` across indexed columns, which
    is the wrong grain for a per-node, per-time status timeline.

    The log is topology-free (transitions keyed by ``node_name`` only); tree structure is
    reconstructed downstream from the BT XML (see robovast_nav's ``Nav2BtTree``), which
    joins against this table's ``node_name`` column.

    ``uid`` is nav2's per-node id, and the only thing separating two nodes that share a
    ``node_name`` — an unnamed ``RecoveryNode`` or ``RateController`` appears once per
    instance in a default nav2 tree, so the name-keyed join above silently merges them.
    It is not a key into the BT XML (uids are assigned at tree construction and appear
    nowhere in the definition), which is why the join stays on the name; it tells a reader
    of this table which rows belong to one node. Absent from pre-Jazzy
    ``BehaviorTreeStatusChange``, where the column is empty.

    ``timestamp`` is the **bag receive time**, like every other table here, and not the event's own
    stamp. nav2 fills that stamp from a wall clock even under ``use_sim_time``, so it lands ~1.8e9 s
    away from the simulator's clock: a run whose bag spans 4-92 s of sim time produced BT events
    stamped 1785100900. Keying the table on that made it unjoinable with the poses, the costmaps and
    the scenario tree, and put every node transition outside the run view's timeline -- the behaviour
    tree panel simply had nothing to show at any playback time. The event's own stamp is still
    recorded, as ``event_timestamp``, since it is the only view of nav2's wall-clock pacing.
    """

    _BT_LOG_TOPIC = "/behavior_tree_log"
    _FIELDNAMES = [
        "timestamp", "node_name", "uid", "previous_status", "current_status", "event_timestamp",
    ]

    def __init__(self, csv_filename: str = "nav2_behavior_tree.csv") -> None:
        self._csv_filename = csv_filename
        self._csvfile = None
        self._writer = None
        self._record_count: int = 0
        self._output_file: str = ""

    def topics(self) -> List[str]:
        return [self._BT_LOG_TOPIC]

    def on_begin(self, bag_path: str, topic_type_map: Dict[str, str]) -> None:
        self._csvfile = None
        self._writer = None
        self._record_count = 0
        self._output_file = os.path.join(self._out_dir(bag_path), self._csv_filename)

    def on_message(self, topic: str, msg: Any, timestamp: int) -> None:
        if topic != self._BT_LOG_TOPIC:
            return
        for event in msg.event_log:
            # The bag receive time is the run's one clock (see the class docstring): every other table
            # here is on it, so this one is too. The event's own stamp rides along in its own column.
            event_stamp = getattr(event, "timestamp", None)
            event_ts = ""
            if event_stamp is not None and (event_stamp.sec or event_stamp.nanosec):
                event_ts = event_stamp.sec + event_stamp.nanosec / 1_000_000_000.0
            if self._csvfile is None:
                self._csvfile = open(self._output_file, "w", newline="")
                self._writer = csv.DictWriter(self._csvfile, fieldnames=self._FIELDNAMES)
                self._writer.writeheader()
            self._writer.writerow({
                "timestamp": timestamp / 1_000_000_000.0,
                "node_name": event.node_name,
                "uid": getattr(event, "uid", ""),
                "previous_status": str(event.previous_status).upper(),
                "current_status": str(event.current_status).upper(),
                "event_timestamp": event_ts,
            })
            self._record_count += 1

    def on_end(self) -> Tuple[int, List[str]]:
        if self._csvfile is not None:
            self._csvfile.close()
        if self._record_count > 0:
            print(f"  ✓ {self._output_file}: {self._record_count} BT status transitions")
            return self._record_count, [self._output_file]
        print(f"  ✗ {self._output_file}: no /behavior_tree_log records found")
        return 0, []

    @classmethod
    def from_config(cls, config: dict) -> "Nav2BtLogToCsvHandler":
        return cls(csv_filename=config.get("csv_filename", "nav2_behavior_tree.csv"))


# ---------------------------------------------------------------------------
# ActionToCsvHandler helpers
# ---------------------------------------------------------------------------

def _msg_to_dict(msg: Any) -> Any:  # pylint: disable=too-many-return-statements
    """Recursively convert a ROS message to a Python dict/list for flattening."""
    try:
        if isinstance(msg, np.ndarray):
            return msg.tolist()
    except ImportError:
        pass
    if isinstance(msg, bytes):
        return list(msg)
    if isinstance(msg, (bool, int, float, str)) or msg is None:
        return msg
    if hasattr(msg, "get_fields_and_field_types"):
        fields = set(msg.get_fields_and_field_types().keys())
        if fields == {"uuid"}:
            return bytearray(msg.uuid).hex()
        if fields == {"sec", "nanosec"}:
            return msg.sec + msg.nanosec / 1_000_000_000.0
        return {field: _msg_to_dict(getattr(msg, field)) for field in msg.get_fields_and_field_types()}
    try:
        return [_msg_to_dict(item) for item in msg]
    except TypeError:
        return msg


def _flatten_to_columns(obj: Any, prefix: str = "", sep: str = "_") -> Dict[str, Any]:
    """Recursively flatten nested dicts/lists to flat key-value pairs for CSV."""
    if isinstance(obj, dict):
        result: Dict[str, Any] = {}
        for key, val in obj.items():
            result.update(_flatten_to_columns(val, f"{prefix}{sep}{key}" if prefix else key, sep))
        return result
    if isinstance(obj, list):
        result = {}
        for i, item in enumerate(obj):
            result.update(_flatten_to_columns(item, f"{prefix}{sep}{i}", sep))
        return result
    return {prefix: obj}


# ---------------------------------------------------------------------------
# ActionToCsvHandler
# ---------------------------------------------------------------------------

class ActionToCsvHandler(RosbagHandler):
    """Extract ROS2 action feedback and status to CSV files."""

    def __init__(self, action: str, filename_prefix: Optional[str] = None) -> None:
        self._action_name = action.lstrip("/")
        self._filename_prefix = filename_prefix or f"action_{self._action_name}"
        self._feedback_topic = f"/{self._action_name}/_action/feedback"
        self._status_topic = f"/{self._action_name}/_action/status"
        self._feedback_rows: List[dict] = []
        self._status_rows: List[dict] = []
        self._parent_dir: str = ""

    def topics(self) -> List[str]:
        return [self._feedback_topic, self._status_topic]

    def on_begin(self, bag_path: str, topic_type_map: Dict[str, str]) -> None:
        self._feedback_rows = []
        self._status_rows = []
        self._parent_dir = self._out_dir(bag_path)
        available = set(topic_type_map)
        if self._feedback_topic not in available and self._status_topic not in available:
            action_topics = sorted(t for t in available if "_action" in t)
            msg = f"  ✗ {bag_path}: neither {self._feedback_topic} nor {self._status_topic} found"
            if action_topics:
                msg += f"\n    Action topics in bag: {action_topics}"
            print(msg)

    def on_message(self, topic: str, msg: Any, timestamp: int) -> None:
        entry = {"timestamp": timestamp / 1_000_000_000.0}
        entry.update(_msg_to_dict(msg))
        row = _flatten_to_columns(entry)
        if topic == self._feedback_topic:
            self._feedback_rows.append(row)
        elif topic == self._status_topic:
            self._status_rows.append(row)

    def on_end(self) -> Tuple[int, List[str]]:
        feedback_path = os.path.join(self._parent_dir, f"{self._filename_prefix}_feedback.csv")
        status_path = os.path.join(self._parent_dir, f"{self._filename_prefix}_status.csv")
        total = 0
        created = []
        if self._feedback_rows:
            all_keys = sorted(set().union(*(r.keys() for r in self._feedback_rows)))
            with open(feedback_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=all_keys, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(self._feedback_rows)
            total += len(self._feedback_rows)
            created.append(feedback_path)
        if self._status_rows:
            all_keys = sorted(set().union(*(r.keys() for r in self._status_rows)))
            with open(status_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=all_keys, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(self._status_rows)
            total += len(self._status_rows)
            created.append(status_path)
        if total > 0:
            print(
                f"  ✓ {self._filename_prefix}: "
                f"{len(self._feedback_rows)} feedback, {len(self._status_rows)} status messages"
            )
            return total, created
        print(f"  ✗ {self._parent_dir}: no messages on action topics for '{self._action_name}'")
        return 0, []

    @classmethod
    def from_config(cls, config: dict) -> "ActionToCsvHandler":
        action = config.get("action")
        if not action:
            raise ValueError("action_to_csv handler requires 'action' parameter")
        return cls(action=action, filename_prefix=config.get("filename_prefix"))


# ---------------------------------------------------------------------------
# RosoutToCsvHandler
# ---------------------------------------------------------------------------

_LEVEL_NAMES = {10: "DEBUG", 20: "INFO", 30: "WARN", 40: "ERROR", 50: "FATAL"}
_LEVEL_BY_NAME = {name: level for level, name in _LEVEL_NAMES.items()}
_ROSOUT_TOPIC = "/rosout"
_ROSOUT_FIELDNAMES = ["timestamp", "stamp", "level", "level_name", "name", "msg", "file", "function", "line"]


class RosoutToCsvHandler(RosbagHandler):
    """Extract /rosout log messages to CSV (one file per bag)."""

    def __init__(self, min_level: int = 10, csv_filename: str = "rosout.csv") -> None:
        self._min_level = min_level
        self._csv_filename = csv_filename
        self._csvfile = None
        self._writer = None
        self._record_count: int = 0
        self._output_file: str = ""

    def topics(self) -> List[str]:
        return [_ROSOUT_TOPIC]

    def on_begin(self, bag_path: str, topic_type_map: Dict[str, str]) -> None:
        self._record_count = 0
        self._output_file = os.path.join(
            self._out_dir(bag_path), self._csv_filename
        )
        if _ROSOUT_TOPIC not in topic_type_map:
            print(f"  ✗ {bag_path}: topic {_ROSOUT_TOPIC} not found in bag")
            self._csvfile = None
            self._writer = None
            return
        self._csvfile = open(self._output_file, "w", newline="")
        self._writer = csv.DictWriter(self._csvfile, fieldnames=_ROSOUT_FIELDNAMES)
        self._writer.writeheader()

    def on_message(self, topic: str, msg: Any, timestamp: int) -> None:
        if self._writer is None or topic != _ROSOUT_TOPIC:
            return
        if msg.level < self._min_level:
            return
        self._writer.writerow({
            "timestamp": timestamp / 1_000_000_000.0,
            "stamp": msg.stamp.sec + msg.stamp.nanosec / 1_000_000_000.0,
            "level": msg.level,
            "level_name": _LEVEL_NAMES.get(msg.level, str(msg.level)),
            "name": msg.name,
            "msg": msg.msg,
            "file": msg.file,
            "function": msg.function,
            "line": msg.line,
        })
        self._record_count += 1

    def on_end(self) -> Tuple[int, List[str]]:
        if self._csvfile is not None:
            self._csvfile.close()
        if self._record_count > 0:
            print(f"  ✓ {self._output_file}: {self._record_count} messages")
        else:
            print(f"  ✗ {self._output_file}: no rosout records (min_level={self._min_level})")
        # Always return the output file (header-only CSV is still useful for generate_data_db)
        return self._record_count, [self._output_file] if self._csvfile is not None else []

    @classmethod
    def from_config(cls, config: dict) -> "RosoutToCsvHandler":
        min_level_str = config.get("min_level", "DEBUG")
        min_level = _LEVEL_BY_NAME.get(min_level_str, 10)
        return cls(min_level=min_level, csv_filename=config.get("csv_filename", "rosout.csv"))


# ---------------------------------------------------------------------------
# ClockToCsvHandler
# ---------------------------------------------------------------------------

_CLOCK_TOPIC = "/clock"


class ClockToCsvHandler(RosbagHandler):
    """Extract the wall↔sim mapping from ``/clock`` (one ``clock_map.csv`` per bag).

    Only meaningful for a bag recorded **without** ``use_sim_time`` — the entrypoint's own
    infrastructure recording. There each message's receive time is wall and its content is
    sim, so the pair is an exact sample of the mapping, taken at clock rate for the whole
    container's life. (In the scenario's sim-time bag both would be sim, which is why that
    bag cannot carry this.)

    Thin I/O over :class:`~rosbags_common.ClockDecimator`: the accuracy
    promise and the reading side share one definition in ``rosbags_common`` (which travels
    with this script into the container) and ``clock_map`` (the host-side reader).
    """

    def __init__(self, tolerance_s: float = DEFAULT_CLOCK_TOLERANCE_S,
                 csv_filename: str = CLOCK_MAP_FILENAME) -> None:
        self._tolerance = tolerance_s
        self._csv_filename = csv_filename
        self._csvfile = None
        self._writer = None
        self._output_file: str = ""
        self._kept: int = 0
        self._decimator = ClockDecimator(tolerance_s)

    def topics(self) -> List[str]:
        return [_CLOCK_TOPIC]

    def on_begin(self, bag_path: str, topic_type_map: Dict[str, str]) -> None:
        self._kept = 0
        self._decimator = ClockDecimator(self._tolerance)
        self._output_file = os.path.join(self._out_dir(bag_path), self._csv_filename)
        if _CLOCK_TOPIC not in topic_type_map:
            # Not an error: a campaign may record only /rosout, and a non-ROS run has no
            # /clock at all. The absence is reported by the missing file, and every
            # consumer of the map treats "no map" as "no sim time", never as zero offset.
            print(f"  ✗ {bag_path}: topic {_CLOCK_TOPIC} not found in bag")
            self._csvfile = None
            self._writer = None
            return
        self._csvfile = open(self._output_file, "w", newline="")
        self._writer = csv.DictWriter(self._csvfile, fieldnames=CLOCK_MAP_FIELDNAMES)
        self._writer.writeheader()

    def _write(self, sample: Tuple[float, float]) -> None:
        self._writer.writerow({"wall_ts": sample[0], "sim_ts": sample[1]})
        self._kept += 1

    def on_message(self, topic: str, msg: Any, timestamp: int) -> None:
        if self._writer is None or topic != _CLOCK_TOPIC:
            return
        clock = getattr(msg, "clock", None)
        if clock is None:
            return
        keep = self._decimator.offer(timestamp / 1_000_000_000.0,
                                     clock.sec + clock.nanosec / 1_000_000_000.0)
        if keep is not None:
            self._write(keep)

    def on_end(self) -> Tuple[int, List[str]]:
        if self._writer is not None:
            final = self._decimator.close()
            if final is not None:
                self._write(final)
        if self._csvfile is not None:
            self._csvfile.close()
        if self._kept > 0:
            print(f"  ✓ {self._output_file}: {self._kept} clock samples "
                  f"(from {self._decimator.seen}, tolerance {self._tolerance}s)")
        else:
            print(f"  ✗ {self._output_file}: no clock samples")
        return self._kept, [self._output_file] if self._csvfile is not None else []

    @classmethod
    def from_config(cls, config: dict) -> "ClockToCsvHandler":
        return cls(
            tolerance_s=float(config.get("tolerance_s", DEFAULT_CLOCK_TOLERANCE_S)),
            csv_filename=config.get("csv_filename", CLOCK_MAP_FILENAME))


# ---------------------------------------------------------------------------
# ToWebmHandler
# ---------------------------------------------------------------------------

def _sanitize_topic(topic: str) -> str:
    """Convert a topic name like /camera/image_raw/compressed to camera_image_raw_compressed."""
    return re.sub(r"[^a-zA-Z0-9]+", "_", topic).strip("_")


class ToWebmHandler(RosbagHandler):
    """Convert a CompressedImage topic to a WebM video file via FFmpeg, and register it.

    Two things here exist because a camera may run at 25 fps, not only at the 1 Hz a monitor
    camera does:

    * **Frames are spooled to disk, not held in a list.** The fps cannot be chosen until the
      last stamp is known, so the frames have to outlive the read loop; keeping them in memory
      cost ~45,000 JPEGs (gigabytes) for half an hour at 25 fps, in the postprocessing
      container. Only ``(offset, length, stamp)`` stays resident.
    * **The keyframe interval is pinned.** Without ``-g`` the encoder's default decides how far
      a seek has to decode from, which is invisible at 1800 frames and painful at 45,000 -- and
      seeking is the whole point of the run-view panel that plays this.

    The encode stays CONSTANT-rate. ``fps = (n-1)/duration`` puts the first and last frames at
    exactly their recorded moments, so only mid-run jitter drifts; making it variable would
    change the shape of an artifact the analysis notebooks and the published videos zip already
    read, to fix a sub-second error at the rate this actually runs at.
    """

    _DEFAULT_TOPIC = "/camera/image_raw/compressed"
    _DEFAULT_FPS = 30.0
    #: Seconds of video between keyframes; ``-g`` is this many frames at the chosen rate.
    _KEYFRAME_SECONDS = 2.0

    def __init__(self, topic: str = _DEFAULT_TOPIC, default_fps: float = _DEFAULT_FPS) -> None:
        self._topic = topic
        self._default_fps = default_fps
        self._spool = None            # open temp file holding the JPEG bytes back to back
        self._sizes: List[int] = []   # byte length of each frame, in arrival order
        self._timestamps: List[int] = []
        self._output_file: str = ""
        self._out_folder: str = ""
        self._run_dir: str = ""

    def topics(self) -> List[str]:
        return [self._topic]

    def on_begin(self, bag_path: str, topic_type_map: Dict[str, str]) -> None:
        self._close_spool()
        self._sizes = []
        self._timestamps = []
        topic_suffix = _sanitize_topic(self._topic)
        bag_name = os.path.basename(bag_path)
        # The bag sits IN the run directory, so its parent is what a ``file`` in the manifest
        # is relative to -- that is the directory the run view addresses files under. Normally
        # identical to the output folder; a ``--output-dir`` elsewhere makes the path escape
        # with ``../``, which is the honest answer since nothing could serve it either way.
        self._run_dir = os.path.dirname(os.path.abspath(bag_path))
        self._out_folder = self._out_dir(bag_path)
        self._output_file = os.path.join(self._out_folder, f"{bag_name}_{topic_suffix}.webm")
        # Beside the output rather than in /tmp: the spool is as large as the frames, and a
        # container's /tmp is routinely the smallest filesystem it has.
        self._spool = tempfile.NamedTemporaryFile(  # pylint: disable=consider-using-with
            prefix=".webm-frames-", dir=self._out_folder, delete=False)
        if self._topic not in topic_type_map:
            print(f"  ✗ {bag_path}: topic '{self._topic}' not in bag")

    def _close_spool(self) -> None:
        """Close and delete the frame spool, if one is open."""
        if self._spool is None:
            return
        name = self._spool.name
        try:
            self._spool.close()
        finally:
            self._spool = None
            with contextlib.suppress(OSError):
                os.unlink(name)

    def on_message(self, topic: str, msg: Any, timestamp: int) -> None:
        if topic == self._topic and self._spool is not None:
            data = bytes(msg.data)
            self._spool.write(data)
            self._sizes.append(len(data))
            self._timestamps.append(timestamp)

    def on_end(self) -> Tuple[int, List[str]]:
        try:
            return self._encode()
        finally:
            self._close_spool()

    def _encode(self) -> Tuple[int, List[str]]:
        if not self._sizes or self._spool is None:
            print(f"  ✗ {self._output_file}: no frames")
            return 0, []

        n = len(self._sizes)
        if n > 1:
            duration_s = (self._timestamps[-1] - self._timestamps[0]) / 1e9
            fps = (n - 1) / duration_s if duration_s > 0 else self._default_fps
        else:
            fps = self._default_fps

        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-f", "image2pipe", "-vcodec", "mjpeg",
            "-r", f"{fps:.6f}",
            "-i", "pipe:0",
            "-c:v", "libvpx-vp9", "-crf", "10", "-b:v", "0",
            "-g", str(max(1, round(fps * self._KEYFRAME_SECONDS))),
            "-deadline", "realtime", "-cpu-used", "8",
            self._output_file,
        ]
        proc = subprocess.Popen(
            ffmpeg_cmd,
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        try:
            self._spool.flush()
            self._spool.seek(0)
            for size in self._sizes:
                proc.stdin.write(self._spool.read(size))
        except BrokenPipeError:
            pass
        finally:
            if proc.stdin:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
                proc.stdin = None

        _, stderr = proc.communicate()
        if proc.returncode != 0:
            print(f"  ✗ {self._output_file}: FFmpeg failed: {stderr.decode(errors='replace')}")
            return -2, []

        # Stamps in SECONDS, like every other table's, so a moment found in one is directly
        # comparable here and with the run view's playback clock.
        manifest = register_video(self._out_folder, {
            "topic": self._topic,
            "file": os.path.relpath(self._output_file, self._run_dir),
            "t_start": self._timestamps[0] / 1_000_000_000.0,
            "t_end": self._timestamps[-1] / 1_000_000_000.0,
            "fps": round(fps, 6),
            "frames": n,
        })
        print(f"  ✓ {self._output_file}: {n} frames @ {fps:.2f} fps")
        return n, [self._output_file, manifest]

    @classmethod
    def from_config(cls, config: dict) -> "ToWebmHandler":
        return cls(
            topic=config.get("topic", cls._DEFAULT_TOPIC),
            default_fps=float(config.get("fps", cls._DEFAULT_FPS)),
        )


# ---------------------------------------------------------------------------
# Handler registry
# ---------------------------------------------------------------------------

class CostmapToCsvHandler(RosbagHandler):
    """Compactly store nav_msgs/OccupancyGrid frames (costmaps / maps) for the web run-view.

    Each grid's int8 cells (-1..100, row-major) are stored losslessly as zlib-compressed raw
    bytes, base64-encoded, alongside its pose metadata -- one row per message in ``costmaps.csv``
    (a ``topic`` column keeps several layers, e.g. global/local/map, in one file). The web costmap
    panel fetches the frame nearest the playback time and inflates it in the browser. Occupancy
    grids are highly uniform, so this is far smaller than the per-cell flatten ``to_csv`` would
    produce (which also blows past SQLite's column limit for any real map) while keeping full
    precision. Not a batchable-by-default step: enable it with ``rosbags_costmap_to_csv`` naming
    the costmap topics recorded by the scenario's ``bag_record(...)``.
    """

    _FIELDNAMES = ["topic", "timestamp", "frame_id", "resolution", "width", "height",
                   "origin_x", "origin_y", "origin_yaw", "data"]

    def __init__(self, topics_list: List[str], csv_filename: str = "costmaps.csv") -> None:
        self._topics = list(dict.fromkeys(topics_list))  # dedup, preserve order
        self._csv_filename = csv_filename
        self._output_file = ""
        self._csvfile = None
        self._writer = None
        self._record_count = 0

    def topics(self) -> List[str]:
        return self._topics

    def on_begin(self, bag_path: str, topic_type_map: Dict[str, str]) -> None:
        self._output_file = os.path.join(self._out_dir(bag_path), self._csv_filename)
        self._csvfile = None
        self._writer = None
        self._record_count = 0
        missing = [t for t in self._topics if t not in topic_type_map]
        if missing:
            print(f"  ℹ {bag_path}: costmap topics not in bag: {missing}")

    def on_message(self, topic: str, msg: Any, timestamp: int) -> None:
        if topic not in self._topics:
            return
        info = msg.info
        o = info.origin
        _, _, yaw = quat_to_rpy(o.orientation.x, o.orientation.y,
                                o.orientation.z, o.orientation.w)
        # int8 cells -> raw bytes -> zlib -> base64. The browser inflates and reads an Int8Array,
        # recovering -1..100 exactly (the high bit maps back to the negative "unknown" value).
        cells = np.asarray(msg.data, dtype=np.int8).tobytes()
        payload = base64.b64encode(zlib.compress(cells, 9)).decode("ascii")
        if self._csvfile is None:
            self._csvfile = open(self._output_file, "w", newline="")
            self._writer = csv.DictWriter(self._csvfile, fieldnames=self._FIELDNAMES)
            self._writer.writeheader()
        self._writer.writerow({
            "topic": topic,
            "timestamp": timestamp / 1_000_000_000.0,
            "frame_id": msg.header.frame_id,
            "resolution": info.resolution,
            "width": info.width,
            "height": info.height,
            "origin_x": o.position.x,
            "origin_y": o.position.y,
            "origin_yaw": yaw,
            "data": payload,
        })
        self._record_count += 1

    def on_end(self) -> Tuple[int, List[str]]:
        if self._csvfile is not None:
            self._csvfile.close()
        if self._record_count > 0:
            print(f"  ✓ {self._output_file}: {self._record_count} costmap frames")
            return self._record_count, [self._output_file]
        print(f"  ✗ {self._output_file}: no costmap frames found")
        return 0, []

    @classmethod
    def from_config(cls, config: dict) -> "CostmapToCsvHandler":
        topics = config.get("topics") or []
        if not topics:
            raise ValueError("costmap_to_csv handler requires 'topics' list")
        return cls(topics, csv_filename=config.get("csv_filename", "costmaps.csv"))


HANDLER_REGISTRY: Dict[str, type] = {
    "to_csv":         ToCsvHandler,
    "tf_to_csv":      TfToCsvHandler,
    "nav2_bt_to_csv": Nav2BtLogToCsvHandler,
    "action_to_csv":  ActionToCsvHandler,
    "rosout_to_csv":  RosoutToCsvHandler,
    "clock_to_csv":   ClockToCsvHandler,
    "costmap_to_csv": CostmapToCsvHandler,
    "to_webm":        ToWebmHandler,
}


# ---------------------------------------------------------------------------
# Per-bag cache helpers
# ---------------------------------------------------------------------------

_CACHE_FILENAME = ".robovast_rosbags_process_cache"


def _bag_fingerprint(bag_path: str, plugin_configs_hash: str) -> str:
    """Stable fingerprint of a bag directory + plugin configs (mtime + size, no content read)."""
    parts: List[str] = []
    for root, _, files in os.walk(bag_path):
        for f in sorted(files):
            fp = os.path.join(root, f)
            try:
                stat = os.stat(fp)
                rel = os.path.relpath(fp, bag_path)
                parts.append(f"{rel}:{stat.st_mtime:.6f}:{stat.st_size}")
            except OSError:
                pass
    parts.sort()
    parts.append(f"plugins:{plugin_configs_hash}")
    return hashlib.md5("|".join(parts).encode()).hexdigest()


def _read_cache(run_dir: str) -> Optional[str]:
    try:
        with open(os.path.join(run_dir, _CACHE_FILENAME), "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def _write_cache(run_dir: str, fingerprint: str) -> None:
    try:
        with open(os.path.join(run_dir, _CACHE_FILENAME), "w", encoding="utf-8") as f:
            f.write(fingerprint)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Per-bag worker
# ---------------------------------------------------------------------------

def process_rosbag_worker(args: tuple) -> Tuple[str, int, List[Tuple[int, List[str]]]]:
    """Process a single rosbag with all configured handlers.

    Args:
        args: (bag_path, plugin_configs, debug, force, plugin_configs_hash, out_dir)
              where plugin_configs is a list of handler config dicts, debug controls
              per-bag output, and out_dir is where this bag's outputs go (``None`` =
              beside the bag, the local default).

    Returns:
        (bag_path, total_records, handler_results) where handler_results is a list of
        (record_count, output_files) per handler. total_records == -2 if the
        bag itself failed to open.
    """
    bag_path, plugin_configs, debug, force, plugin_configs_hash, out_dir = args
    run_dir = os.path.dirname(bag_path)
    fingerprint: Optional[str] = None

    if not force:
        fingerprint = _bag_fingerprint(bag_path, plugin_configs_hash)
        if _read_cache(run_dir) == fingerprint:
            return bag_path, -1, []  # cache hit — already processed

    with contextlib.redirect_stdout(sys.stdout if debug else io.StringIO()):
        # Instantiate handlers from config inside the worker (avoids pickling issues)
        handlers: List[RosbagHandler] = []
        for cfg in plugin_configs:
            handler_type = cfg.get("type", "")
            handler_cls = HANDLER_REGISTRY.get(handler_type)
            if handler_cls is None:
                print(f"  ✗ Unknown handler type '{handler_type}' — skipping")
                continue
            try:
                handler = handler_cls.from_config(cfg)
                handler.output_dir = out_dir  # None = beside the bag (local default)
                handlers.append(handler)
            except Exception as e:
                print(f"  ✗ Handler '{handler_type}' init failed: {e}")

        if not handlers:
            return bag_path, -2, []

        # Detect storage format from metadata.yaml, fall back to mcap
        storage_id = "mcap"
        metadata_path = os.path.join(bag_path, "metadata.yaml")
        if os.path.isfile(metadata_path):
            try:
                with open(metadata_path, "r", encoding="utf-8") as _f:
                    _meta = yaml.safe_load(_f) or {}
                storage_id = (
                    _meta.get("rosbag2_bagfile_information", {}).get("storage_identifier")
                    or "mcap"
                )
            except Exception:
                pass

        # Open bag
        try:
            reader = rosbag2_py.SequentialReader()
            reader.open(
                rosbag2_py.StorageOptions(uri=bag_path, storage_id=storage_id),
                rosbag2_py.ConverterOptions(
                    input_serialization_format="cdr",
                    output_serialization_format="cdr",
                ),
            )
            topic_type_map: Dict[str, str] = {
                t.name: t.type for t in reader.get_all_topics_and_types()
            }
        except Exception as e:
            print(f"✗ {bag_path}: failed to open — {e}")
            return bag_path, -2, []

        # Call on_begin for each handler; remove those that fail
        active_handlers: List[RosbagHandler] = []
        for h in handlers:
            try:
                h.on_begin(bag_path, topic_type_map)
                active_handlers.append(h)
            except Exception as e:
                print(f"  ✗ Handler {type(h).__name__} on_begin failed: {e}")

        if not active_handlers:
            return bag_path, 0, []

        # Build topic→handlers dispatch map (intersect with topics in this bag)
        topic_to_handlers: Dict[str, List[RosbagHandler]] = {}
        for h in active_handlers:
            for t in h.topics():
                if t in topic_type_map:
                    topic_to_handlers.setdefault(t, []).append(h)

        # Pre-load message types once for all subscribed+available topics
        msg_type_cache: Dict[str, type] = {}
        for topic in topic_to_handlers:
            try:
                msg_type_cache[topic] = get_message(topic_type_map[topic])
            except Exception as e:
                print(f"  ✗ Could not load message type for {topic}: {e}")

        # Main read loop — deserialize each message at most once
        while reader.has_next():
            topic, data, timestamp = reader.read_next()
            if topic not in topic_to_handlers:
                continue
            msg_cls = msg_type_cache.get(topic)
            if msg_cls is None:
                continue
            try:
                msg = deserialize_message(data, msg_cls)
            except Exception as e:
                print(f"  ✗ Deserialization error on {topic}: {e}")
                continue
            for h in topic_to_handlers[topic]:
                try:
                    h.on_message(topic, msg, timestamp)
                except Exception as e:
                    print(f"  ✗ Handler {type(h).__name__} on_message error: {e}")

        # Collect results
        handler_results: List[Tuple[int, List[str]]] = []
        for h in active_handlers:
            try:
                result = h.on_end()
                handler_results.append(result)
            except Exception as e:
                print(f"  ✗ Handler {type(h).__name__} on_end error: {e}")
                handler_results.append((-2, []))

    total = sum(r for r, _ in handler_results if r > 0)
    if all(r != -2 for r, _ in handler_results):
        if fingerprint is None:
            fingerprint = _bag_fingerprint(bag_path, plugin_configs_hash)
        _write_cache(run_dir, fingerprint)
    return bag_path, total, handler_results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input",
        help="Input directory path to search for rosbags",
    )
    parser.add_argument(
        "--config",
        required=True,
        help='JSON config string: {"plugins": [{"type": "...", ...}, ...]}',
    )
    parser.add_argument(
        "--bag-dir",
        default="rosbag2",
        help="Name of the rosbag subdirectory within each run directory (default: rosbag2)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=cpu_count(),
        help=f"Number of parallel workers (default: {cpu_count()})",
    )
    parser.add_argument(
        "--provenance-file",
        default=None,
        help="Write provenance JSON to this path (paths relative to input dir)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print per-bag processing details (default: progress bar only)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reprocess all bags even if already cached",
    )
    parser.add_argument(
        "--tolerate-under",
        action="append",
        default=[],
        metavar="REL_DIR",
        help="A directory, relative to the input root, whose unreadable bags are "
             "EXPECTED rather than a failure. Given for each job an operator stopped by "
             "hand: killing a pod mid-write leaves its rosbag unfinalized, so it cannot "
             "be opened and never will be. Such a bag is still attempted (a kill that "
             "landed between bags leaves readable ones, and their data is worth having); "
             "it just does not fail the step. Repeatable.",
    )
    parser.add_argument(
        "--output-root",
        default=None,
        help="Write outputs under this root, mirroring each bag's path relative to "
             "the input dir (e.g. <output-root>/<config>/<run>/). Default: write "
             "beside each bag, i.e. into the input tree itself. Use this when the "
             "input is read-only or lives in a different volume than the outputs "
             "(the in-cluster postprocessing Job mounts bags and outputs separately).",
    )
    args = parser.parse_args()

    try:
        plugin_configs: List[dict] = json.loads(args.config)["plugins"]
    except (json.JSONDecodeError, KeyError) as e:
        print(f"Error: invalid --config JSON: {e}")
        return 1

    if not plugin_configs:
        print("Error: --config must contain at least one plugin")
        return 1

    # Validate handler types up front
    unknown = [c.get("type") for c in plugin_configs if c.get("type") not in HANDLER_REGISTRY]
    if unknown:
        print(f"Error: unknown handler type(s): {unknown}. Available: {list(HANDLER_REGISTRY)}")
        return 1

    print(f"Scanning for rosbags ({args.bag_dir})...", end="", flush=True)
    _t_scan = time.time()
    rosbag_paths = find_rosbags(args.input, bag_dir_name=args.bag_dir)
    print(f"\r{len(rosbag_paths)} rosbags found in {time.time() - _t_scan:.1f}s{' ' * 20}")
    if not rosbag_paths:
        return 0

    types_desc = ", ".join(c.get("type", "?") for c in plugin_configs)
    print(
        f"Handlers: [{types_desc}]  workers: {args.workers}"
    )

    plugin_configs_hash = hashlib.md5(
        json.dumps(plugin_configs, sort_keys=True).encode()
    ).hexdigest()
    n_bags = len(rosbag_paths)
    input_root = os.path.abspath(args.input)

    # Bags whose failure to open is expected: their job was killed by hand mid-write (see
    # --tolerate-under). The predicate lives in rosbags_common so it is testable on the
    # host — this script cannot be imported there, it pulls in rosbag2_py at module level.
    _tolerated_roots = resolve_tolerated_roots(input_root, args.tolerate_under)

    def _is_tolerated(bag_path: str) -> bool:
        return is_under_tolerated_root(bag_path, _tolerated_roots)

    def _out_dir_for(bag_path: str) -> Optional[str]:
        """Mirror the bag's location under --output-root, or None (beside the bag)."""
        if not args.output_root:
            return None
        rel = os.path.relpath(os.path.dirname(os.path.abspath(bag_path)), input_root)
        return os.path.join(os.path.abspath(args.output_root), rel)

    process_args = [
        (bag_path, plugin_configs, args.debug, args.force, plugin_configs_hash,
         _out_dir_for(bag_path))
        for bag_path in rosbag_paths
    ]

    start = time.time()
    total_records = 0
    processed_bags = 0
    cached_bags = 0
    error_bags = 0
    # Errors under a --tolerate-under dir: reported, never fatal. Counted apart from
    # ``error_bags`` rather than ignored, so "we skipped 1 unreadable bag" stays visible
    # instead of a campaign quietly having less data than it looks like it has.
    expected_error_bags = 0
    failed_bags = 0
    completed = 0
    all_results: List[Tuple[str, int, List[Tuple[int, List[str]]]]] = []

    try:
        with Pool(processes=args.workers) as pool:
            for bag_path, bag_total, handler_results in pool.imap_unordered(
                process_rosbag_worker, process_args, chunksize=1
            ):
                completed += 1
                elapsed = max(time.time() - start, 1e-6)
                rate = completed / elapsed
                filled = int(20 * completed / n_bags)
                progressbar = "█" * filled + "░" * (20 - filled)
                pct = completed / n_bags * 100
                remaining = (n_bags - completed) / rate
                eta_m, eta_s = divmod(int(remaining), 60)
                eta_str = f"{eta_m}m{eta_s:02d}s" if eta_m else f"{eta_s}s"
                print(
                    f"Processing rosbags  [{progressbar}]  {pct:5.1f}%"
                    f"  {completed}/{n_bags} bag  {rate:.1f} bag/s  ETA {eta_str}",
                    flush=True,
                )
                all_results.append((bag_path, bag_total, handler_results))
    except KeyboardInterrupt:
        print("Processing interrupted by user.")
        return 1

    # Aggregate and write provenance
    for bag_path, bag_total, handler_results in all_results:
        if bag_total == -1:
            cached_bags += 1
            continue
        if bag_total == -2:
            if _is_tolerated(bag_path):
                expected_error_bags += 1
            else:
                error_bags += 1
            continue

        source_rel = os.path.relpath(bag_path, input_root)
        bag_had_records = False

        for j, (record_count, output_files) in enumerate(handler_results):
            if record_count == -2:
                if _is_tolerated(bag_path):
                    expected_error_bags += 1
                else:
                    error_bags += 1
                continue
            if record_count > 0:
                total_records += record_count
                bag_had_records = True
            if output_files and args.provenance_file:
                cfg = plugin_configs[j]
                for output_file in output_files:
                    output_rel = os.path.relpath(output_file, input_root)
                    write_provenance_entry(
                        args.provenance_file,
                        output_rel,
                        [source_rel],
                        f"rosbags_process/{cfg.get('type', 'unknown')}",
                        params=cfg,
                    )

        if bag_had_records:
            processed_bags += 1
        else:
            failed_bags += 1

    elapsed = time.time() - start
    cached_str = f", {cached_bags} cached" if cached_bags else ""
    killed_str = f", {expected_error_bags} from stopped job(s)" if expected_error_bags else ""
    print(
        f"Summary: {len(rosbag_paths)} rosbags "
        f"({processed_bags} success{cached_str}, {error_bags} errors{killed_str}, "
        f"{failed_bags} no-data), {total_records} total records, {elapsed:.2f}s"
    )
    if expected_error_bags:
        # Stated, not silent: the campaign really does have less data than its run count
        # suggests, and the reason is a decision somebody made rather than a defect.
        print(f"NOTE: {expected_error_bags} unreadable bag(s) belong to job(s) stopped by "
              f"hand — expected, not counted as failures")
    # A handler that failed outright must not be reported as a successful postprocessing step: this
    # exit code is what the campaign's results phase reads, and returning 0 regardless meant a run
    # could be graded on data a handler had already refused to produce. A bag left unfinalized by a
    # deliberate kill is the one exception, and it is excluded above rather than here — so the
    # campaign that ran a per-job stop still gets its metrics for every job that did finish.
    if error_bags:
        print(f"ERROR: {error_bags} handler error(s) — see the messages above")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
