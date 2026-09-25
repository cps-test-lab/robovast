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

"""What turns a recorded topic into a table's rows.

A handler reads a fixed set of topics and fills one or more tables. The decoder feeds it every
message of those topics, once, in the order they were recorded, already deserialized. Each
handler keeps the table names, columns and clocks that table has always had, because a
table's name and columns are what queries, panels and notebooks address:

=====================  ===============================  ==========================================
handler                table                             rows
=====================  ===============================  ==========================================
:class:`TfPoses`       ``poses``                         one per resolved ``map -> frame`` sample
:class:`TopicTable`    ``<bag>_<topic>``                 one per message, fields as columns
:class:`Nav2BtLog`     ``nav2_behavior_tree``            one per behaviour-tree status change
:class:`ActionTopics`  ``action_<name>_feedback/status`` one per message, flattened
:class:`Rosout`        ``rosout``                        one per log message at or above a level
:class:`Clock`         ``clock_map``                     the decimated wall -> sim samples
:class:`Costmaps`      ``costmaps``                      one per occupancy grid, cells compressed
:class:`Videos`        ``videos``                        one per encoded camera topic
=====================  ===============================  ==========================================

**One clock per run.** ``timestamp`` is the bag's receive time -- in seconds, except in a
topic's own table, which has always carried it in nanoseconds -- and every table is joinable
on it. A topic's own stamp, where it has one, is kept beside it under its own name.
"""

from __future__ import annotations

import base64
import contextlib
import math
import os
import re
import shutil
import subprocess
import tempfile
import zlib
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from .tables import TableBuffer, fixed, leading_then_sorted
from .tf import TransformBuffer, TransformError
from .values import (DEFAULT_CLOCK_TOLERANCE_S, ClockDecimator, column_values, flatten,
                     message_to_dict)


class HandlerError(RuntimeError):
    """A handler that read its input and could not produce a truthful table from it."""


class Handler:
    """Base: the topics read, the tables filled, and the per-message hook."""

    def __init__(self):
        self.buffers: Dict[str, TableBuffer] = {}
        self.orders: Dict[str, Callable] = {}
        self.fields_of = None      # set by the decoder: type name -> rosbags field definitions

    def topics(self) -> List[str]:
        raise NotImplementedError

    def tables(self) -> List[str]:
        """The tables this handler fills, whether or not the recording yields rows for them."""
        raise NotImplementedError

    def message(self, topic: str, msg, typename: str, log_time: int) -> None:
        raise NotImplementedError

    def end(self, recorded: Dict[str, str]) -> None:
        """Called after the last message with ``{topic: type}`` of everything recorded.

        Raise :class:`HandlerError` for a table that would not be truthful.
        """

    def _buffer(self, table: str, order: Optional[Callable] = None) -> TableBuffer:
        buf = self.buffers.get(table)
        if buf is None:
            buf = self.buffers[table] = TableBuffer(table)
            if order is not None:
                self.orders[table] = order
        return buf


def _seconds(stamp) -> float:
    return stamp.sec + stamp.nanosec / 1_000_000_000.0


def _stamp_ns(stamp) -> int:
    return stamp.sec * 1_000_000_000 + stamp.nanosec


def quat_to_rpy(x: float, y: float, z: float, w: float):
    roll = math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y))
    pitch = math.asin(max(-1.0, min(1.0, 2 * (w * y - z * x))))
    yaw = math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))
    return roll, pitch, yaw


# -- poses ------------------------------------------------------------------------------------

#: RoboVAST's pose-table contract. ``timestamp`` is the receive time and the join key;
#: ``stamp`` is the publisher's own stamp, when the pose was true -- difference that, never
#: ``timestamp``. A quaternion, never Euler angles. Twist columns exist and stay empty,
#: because TF carries no velocity and a column only some producers have would make every
#: query producer-specific.
POSE_FIELDNAMES = [
    "frame", "timestamp", "stamp",
    "position.x", "position.y", "position.z",
    "orientation.x", "orientation.y", "orientation.z", "orientation.w",
    "twist.linear.x", "twist.linear.y", "twist.linear.z",
    "twist.angular.x", "twist.angular.y", "twist.angular.z",
]


