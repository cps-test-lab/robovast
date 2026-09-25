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

"""Camera frames of a recording, found by stamp and read one at a time.

An image topic is bulk data (:data:`~robovast_decode.registry.BULK_TYPES`): its messages
never become rows. What a viewer needs of it is one frame at a moment, so a
:class:`FrameIndex` walks a recording's message *headers* -- no message is deserialized --
and keeps, per message of the topic, its stamp and where it lies in which segment. One
frame is then read from that place alone (:meth:`FrameIndex.read_frame`), as JPEG no
wider than :data:`MAX_WIDTH`.

The stamp is the message's ``log_time`` in seconds, the same time base as the ``timestamp``
of every table decoded from the recording, so a moment found in a table is directly a
frame here. An index is extended from where its last walk stopped, so the index of a
recording still being written follows its open segment.

A :class:`FrameTap` is the same index fed by a :class:`~robovast_decode.live.Session` as
it advances, rather than by walking the recording itself, and it keeps the newest message's
bytes so a viewer of a live run gets the current frame without a read.
"""

from __future__ import annotations

import io
import os
import threading
from bisect import bisect_right
from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional, Tuple

import numpy as np
from PIL import Image as PILImage

from .decode import channel_type, segments
from .definitions import TypeCatalog
from .framing import Channel, McapTail, Message, Schema

#: The message types a frame can be read from.
IMAGE_TYPES = frozenset({"sensor_msgs/msg/CompressedImage", "sensor_msgs/msg/Image"})

#: Widest a frame is served: a wider one is downscaled, keeping its aspect ratio.
MAX_WIDTH = 640

#: JPEG quality of a frame that has to be encoded.
JPEG_QUALITY = 85

#: ``sensor_msgs/msg/Image`` encodings this renders, as Pillow's raw modes.
_RAW_MODES = {
    "rgb8": ("RGB", "RGB"), "bgr8": ("RGB", "BGR"), "rgba8": ("RGBA", "RGBA"),
    "bgra8": ("RGBA", "BGRA"), "mono8": ("L", "L"), "8UC1": ("L", "L"), "8UC3": ("RGB", "BGR"),
}

#: 16-bit and float single-channel encodings (depth cameras): scaled to 8 bits by their range.
_DEPTH_DTYPES = {"mono16": "u2", "16UC1": "u2", "32FC1": "f4"}


@dataclass(frozen=True)
class FrameRef:
    """Where one message of an image topic lies: its stamp, and its place in a segment.

    *offset* is the start of the top-level mcap record that holds the message, *index*
    which of that record's messages it is: a message written outside a chunk is the only
    one of its record, one inside a chunk is the *index*-th message the chunk holds.
    """
    t: float
    segment: str
    offset: int
    index: int = 0


