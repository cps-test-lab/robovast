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
where it is wanted: :func:`iter_messages` goes through a recording's segments once, front to
back, and yields each message of the topic deserialized, so a loop over a run's camera frames
holds one message at a time. A message outside the asked span, or one thinned out by *every*,
is skipped before it is deserialized, which is where the time would go.

How a segment is gone through depends on what it is. A closed segment whose chunks carry
message indexes -- every packed one does (:mod:`robovast_decode.pack`) -- is read through
them: they say when and where each message of the topic lies, and only the chunks holding a
message that is kept are decompressed. Any other segment -- one still being written, one
written without message indexes -- is walked record by record.

The stamp is the message's ``log_time``, the receive time every table decoded from the same
recording carries as ``timestamp`` (in seconds in ``poses``, in nanoseconds in a topic's own
table), so a moment found in a table names a message here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, Optional, Tuple

from .decode import channel_type, segments
from .definitions import TypeCatalog
from .framing import Channel, IndexedFile, McapTail, Message, Schema


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


#: One message of a topic as a segment holds it: ``(log_time, channel, schemas, data)``,
#: *data* reading the message's bytes when called.
_Located = Tuple[int, Channel, Dict[int, Schema], Callable[[], bytes]]


def _topic_messages(path: str, topic: str, start_ns: Optional[int] = None
                    ) -> Iterator[_Located]:
    """Every message of *topic* in the segment *path*, in file order, its bytes unread.

    Through the message indexes for a segment that has them in every chunk, a chunk whose
    messages all end before *start_ns* not looked into; walked otherwise.
    """
    indexed = IndexedFile.open(path)
    if indexed is not None:
        channel = indexed.channel(topic)
        if channel is None:
            return
        schemas = indexed.summary.schemas
        for entry in indexed.entries(channel.id, start_ns):
            yield (entry.log_time, channel, schemas,
                   lambda entry=entry: indexed.message(entry).data)
        return
    tail = McapTail(path)
    channel_id = None
    for record in tail.read():
        if isinstance(record, Channel):
            if record.topic == topic:
                channel_id = record.id
        elif isinstance(record, Message) and channel_id is not None \
                and record.channel_id == channel_id:
            yield (record.log_time, tail.channels[channel_id], tail.schemas,
                   lambda data=record.data: data)


class _Catalog(TypeCatalog):
    """A catalog fed every schema a segment holds before a message of it is deserialized:
    a schema embeds the definitions its type depends on, which can be the only definition
    of a type another channel names."""

    def __init__(self, bag_dir: str):
        super().__init__()
        self.add_sidecar(bag_dir)
        # Keyed by the segment's schema map, which is held here so its id stays its own.
        self._fed: Dict[int, Tuple[dict, set]] = {}

    def feed(self, schemas: Dict[int, Schema]) -> None:
        _, fed = self._fed.setdefault(id(schemas), (schemas, set()))
        if len(fed) == len(schemas):
            return
        for schema_id, schema in list(schemas.items()):
            if schema_id not in fed:
                fed.add(schema_id)
                self.add_schema(schema)


def _sample(catalog: _Catalog, topic: str, located: _Located) -> Sample:
    log_time, channel, schemas, data = located
    catalog.feed(schemas)
    typename = channel_type(channel, schemas)
    if not catalog.ensure(typename, channel.message_encoding):
        raise ValueError(f"{topic} carries {typename}, which cannot be decoded: "
                         f"{catalog.missing([typename])[typename]}")
    return Sample(log_time, typename,
                  catalog.deserialize(data(), typename, channel.message_encoding))


def iter_messages(bag_dir: str, topic: str, start: Optional[float] = None,
                  end: Optional[float] = None, every: Optional[float] = None
                  ) -> Iterator[Sample]:
    """Every message of *topic* in *bag_dir*, in recording order, deserialized.

    *start* and *end* bound the stamps kept, in seconds of the recording's clock; *every*
    keeps one message per that many seconds (the first at or after each step). A recording
    that never carried *topic* yields nothing; a type that cannot be decoded raises
    ``ValueError`` naming it at its first message.
    """
    catalog = _Catalog(bag_dir)
    next_keep = None if every is None else (start or float("-inf"))
    start_ns = None if start is None else int(start * 1e9)
    for path in segments(bag_dir):
        for located in _topic_messages(path, topic, start_ns):
            t = located[0] / 1e9
            if start is not None and t < start:
                continue
            if end is not None and t > end:
                return
            if next_keep is not None:
                if t < next_keep:
                    continue
                next_keep = max(next_keep, t) + every
            yield _sample(catalog, topic, located)


def nearest_message(bag_dir: str, topic: str, t: Optional[float] = None,
                    after: bool = False) -> Optional[Sample]:
    """The message of *topic* at or before *t* (the first when none is; the last for
    ``None``), deserialized; ``None`` when the recording has none of the topic. With
    *after*, the first message strictly after *t* instead, and ``None`` when there is none:
    how a reader steps through a topic one message at a time.

    The messages' stamps are gone through without reading them -- from the message indexes
    where a segment has them, which decompresses nothing -- and the one message chosen is
    read and deserialized: what a viewer's "the message at this moment" costs, which is why it
    does not go through :func:`iter_messages`.
    """
    catalog = _Catalog(bag_dir)
    chosen: Optional[_Located] = None
    for path in segments(bag_dir):
        for located in _topic_messages(path, topic):
            stamp = located[0] / 1e9
            if after:
                if t is None or stamp > t:
                    chosen = located
                    break
                continue
            if t is not None and stamp > t:
                if chosen is None:
                    chosen = located
                break
            chosen = located
        else:
            continue
        break
    if chosen is None:
        return None
    return _sample(catalog, topic, chosen)


__all__ = ["Sample", "iter_messages", "nearest_message"]
