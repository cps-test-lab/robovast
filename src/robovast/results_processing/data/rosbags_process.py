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
  bt_to_csv       Extract behavior tree snapshots to CSV
  action_to_csv   Extract ROS2 action feedback/status to CSV
  rosout_to_csv   Extract /rosout log messages to CSV

Usage::

    rosbags_process.py INPUT_DIR \\
        --config '{"plugins": [{"type": "rosout_to_csv"}, {"type": "bt_to_csv"}]}' \\
        --workers 4 \\
        --provenance-file /provenance/process_provenance.json
"""

import argparse
import csv
import json
import math
import os
import sys
import time
from abc import ABC, abstractmethod
from multiprocessing import Pool, cpu_count
from typing import Any, Dict, List, Optional, Tuple

import rosbag2_py
from rclpy.serialization import deserialize_message
from rosbags_common import find_rosbags, gen_msg_values, write_provenance_entry
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
        self._parent_folder = os.path.abspath(os.path.dirname(bag_path))
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


class TfToCsvHandler(RosbagHandler):
    """Extract TF transforms to CSV (one file per bag)."""

    _FIELDNAMES = [
        "frame", "timestamp",
        "position.x", "position.y", "position.z",
        "orientation.roll", "orientation.pitch", "orientation.yaw",
    ]

    def __init__(self, frames: Optional[List[str]] = None, csv_filename: str = "poses.csv") -> None:
        self._frames = frames or ["base_link"]
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
        from tf2_ros import Buffer  # noqa: PLC0415
        self._tf_buffer = Buffer()
        self._record_counts = {f: 0 for f in self._frames}
        self._found_tfs = set()
        self._output_file = os.path.join(
            os.path.abspath(os.path.dirname(bag_path)), self._csv_filename
        )
        self._csvfile = None
        self._writer = None

    def on_message(self, topic: str, msg: Any, timestamp: int) -> None:
        from tf2_py import ConnectivityException, ExtrapolationException, LookupException  # noqa: PLC0415
        if topic not in ("/tf", "/tf_static"):
            return
        if not hasattr(msg, "transforms"):
            return
        for transform in msg.transforms:
            self._tf_buffer.set_transform(transform, "default_authority")
            self._found_tfs.add(
                f"{transform.header.frame_id} -> {transform.child_frame_id}"
            )
            for frame in self._frames:
                if transform.child_frame_id != frame:
                    continue
                try:
                    map_to_frame = self._tf_buffer.lookup_transform(
                        "map", frame, transform.header.stamp
                    )
                    t = map_to_frame.transform.translation
                    r = map_to_frame.transform.rotation
                    roll, pitch, yaw = quat_to_rpy(r.x, r.y, r.z, r.w)
                    if self._csvfile is None:
                        self._csvfile = open(self._output_file, "w", newline="")
                        self._writer = csv.DictWriter(
                            self._csvfile, fieldnames=self._FIELDNAMES
                        )
                        self._writer.writeheader()
                    self._writer.writerow({
                        "frame": frame,
                        "timestamp": timestamp / 1_000_000_000.0,
                        "position.x": t.x,
                        "position.y": t.y,
                        "position.z": t.z,
                        "orientation.roll": roll,
                        "orientation.pitch": pitch,
                        "orientation.yaw": yaw,
                    })
                    self._record_counts[frame] += 1
                except (LookupException, ConnectivityException, ExtrapolationException):
                    pass

    def on_end(self) -> Tuple[int, List[str]]:
        if self._csvfile is not None:
            self._csvfile.close()
        total = sum(self._record_counts.values())
        if total > 0:
            summary = ", ".join(
                f"{f}: {c}" for f, c in self._record_counts.items() if c > 0
            )
            print(f"  ✓ {self._output_file}: {total} records ({summary})")
            return total, [self._output_file]
        print(f"  ✗ {self._output_file}: no records found")
        if len(self._frames) == 1:
            print(f"    Found TF frames:\n" + "\n".join(f"    - {t}" for t in self._found_tfs))
        return 0, []

    @classmethod
    def from_config(cls, config: dict) -> "TfToCsvHandler":
        return cls(
            frames=config.get("frames"),
            csv_filename=config.get("csv_filename", "poses.csv"),
        )


# ---------------------------------------------------------------------------
# BtToCsvHandler
# ---------------------------------------------------------------------------

class BtToCsvHandler(RosbagHandler):
    """Extract behavior tree status changes to CSV (one file per bag)."""

    _SNAPSHOTS_TOPIC = "/scenario_execution/snapshots"
    _FIELDNAMES = ["timestamp", "behavior_name", "behavior_id", "status", "status_name", "class_name"]
    _STATUS_NAMES = {1: "INVALID", 2: "RUNNING", 3: "SUCCESS", 4: "FAILURE"}

    def __init__(self, csv_filename: str = "behaviors.csv") -> None:
        self._csv_filename = csv_filename
        self._uuid_to_int: Dict[str, int] = {}
        self._next_id: int = 1
        self._last_status: Dict[tuple, int] = {}
        self._csvfile = None
        self._writer = None
        self._record_count: int = 0
        self._output_file: str = ""

    def topics(self) -> List[str]:
        return [self._SNAPSHOTS_TOPIC]

    def on_begin(self, bag_path: str, topic_type_map: Dict[str, str]) -> None:
        self._uuid_to_int = {}
        self._next_id = 1
        self._last_status = {}
        self._record_count = 0
        self._csvfile = None
        self._writer = None
        self._output_file = os.path.join(
            os.path.abspath(os.path.dirname(bag_path)), self._csv_filename
        )

    def on_message(self, topic: str, msg: Any, timestamp: int) -> None:
        if topic != self._SNAPSHOTS_TOPIC:
            return
        for behavior in msg.behaviours:
            uuid_str = str(behavior.own_id)
            if uuid_str not in self._uuid_to_int:
                self._uuid_to_int[uuid_str] = self._next_id
                self._next_id += 1
            behavior_id = self._uuid_to_int[uuid_str]
            key = (behavior.name, behavior_id)
            if self._last_status.get(key) == behavior.status:
                continue
            self._last_status[key] = behavior.status
            if self._csvfile is None:
                self._csvfile = open(self._output_file, "w", newline="")
                self._writer = csv.DictWriter(self._csvfile, fieldnames=self._FIELDNAMES)
                self._writer.writeheader()
            self._writer.writerow({
                "timestamp": timestamp / 1_000_000_000.0,
                "behavior_name": behavior.name,
                "behavior_id": behavior_id,
                "status": behavior.status,
                "status_name": self._STATUS_NAMES.get(behavior.status, "UNKNOWN"),
                "class_name": behavior.class_name,
            })
            self._record_count += 1

    def on_end(self) -> Tuple[int, List[str]]:
        if self._csvfile is not None:
            self._csvfile.close()
        if self._record_count > 0:
            print(f"  ✓ {self._output_file}: {self._record_count} status records")
            return self._record_count, [self._output_file]
        print(f"  ✗ {self._output_file}: no behavior records found")
        return 0, []

    @classmethod
    def from_config(cls, config: dict) -> "BtToCsvHandler":
        return cls(csv_filename=config.get("csv_filename", "behaviors.csv"))


# ---------------------------------------------------------------------------
# ActionToCsvHandler helpers
# ---------------------------------------------------------------------------

def _msg_to_dict(msg: Any) -> Any:  # pylint: disable=too-many-return-statements
    """Recursively convert a ROS message to a Python dict/list for flattening."""
    try:
        import numpy as np  # noqa: PLC0415
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
        self._parent_dir = os.path.dirname(bag_path)
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
            os.path.abspath(os.path.dirname(bag_path)), self._csv_filename
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
# Handler registry
# ---------------------------------------------------------------------------

HANDLER_REGISTRY: Dict[str, type] = {
    "to_csv":        ToCsvHandler,
    "tf_to_csv":     TfToCsvHandler,
    "bt_to_csv":     BtToCsvHandler,
    "action_to_csv": ActionToCsvHandler,
    "rosout_to_csv": RosoutToCsvHandler,
}


# ---------------------------------------------------------------------------
# Per-bag worker
# ---------------------------------------------------------------------------

def process_rosbag_worker(args: tuple) -> Tuple[str, int, List[Tuple[int, List[str]]]]:
    """Process a single rosbag with all configured handlers.

    Args:
        args: (bag_path, plugin_configs) where plugin_configs is a list of
              handler config dicts.

    Returns:
        (bag_path, total_records, handler_results) where handler_results is a list of
        (record_count, output_files) per handler. total_records == -2 if the
        bag itself failed to open.
    """
    bag_path, plugin_configs = args

    # Instantiate handlers from config inside the worker (avoids pickling issues)
    handlers: List[RosbagHandler] = []
    for cfg in plugin_configs:
        handler_type = cfg.get("type", "")
        handler_cls = HANDLER_REGISTRY.get(handler_type)
        if handler_cls is None:
            print(f"  ✗ Unknown handler type '{handler_type}' — skipping")
            continue
        try:
            handlers.append(handler_cls.from_config(cfg))
        except Exception as e:
            print(f"  ✗ Handler '{handler_type}' init failed: {e}")

    if not handlers:
        return -2, []

    # Open bag
    try:
        reader = rosbag2_py.SequentialReader()
        reader.open(
            rosbag2_py.StorageOptions(uri=bag_path, storage_id="mcap"),
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
        return -2, []

    # Call on_begin for each handler; remove those that fail
    active_handlers: List[RosbagHandler] = []
    for h in handlers:
        try:
            h.on_begin(bag_path, topic_type_map)
            active_handlers.append(h)
        except Exception as e:
            print(f"  ✗ Handler {type(h).__name__} on_begin failed: {e}")

    if not active_handlers:
        return 0, []

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

    rosbag_paths = find_rosbags(args.input, bag_dir_name=args.bag_dir)
    if not rosbag_paths:
        print(f"No rosbags found in {args.input}")
        return 0

    types_desc = ", ".join(c.get("type", "?") for c in plugin_configs)
    print(
        f"Found {len(rosbag_paths)} rosbags, handlers: [{types_desc}], "
        f"workers: {args.workers}"
    )

    process_args = [(bag_path, plugin_configs) for bag_path in rosbag_paths]
    n_bags = len(rosbag_paths)
    input_root = os.path.abspath(args.input)

    start = time.time()
    total_records = 0
    processed_bags = 0
    error_bags = 0
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
                bar = "█" * filled + "░" * (20 - filled)
                pct = completed / n_bags * 100
                print(
                    f"Processing rosbags  [{bar}]  {pct:5.1f}%"
                    f"  {completed}/{n_bags} bag  {rate:.1f} bag/s",
                    flush=True,
                )
                all_results.append((bag_path, bag_total, handler_results))
    except KeyboardInterrupt:
        print("Processing interrupted by user.")
        return 1

    # Aggregate and write provenance
    for bag_path, bag_total, handler_results in all_results:
        if bag_total == -2:
            error_bags += 1
            continue

        source_rel = os.path.relpath(bag_path, input_root)
        bag_had_records = False

        for j, (record_count, output_files) in enumerate(handler_results):
            if record_count == -2:
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
    print(
        f"Summary: {len(rosbag_paths)} rosbags "
        f"({processed_bags} success, {error_bags} errors, {failed_bags} no-data), "
        f"{total_records} total records, {elapsed:.2f}s"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