class TfPoses(Handler):
    """``map``-relative poses of TF frames, resolved through a ``tf2``-exact buffer.

    ``frames`` names the child frames to resolve, or ``all`` for every child frame the
    recording carries. A frame in ``require`` that yields nothing fails the table: the frame
    was not published or is not connected to ``map``, and an empty trajectory must not read
    as a measured one. An explicit ``frames`` list requires itself.

    A frame published once on ``/tf_static`` before the chain to ``map`` exists is resolved
    when it arrives and never again, exactly as a ``tf2`` lookup at that moment would be.
    """

    ALL = "all"
    TABLE = "poses"

    def __init__(self, frames=None, require: Optional[Iterable[str]] = None,
                 table: str = TABLE):
        super().__init__()
        self._all = isinstance(frames, str) and frames.lower() == self.ALL
        self._frames = [] if self._all else list(frames or ["base_link"])
        self._require = list(require) if require else list(self._frames)
        self._table = table
        self._tf = TransformBuffer()
        self._counts: Dict[str, int] = {f: 0 for f in self._frames}
        self._edges: set = set()

    def topics(self) -> List[str]:
        return ["/tf", "/tf_static"]

    def tables(self):
        return [self._table]

    def message(self, topic, msg, typename, log_time):
        static = topic == "/tf_static"
        buf = self._buffer(self._table, fixed(POSE_FIELDNAMES))
        for transform in msg.transforms:
            child = transform.child_frame_id
            t = transform.transform.translation
            r = transform.transform.rotation
            self._tf.set_transform(child, transform.header.frame_id,
                                   _stamp_ns(transform.header.stamp),
                                   (t.x, t.y, t.z), (r.x, r.y, r.z, r.w), static=static)
            self._edges.add(f"{transform.header.frame_id} -> {child}")
            if self._all:
                self._counts.setdefault(child, 0)
            elif child not in self._counts:
                continue
            try:
                (px, py, pz), (qx, qy, qz, qw) = self._tf.lookup(
                    "map", child, _stamp_ns(transform.header.stamp))
            except TransformError:
                continue
            buf.add({
                "frame": child,
                "timestamp": log_time / 1_000_000_000.0,
                "stamp": None if static else _seconds(transform.header.stamp),
                "position.x": px, "position.y": py, "position.z": pz,
                "orientation.x": qx, "orientation.y": qy, "orientation.z": qz,
                "orientation.w": qw,
                "twist.linear.x": None, "twist.linear.y": None, "twist.linear.z": None,
                "twist.angular.x": None, "twist.angular.y": None, "twist.angular.z": None,
            })
            self._counts[child] += 1

    def end(self, recorded):
        missing = [f for f in self._require if not self._counts.get(f)]
        if missing:
            found = "\n".join(f"    - {e}" for e in sorted(self._edges)) or "    (none)"
            got = ", ".join(f"{f}: {c}" for f, c in self._counts.items() if c) or "nothing"
            raise HandlerError(
                f"no map-relative poses for required TF frame(s) {', '.join(missing)} "
                f"(extracted {got}). Either the frame is not published, or it does not "
                f"connect to 'map'. Transforms present in the recording:\n{found}")


# -- a topic's own table --------------------------------------------------------------------

class TopicTable(Handler):
    """One row per message of each topic, its fields as columns.

    Named ``<bag>_<topic>`` (``rosbag2_collision`` for ``/collision`` in ``rosbag2/``).
    ``timestamp`` is the receive time in nanoseconds and ``type`` the message's type name,
    then the fields: nested ones joined with ``.``, a numeric array as one encoded cell.
    """

    def __init__(self, topics: Iterable[str], bag_name: str = "rosbag2"):
        super().__init__()
        self._topics = list(dict.fromkeys(topics))
        self._bag = bag_name

    def topics(self):
        return self._topics

    def tables(self):
        return [self.table_for(t) for t in self._topics]

    def table_for(self, topic: str) -> str:
        return f"{self._bag}_{topic.strip('/').replace('/', '_')}"

    def subset(self, topics: Iterable[str]) -> "TopicTable":
        """A handler for only *topics* of this one's, filling the same tables."""
        return TopicTable(topics, bag_name=self._bag)

    def message(self, topic, msg, typename, log_time):
        row = {"timestamp": log_time, "type": typename.rsplit("/", 1)[-1]}
        row.update(column_values(self.fields_of, msg, typename))
        self._buffer(self.table_for(topic), leading_then_sorted("timestamp", "type")).add(row)