class Frames:
    """The stamps and places of every message of one image topic, in recording order.

    The base of :class:`FrameIndex` (which fills itself from the recording) and
    :class:`FrameTap` (filled by a session): what is common is finding a frame by stamp
    and reading it.
    """

    def __init__(self, bag_dir: str, topic: str):
        self.bag_dir = os.path.abspath(bag_dir)
        self.topic = topic
        #: Every message of the topic seen so far, in recording order.
        self.entries: List[FrameRef] = []
        #: The topic's message type and encoding, once its channel has been seen.
        self.typename: Optional[str] = None
        self.encoding: Optional[str] = None
        self.catalog = TypeCatalog()
        self.catalog.add_sidecar(self.bag_dir)
        self._times: List[float] = []

    def __len__(self) -> int:
        return len(self.entries)

    @property
    def times(self) -> List[float]:
        """The stamps of :attr:`entries`, in seconds."""
        return list(self._times)

    def _add(self, ref: FrameRef) -> None:
        self.entries.append(ref)
        self._times.append(ref.t)

    def nearest(self, t: Optional[float] = None) -> Optional[FrameRef]:
        """The last frame at or before *t*, the first when none is; the newest for ``None``.

        ``None`` when the topic has no frame yet.
        """
        if not self.entries:
            return None
        if t is None:
            return self.entries[-1]
        i = bisect_right(self._times, t)
        return self.entries[i - 1] if i > 0 else self.entries[0]

    def read_frame(self, ref: FrameRef) -> bytes:
        """The JPEG of the frame at *ref*, no wider than :data:`MAX_WIDTH`.

        The one message is read from its record and deserialized; nothing else of the
        recording is touched.
        """
        return to_jpeg(self._deserialize(self._message_data(ref)), self.typename)

    def newest_frame(self) -> Optional[Tuple[float, bytes]]:
        """``(stamp, JPEG)`` of the newest frame; ``None`` before the first."""
        ref = self.nearest()
        return None if ref is None else (ref.t, self.read_frame(ref))

    def _message_data(self, ref: FrameRef) -> bytes:
        tail = McapTail(ref.segment, ref.offset)
        records = tail.read()
        index = 0
        try:
            for record in records:
                if tail.offset != ref.offset:
                    break
                if not isinstance(record, Message):
                    continue
                if index == ref.index:
                    return record.data
                index += 1
        finally:
            records.close()
        raise KeyError(f"{ref.segment} holds no message {ref.index} at offset {ref.offset}: "
                       "the recording changed under the index")

    def _deserialize(self, data: bytes):
        if self.typename is None:
            raise KeyError(f"{self.topic} has not been seen in {self.bag_dir}")
        if self.typename not in IMAGE_TYPES:
            raise ValueError(f"{self.topic} carries {self.typename}, not an image type")
        if not self.catalog.ensure(self.typename, self.encoding):
            raise ValueError(f"{self.typename} cannot be decoded: "
                             f"{self.catalog.missing([self.typename])[self.typename]}")
        return self.catalog.deserialize(data, self.typename, self.encoding)


class FrameIndex(Frames):
    """The frames of one image topic of one recording, built by walking its segments.

    :meth:`extend` walks every segment from where the last walk stopped and adds the
    messages it finds: called again on a recording being written, the index follows it. The
    walk reads message headers only; a frame is deserialized by :meth:`read_frame` alone.
    """

    def __init__(self, bag_dir: str, topic: str):
        super().__init__(bag_dir, topic)
        self._tails: Dict[str, McapTail] = {}
        self.extend()

    def extend(self) -> int:
        """Add what the recording gained since the last walk; how many frames that was."""
        added = 0
        for path in segments(self.bag_dir):
            tail = self._tails.get(path)
            if tail is None:
                # Schema and channel ids are per file: every segment has its own maps.
                tail = self._tails[path] = McapTail(path)
            if tail.finished:
                continue
            for ref, typename, encoding in _walk(tail, self.topic, self.catalog):
                if self.typename is None:
                    self.typename, self.encoding = typename, encoding
                self._add(ref)
                added += 1
        return added


class FrameTap(Frames):
    """A :class:`Frames` a session fills as it reads, keeping the newest message's bytes.

    :meth:`record` is called by the session for every message of the topic, with the
    message's place and its raw bytes; nothing is deserialized until :meth:`newest_frame`
    asks, whose JPEG is kept until a newer message arrives.
    """

    def __init__(self, bag_dir: str, topic: str):
        super().__init__(bag_dir, topic)
        self._lock = threading.Lock()
        self._newest: Optional[Tuple[FrameRef, bytes]] = None
        self._newest_jpeg: Optional[Tuple[FrameRef, bytes]] = None

    def record(self, ref: FrameRef, typename: str, encoding: str, data: bytes) -> None:
        """One message of the topic, where it lies and its bytes."""
        with self._lock:
            if self.typename is None:
                self.typename, self.encoding = typename, encoding
            self._add(ref)
            self._newest = (ref, data)

    def newest_frame(self) -> Optional[Tuple[float, bytes]]:
        """``(stamp, JPEG)`` of the newest message; ``None`` before the first."""
        with self._lock:
            newest = self._newest
            cached = self._newest_jpeg
        if newest is None:
            return None
        ref, data = newest
        if cached is not None and cached[0] == ref:
            return ref.t, cached[1]
        jpeg = to_jpeg(self._deserialize(data), self.typename)
        with self._lock:
            self._newest_jpeg = (ref, jpeg)
        return ref.t, jpeg


