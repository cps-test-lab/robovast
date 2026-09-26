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

"""The messages of one bulk topic, in recording order, one at a time.

A bulk topic (:data:`~robovast_decode.registry.BULK_TYPES`) is read from the recording
where it is wanted: :func:`iter_messages` walks a recording's segments once, front to back,
and yields each message of the topic deserialized, so a loop over a run's camera frames
costs one pass over the file and holds one message at a time. A message outside the asked
span, or one thinned out by *every*, is skipped before it is deserialized, which is where
the time would go.

The stamp is the message's ``log_time``, the receive time every table decoded from the same
recording carries as ``timestamp`` (in seconds in ``poses``, in nanoseconds in a topic's own
table), so a moment found in a table names a message here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator, Optional

from .decode import channel_type, segments
from .definitions import TypeCatalog
from .framing import Channel, McapTail, Message, Schema


@dataclass(frozen=True)
class Sample:
    """One message of a bulk topic: when it was received, what type it is, and the message."""
    t_ns: int
    typename: str
    msg: Any

    @property
    def t(self) -> float:
        """The stamp in seconds, the clock ``poses`` and the frame route use."""
        return self.t_ns / 1e9


def iter_messages(bag_dir: str, topic: str, start: Optional[float] = None,
                  end: Optional[float] = None, every: Optional[float] = None
                  ) -> Iterator[Sample]:
    """Every message of *topic* in *bag_dir*, in recording order, deserialized.

    *start* and *end* bound the stamps kept, in seconds of the recording's clock; *every*
    keeps one message per that many seconds (the first at or after each step). A recording
    that never carried *topic* yields nothing; a type that cannot be decoded raises
    ``ValueError`` naming it at its first message.
    """
    catalog = TypeCatalog()
    catalog.add_sidecar(bag_dir)
    next_keep = None if every is None else (start or float("-inf"))
    for path in segments(bag_dir):
        tail = McapTail(path)
        channel_id = None
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
            if not isinstance(record, Message) or record.channel_id != channel_id:
                continue
            t = record.log_time / 1e9
            if start is not None and t < start:
                continue
            if end is not None and t > end:
                return
            if next_keep is not None:
                if t < next_keep:
                    continue
                next_keep = max(next_keep, t) + every
            channel = tail.channels[channel_id]
            typename = channel_type(channel, tail.schemas)
            if not catalog.ensure(typename, channel.message_encoding):
                raise ValueError(f"{topic} carries {typename}, which cannot be decoded: "
                                 f"{catalog.missing([typename])[typename]}")
            yield Sample(record.log_time, typename,
                         catalog.deserialize(record.data, typename, channel.message_encoding))


def nearest_message(bag_dir: str, topic: str, t: Optional[float] = None) -> Optional[Sample]:
    """The message of *topic* at or before *t* (the first when none is; the last for
    ``None``), deserialized; ``None`` when the recording has none of the topic.

    One walk over the headers, one deserialization: what a viewer's "the frame at this
    moment" costs, which is why it does not go through :func:`iter_messages`.
    """
    catalog = TypeCatalog()
    catalog.add_sidecar(bag_dir)
    chosen = None                                   # (log_time, path, channel, data)
    for path in segments(bag_dir):
        tail = McapTail(path)
        channel_id = None
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
            if not isinstance(record, Message) or record.channel_id != channel_id:
                continue
            if t is not None and record.log_time / 1e9 > t:
                if chosen is None:
                    chosen = (record.log_time, tail.channels[channel_id], tail.schemas, record.data)
                break
            chosen = (record.log_time, tail.channels[channel_id], tail.schemas, record.data)
        else:
            continue
        break
    if chosen is None:
        return None
    log_time, channel, schemas, data = chosen
    typename = channel_type(channel, schemas)
    if not catalog.ensure(typename, channel.message_encoding):
        raise ValueError(f"{topic} carries {typename}, which cannot be decoded: "
                         f"{catalog.missing([typename])[typename]}")
    return Sample(log_time, typename, catalog.deserialize(data, typename, channel.message_encoding))


__all__ = ["Sample", "iter_messages", "nearest_message"]