# -- nav2's behaviour tree --------------------------------------------------------------------

class Nav2BtLog(Handler):
    """nav2's ``/behavior_tree_log``, one row per status change.

    ``timestamp`` is the receive time, like every other table: nav2 stamps its events from a
    wall clock even under ``use_sim_time``, so its own stamp is kept as ``event_timestamp``.
    ``uid`` separates two nodes sharing a name (unnamed recovery and rate nodes do).
    """

    TOPIC = "/behavior_tree_log"
    TABLE = "nav2_behavior_tree"
    FIELDNAMES = ["timestamp", "node_name", "uid", "previous_status", "current_status",
                  "event_timestamp"]

    def topics(self):
        return [self.TOPIC]

    def tables(self):
        return [self.TABLE]

    def message(self, topic, msg, typename, log_time):
        buf = self._buffer(self.TABLE, fixed(self.FIELDNAMES))
        for event in msg.event_log:
            stamp = getattr(event, "timestamp", None)
            event_ts = None
            if stamp is not None and (stamp.sec or stamp.nanosec):
                event_ts = _seconds(stamp)
            buf.add({
                "timestamp": log_time / 1_000_000_000.0,
                "node_name": event.node_name,
                "uid": getattr(event, "uid", None),
                "previous_status": str(event.previous_status).upper(),
                "current_status": str(event.current_status).upper(),
                "event_timestamp": event_ts,
            })


# -- actions ----------------------------------------------------------------------------------

class ActionTopics(Handler):
    """An action's feedback and status, flattened: ``action_<name>_feedback``/``_status``."""

    def __init__(self, action: str, prefix: Optional[str] = None):
        super().__init__()
        self._name = action.lstrip("/")
        self._prefix = prefix or f"action_{self._name}"
        self._feedback = f"/{self._name}/_action/feedback"
        self._status = f"/{self._name}/_action/status"

    def topics(self):
        return [self._feedback, self._status]

    def tables(self):
        return [f"{self._prefix}_feedback", f"{self._prefix}_status"]

    def message(self, topic, msg, typename, log_time):
        entry = {"timestamp": log_time / 1_000_000_000.0}
        entry.update(message_to_dict(self.fields_of, msg, typename))
        table = f"{self._prefix}_feedback" if topic == self._feedback else f"{self._prefix}_status"
        self._buffer(table, sorted).add(flatten(entry))


# -- /rosout ------------------------------------------------------------------------------------

LEVEL_NAMES = {10: "DEBUG", 20: "INFO", 30: "WARN", 40: "ERROR", 50: "FATAL"}
LEVEL_BY_NAME = {name: level for level, name in LEVEL_NAMES.items()}


class Rosout(Handler):
    """``/rosout`` at or above a level, one row per message."""

    TOPIC = "/rosout"
    TABLE = "rosout"
    FIELDNAMES = ["timestamp", "stamp", "level", "level_name", "name", "msg", "file",
                  "function", "line"]

    def __init__(self, min_level: int = 10):
        super().__init__()
        self._min_level = min_level

    def topics(self):
        return [self.TOPIC]

    def tables(self):
        return [self.TABLE]

    def message(self, topic, msg, typename, log_time):
        if msg.level < self._min_level:
            return
        self._buffer(self.TABLE, fixed(self.FIELDNAMES)).add({
            "timestamp": log_time / 1_000_000_000.0,
            "stamp": _seconds(msg.stamp),
            "level": msg.level,
            "level_name": LEVEL_NAMES.get(msg.level, str(msg.level)),
            "name": msg.name, "msg": msg.msg, "file": msg.file,
            "function": msg.function, "line": msg.line,
        })

    def end(self, recorded):
        # A recording that carries /rosout has the table, even with nothing at this level.
        if self.TOPIC in recorded:
            self._buffer(self.TABLE, fixed(self.FIELDNAMES))