def _walk(tail: McapTail, topic: str, catalog: TypeCatalog) -> Iterator[Tuple[FrameRef, str, str]]:
    """Every message of *topic* the tail yields, with its place and the channel's type.

    ``tail.offset`` is the start of the top-level record whose records are being yielded
    (the tail advances it once the record is done), which is what a :class:`FrameRef`
    stores with the message's ordinal in that record.
    """
    # The tail keeps the channels it has read, so a walk resumed past the channel record
    # still knows the topic's id.
    channel_id = next((cid for cid, ch in tail.channels.items() if ch.topic == topic), None)
    record_offset, index = -1, 0
    for record in tail.read():
        if isinstance(record, Schema):
            if record.encoding in ("ros2msg", "ros2idl") and record.data:
                catalog.add_definition(record.name, record.encoding,
                                       record.data.decode("utf-8", errors="replace"))
            continue
        if isinstance(record, Channel):
            if record.topic == topic:
                channel_id = record.id
            continue
        if not isinstance(record, Message):
            continue
        if tail.offset != record_offset:
            record_offset, index = tail.offset, 0
        ordinal, index = index, index + 1
        if channel_id is None or record.channel_id != channel_id:
            continue
        channel = tail.channels[channel_id]
        yield (FrameRef(record.log_time / 1e9, tail.path, record_offset, ordinal),
               channel_type(channel, tail.schemas), channel.message_encoding)


# -- one message to a JPEG ----------------------------------------------------------------

def to_jpeg(msg, typename: str) -> bytes:
    """The JPEG of one image message, no wider than :data:`MAX_WIDTH`.

    A ``CompressedImage`` whose ``format`` says JPEG and that is narrow enough is passed
    through as recorded; any other compressed format, a wider JPEG, and every raw ``Image``
    is decoded and encoded. A raw encoding this cannot render is an error, not a blank
    frame.
    """
    if typename == "sensor_msgs/msg/CompressedImage":
        data = bytes(msg.data)
        fmt = (msg.format or "").lower()
        image = PILImage.open(io.BytesIO(data))
        if ("jpeg" in fmt or "jpg" in fmt) and image.format == "JPEG" and image.width <= MAX_WIDTH:
            return data
    elif typename == "sensor_msgs/msg/Image":
        image = _raw_image(msg)
    else:
        raise ValueError(f"{typename} is not an image type")
    return _encode(image)


def _raw_image(msg) -> PILImage.Image:
    encoding = msg.encoding
    width, height, step = int(msg.width), int(msg.height), int(msg.step)
    data = bytes(msg.data)
    if encoding in _RAW_MODES:
        mode, raw_mode = _RAW_MODES[encoding]
        return PILImage.frombuffer(mode, (width, height), data, "raw", raw_mode, step, 1)
    if encoding in _DEPTH_DTYPES:
        dtype = np.dtype((">" if msg.is_bigendian else "<") + _DEPTH_DTYPES[encoding])
        row = step // dtype.itemsize
        values = np.frombuffer(data, dtype=dtype)[:height * row].reshape(height, row)[:, :width]
        values = values.astype(np.float64)
        finite = np.isfinite(values)
        top = float(values[finite].max()) if finite.any() else 0.0
        scaled = np.where(finite, values / top if top > 0 else 0.0, 0.0)
        return PILImage.fromarray((scaled * 255).astype(np.uint8), "L")
    raise ValueError(f"cannot render a sensor_msgs/msg/Image in encoding {encoding!r}")


def _encode(image: PILImage.Image) -> bytes:
    if image.width > MAX_WIDTH:
        height = max(1, round(image.height * MAX_WIDTH / image.width))
        image = image.resize((MAX_WIDTH, height), PILImage.Resampling.BILINEAR)
    if image.mode != "RGB":
        image = image.convert("RGB")
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=JPEG_QUALITY)
    return out.getvalue()


__all__ = ["FrameIndex", "FrameRef", "FrameTap", "Frames", "IMAGE_TYPES", "MAX_WIDTH", "to_jpeg"]
