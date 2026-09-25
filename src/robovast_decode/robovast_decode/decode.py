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

"""One pass over one recording, feeding every handler that asked for its topics.

A recording is a bag directory: one or more ``*.mcap`` segments, read in order, and possibly
a definitions sidecar. Every message is framed (cheap: the counts and bytes per topic come
from this), and only a message whose topic some handler reads is deserialized -- once, however
many handlers read it.

A segment that is still being written, or that a killed recorder left without its summary, is
read up to its last complete record; nothing about it is an error.

A metadata record (a named string map, which roqsim's recording uses for its provenance and
its entity roster) goes to every handler's :meth:`Handler.metadata` as it is met.
"""

from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List

from .definitions import SIDECAR_NAME, TypeCatalog
from .framing import Channel, McapTail, Message, Metadata, Schema
from .handlers import Handler

_SEGMENT_INDEX = re.compile(r"_(\d+)\.mcap$")


def segments(bag_dir: str) -> List[str]:
    """The ``*.mcap`` files of *bag_dir*, in recording order (``_0``, ``_1``, ...)."""
    files = glob.glob(os.path.join(bag_dir, "*.mcap"))

    def key(path):
        match = _SEGMENT_INDEX.search(os.path.basename(path))
        return (int(match.group(1)) if match else -1, os.path.basename(path))
    return sorted(files, key=key)


def channel_type(channel: Channel, schemas: Dict[int, Schema]) -> str:
    """The type name a channel's messages are reported under.

    A channel with a schema is typed by the schema's name; one without (roqsim's ``state``
    channel) by its message encoding, which is the only thing that says what it holds.
    """
    schema = schemas.get(channel.schema_id)
    return schema.name if schema else channel.message_encoding


@dataclass
class TopicStats:
    type: str = ""
    messages: int = 0
    bytes: int = 0
    undecodable: str = ""


@dataclass
class BagReport:
    """What one pass read: per-topic counts, and the handlers that failed and why."""
    bag_dir: str
    topics: Dict[str, TopicStats] = field(default_factory=dict)
    failed: Dict[str, str] = field(default_factory=dict)
    segments: List[str] = field(default_factory=list)
    bytes_read: Dict[str, int] = field(default_factory=dict)


def decode_bag(bag_dir: str, handlers: Iterable[Handler]) -> BagReport:
    """Read every segment of *bag_dir* once and feed *handlers*; return what was read.

    A handler that raises is dropped for the rest of the pass and named in
    ``report.failed``; the others carry on. :class:`HandlerError` from ``end`` is how a
    handler says its table would not be truthful (a required TF frame that never resolved).
    """
    handlers = list(handlers)
    report = BagReport(bag_dir)
    catalog = TypeCatalog()
    catalog.add_sidecar(bag_dir)
    for handler in handlers:
        handler.fields_of = catalog.fields
    wanted: Dict[str, List[Handler]] = {}
    for handler in handlers:
        for topic in handler.topics():
            wanted.setdefault(topic, []).append(handler)

    report.segments = segments(bag_dir)
    recorded: Dict[str, str] = {}
    active = list(handlers)

    def fail(handler, exc):
        if handler in active:
            active.remove(handler)
            report.failed[type(handler).__name__] = f"{type(exc).__name__}: {exc}"
            for topic_handlers in wanted.values():
                if handler in topic_handlers:
                    topic_handlers.remove(handler)

    for path in report.segments:
        # Schema and channel ids are per file: each segment is read with fresh maps, and
        # rosbag2 writes a segment's own schema and channel records into it.
        tail = McapTail(path)
        for record in tail.read():
            if isinstance(record, Schema):
                if record.encoding in ("ros2msg", "ros2idl") and record.data:
                    catalog.add_definition(record.name, record.encoding,
                                           record.data.decode("utf-8", errors="replace"))
                continue
            if isinstance(record, Channel):
                typename = channel_type(record, tail.schemas)
                recorded.setdefault(record.topic, typename)
                report.topics.setdefault(record.topic, TopicStats(type=typename))
                continue
            if isinstance(record, Metadata):
                for handler in list(active):
                    try:
                        handler.metadata(record.name, record.metadata)
                    except Exception as exc:  # noqa: BLE001
                        fail(handler, exc)
                continue
            if not isinstance(record, Message):
                continue
            channel = tail.channels.get(record.channel_id)
            if channel is None:
                continue
            stats = report.topics[channel.topic]
            stats.messages += 1
            stats.bytes += len(record.data)
            readers = wanted.get(channel.topic)
            if not readers or stats.undecodable:
                continue
            encoding = channel.message_encoding
            if not catalog.ensure(stats.type, encoding):
                stats.undecodable = catalog.missing([stats.type])[stats.type]
                continue
            try:
                msg = catalog.deserialize(record.data, stats.type, encoding)
            except Exception as exc:  # noqa: BLE001 - a message that does not match its schema
                stats.undecodable = f"a message does not decode as {stats.type}: {exc}"
                continue
            for handler in list(readers):
                try:
                    handler.message(channel.topic, msg, stats.type, record.log_time)
                except Exception as exc:  # noqa: BLE001
                    fail(handler, exc)
        report.bytes_read[path] = tail.offset

    for handler in list(active):
        try:
            handler.end(dict(recorded))
        except Exception as exc:  # noqa: BLE001 - HandlerError, or a handler's own bug
            fail(handler, exc)
    return report


__all__ = ["BagReport", "SIDECAR_NAME", "TopicStats", "channel_type", "decode_bag", "segments"]