# -- /clock -------------------------------------------------------------------------------------

class Clock(Handler):
    """The wall -> sim mapping from ``/clock`` in a wall-time recording, decimated.

    Only meaningful for the infrastructure recording (wall receive time, sim content): each
    message is an exact sample of the mapping. See :class:`~robovast_decode.values.ClockDecimator`.
    """

    TOPIC = "/clock"
    TABLE = "clock_map"

    def __init__(self, tolerance_s: float = DEFAULT_CLOCK_TOLERANCE_S):
        super().__init__()
        self._decimator = ClockDecimator(tolerance_s)

    def topics(self):
        return [self.TOPIC]

    def tables(self):
        return [self.TABLE]

    def message(self, topic, msg, typename, log_time):
        clock = getattr(msg, "clock", None)
        if clock is None:
            return
        keep = self._decimator.offer(log_time / 1_000_000_000.0, _seconds(clock))
        if keep is not None:
            self._buffer(self.TABLE, fixed(["wall_ts", "sim_ts"])).add(
                {"wall_ts": keep[0], "sim_ts": keep[1]})

    def end(self, recorded):
        if self.TOPIC not in recorded:
            return
        buf = self._buffer(self.TABLE, fixed(["wall_ts", "sim_ts"]))
        final = self._decimator.close()
        if final is not None:
            buf.add({"wall_ts": final[0], "sim_ts": final[1]})


# -- occupancy grids ----------------------------------------------------------------------------

class Costmaps(Handler):
    """``nav_msgs/OccupancyGrid`` frames: pose metadata beside the cells, compressed.

    The int8 cells (-1..100, row-major) are stored losslessly as zlib-compressed raw bytes,
    base64-encoded; the run view's costmap panel inflates them straight into an ``Int8Array``.
    A ``topic`` column keeps several layers in one table.
    """

    TABLE = "costmaps"
    FIELDNAMES = ["topic", "timestamp", "frame_id", "resolution", "width", "height",
                  "origin_x", "origin_y", "origin_yaw", "data"]

    def __init__(self, topics: Iterable[str]):
        super().__init__()
        self._topics = list(dict.fromkeys(topics))

    def topics(self):
        return self._topics

    def tables(self):
        return [self.TABLE]

    def message(self, topic, msg, typename, log_time):
        info = msg.info
        o = info.origin
        _, _, yaw = quat_to_rpy(o.orientation.x, o.orientation.y, o.orientation.z,
                                o.orientation.w)
        cells = np.asarray(msg.data, dtype=np.int8).tobytes()
        self._buffer(self.TABLE, fixed(self.FIELDNAMES)).add({
            "topic": topic,
            "timestamp": log_time / 1_000_000_000.0,
            "frame_id": msg.header.frame_id,
            "resolution": info.resolution,
            "width": info.width,
            "height": info.height,
            "origin_x": o.position.x,
            "origin_y": o.position.y,
            "origin_yaw": yaw,
            "data": base64.b64encode(zlib.compress(cells, 9)).decode("ascii"),
        })


# -- camera video ------------------------------------------------------------------------------

class Videos(Handler):
    """Each configured ``CompressedImage`` topic encoded to a WebM file beside the recording.

    The file lands in the run directory, named after the recording and the topic, and a row
    in ``videos`` says which topic it holds and what span of the run: the camera panel plays
    it, and a frame is decoded from it on request.

    * **Frames are spooled to disk, not held in memory.** The rate cannot be chosen until the
      last stamp is known, so the frames outlive the read; a 25 fps camera over half an hour
      is gigabytes of JPEG. Only each frame's length and stamp stays resident.
    * **The rate is constant**: ``(n - 1) / duration`` puts the first and last frames at their
      recorded moments.
    * **The keyframe interval is pinned** (``-g``), so a seek decodes from at most two seconds
      back -- seeking is what the panel does.

    Encoding needs ``ffmpeg`` where the table is built; without it the table fails with that
    reason rather than coming out empty. So does a named topic that gave no frame.
    """

    TABLE = "videos"
    FIELDNAMES = ["topic", "file", "t_start", "t_end", "fps", "frames"]
    DEFAULT_FPS = 30.0
    KEYFRAME_SECONDS = 2.0

    def __init__(self, streams: Sequence[Tuple[str, float]]):
        super().__init__()
        self._fps = dict(streams)
        #: Set by the builder: the run directory the files go to, and the recording's name.
        self.output_dir: Optional[str] = None
        self.bag_name = "rosbag2"
        self._spools: Dict[str, tuple] = {}

    def topics(self):
        return list(self._fps)

    def tables(self):
        return [self.TABLE]

    def message(self, topic, msg, typename, log_time):
        spool = self._spools.get(topic)
        if spool is None:
            if self.output_dir is None:
                raise HandlerError("videos: no run directory to write the video into")
            handle = tempfile.NamedTemporaryFile(  # pylint: disable=consider-using-with
                prefix=".webm-frames-", dir=self.output_dir, delete=False)
            spool = self._spools[topic] = (handle, [], [])
        data = bytes(msg.data)
        spool[0].write(data)
        spool[1].append(len(data))
        spool[2].append(log_time)

    def end(self, recorded):
        buf = self._buffer(self.TABLE, fixed(self.FIELDNAMES))
        try:
            # A named topic that gave no frame fails the table, as a required TF frame does:
            # a camera panel would otherwise say "no video" for a topic that was never there.
            missing = sorted(t for t in self._fps if t not in self._spools)
            if missing:
                raise HandlerError(f"videos: no frames on {', '.join(missing)} in this "
                                   "recording; check the topic is recorded")
            if self._spools and shutil.which("ffmpeg") is None:
                raise HandlerError("videos: ffmpeg is not installed where this campaign's "
                                   "tables are built")
            for topic, (handle, sizes, stamps) in sorted(self._spools.items()):
                buf.add(self._encode(topic, handle, sizes, stamps))
        finally:
            for handle, _sizes, _stamps in self._spools.values():
                handle.close()
                with contextlib.suppress(OSError):
                    os.unlink(handle.name)
            self._spools = {}

    def _encode(self, topic, handle, sizes, stamps) -> dict:
        n = len(sizes)
        duration_s = (stamps[-1] - stamps[0]) / 1e9 if n > 1 else 0.0
        fps = (n - 1) / duration_s if duration_s > 0 else self._fps[topic]
        name = f"{self.bag_name}_{re.sub(r'[^a-zA-Z0-9]+', '_', topic).strip('_')}.webm"
        output = os.path.join(self.output_dir, name)
        command = ["ffmpeg", "-y", "-loglevel", "error",
                   "-f", "image2pipe", "-vcodec", "mjpeg", "-r", f"{fps:.6f}", "-i", "pipe:0",
                   "-c:v", "libvpx-vp9", "-crf", "10", "-b:v", "0",
                   "-g", str(max(1, round(fps * self.KEYFRAME_SECONDS))),
                   "-deadline", "realtime", "-cpu-used", "8", "-threads", "1", output]
        with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE) as proc:
            handle.flush()
            handle.seek(0)
            try:
                for size in sizes:
                    proc.stdin.write(handle.read(size))
            except BrokenPipeError:
                pass
            _, stderr = proc.communicate()
        if proc.returncode != 0:
            raise HandlerError(f"videos: ffmpeg failed on {topic}: "
                               f"{stderr.decode(errors='replace').strip()[:500]}")
        # Stamps in seconds, like every other table's, so a moment found in one is directly
        # comparable here and with the run view's playback clock.
        return {"topic": topic, "file": name, "t_start": stamps[0] / 1e9,
                "t_end": stamps[-1] / 1e9, "fps": round(fps, 6), "frames": n}


__all__ = ["ActionTopics", "Clock", "Costmaps", "Handler", "HandlerError", "LEVEL_BY_NAME",
           "Nav2BtLog", "POSE_FIELDNAMES", "Rosout", "TfPoses", "TopicTable", "Videos"]
