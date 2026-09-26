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

"""Read an mcap file record by record, including one that is still being written.

An mcap file is a magic string followed by records, each an opcode byte, a little-endian
``uint64`` length and that many bytes of body. That framing is all a reader needs to walk
a file front to back, and it is what makes a file readable while it grows: every record
before the last complete one is final, and the incomplete tail is simply not read yet.

Walking needs nothing from the summary section at the end of a finished file, which is why
this reader handles a recording that was cut off -- a killed recorder, a segment still open --
as ordinary input: it yields every complete record and stops at the cut. ``offset`` is the
byte after the last complete top-level record, so a caller that stores it can resume exactly
where it stopped. What a file's channels are is the one question the summary answers without
a walk (:func:`summary_channels`).

Chunks are opened and their records yielded in order. A chunk is only yielded once it is
complete, so a chunked file is readable while it grows at chunk granularity, and a file
written without chunking at record granularity.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field
from typing import Iterator, Optional

MAGIC = b"\x89MCAP0\r\n"

OP_HEADER = 0x01
OP_FOOTER = 0x02
OP_SCHEMA = 0x03
OP_CHANNEL = 0x04
OP_MESSAGE = 0x05
OP_CHUNK = 0x06
OP_STATISTICS = 0x0B
OP_METADATA = 0x0C
OP_DATA_END = 0x0F

_READ_SIZE = 4 * 1024 * 1024

#: The size of the footer record, its opcode and length included.
_FOOTER_SIZE = 1 + 8 + 20


@dataclass(frozen=True)
class Schema:
    id: int
    name: str
    encoding: str
    data: bytes


@dataclass(frozen=True)
class Channel:
    id: int
    schema_id: int
    topic: str
    message_encoding: str
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Message:
    channel_id: int
    sequence: int
    log_time: int
    publish_time: int
    data: bytes


@dataclass(frozen=True)
class Metadata:
    name: str
    metadata: dict


class McapFormatError(ValueError):
    """The bytes are not an mcap file, or a complete record does not parse."""


class _Cursor:
    """Sequential reads over one record body."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def u16(self) -> int:
        (value,) = struct.unpack_from("<H", self.data, self.pos)
        self.pos += 2
        return value

    def u32(self) -> int:
        (value,) = struct.unpack_from("<I", self.data, self.pos)
        self.pos += 4
        return value

    def u64(self) -> int:
        (value,) = struct.unpack_from("<Q", self.data, self.pos)
        self.pos += 8
        return value

    def string(self) -> str:
        n = self.u32()
        value = self.data[self.pos:self.pos + n].decode("utf-8")
        self.pos += n
        return value

    def prefixed_bytes(self, width: int) -> bytes:
        n = self.u32() if width == 4 else self.u64()
        value = self.data[self.pos:self.pos + n]
        self.pos += n
        return value

    def string_map(self) -> dict:
        end = self.pos + 4 + struct.unpack_from("<I", self.data, self.pos)[0]
        self.pos += 4
        out = {}
        while self.pos < end:
            key = self.string()
            out[key] = self.string()
        return out

    def rest(self) -> bytes:
        return self.data[self.pos:]


def _decompress(compression: str, data: bytes, size: int) -> bytes:
    if compression == "":
        return data
    if compression == "zstd":
        import zstandard  # pylint: disable=import-outside-toplevel
        return zstandard.ZstdDecompressor().decompress(data, max_output_size=size)
    if compression == "lz4":
        import lz4.frame  # pylint: disable=import-outside-toplevel
        return lz4.frame.decompress(data)
    raise McapFormatError(f"unsupported chunk compression {compression!r}")


def _parse(op: int, body: bytes):
    """One record's body as a record object, or ``None`` for a record nobody reads."""
    cur = _Cursor(body)
    if op == OP_SCHEMA:
        sid = cur.u16()
        name = cur.string()
        encoding = cur.string()
        return Schema(sid, name, encoding, cur.prefixed_bytes(4))
    if op == OP_CHANNEL:
        cid = cur.u16()
        schema_id = cur.u16()
        topic = cur.string()
        encoding = cur.string()
        return Channel(cid, schema_id, topic, encoding, cur.string_map())
    if op == OP_MESSAGE:
        return Message(cur.u16(), cur.u32(), cur.u64(), cur.u64(), cur.rest())
    if op == OP_METADATA:
        name = cur.string()
        return Metadata(name, cur.string_map())
    return None


def _chunk_records(body: bytes) -> Iterator:
    cur = _Cursor(body)
    cur.u64()  # message_start_time
    cur.u64()  # message_end_time
    size = cur.u64()
    cur.u32()  # uncompressed_crc
    compression = cur.string()
    records = _decompress(compression, cur.prefixed_bytes(8), size)
    pos = 0
    while pos + 9 <= len(records):
        op = records[pos]
        (n,) = struct.unpack_from("<Q", records, pos + 1)
        start = pos + 9
        if start + n > len(records):
            raise McapFormatError("a record inside a chunk runs past the chunk's end")
        record = _parse(op, records[start:start + n])
        if record is not None:
            yield record
        pos = start + n


def has_footer(path) -> bool:
    """Whether the file ends with the mcap magic: a writer finished it and wrote its footer.

    A file still being written, or one a killed writer left, ends wherever its last write
    stopped; only ``finish`` puts the closing magic there.
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            if size < 2 * len(MAGIC):
                return False
            fh.seek(size - len(MAGIC))
            return fh.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


def summary_channels(path) -> Optional[tuple]:
    """``(schemas, channels)`` of a finished file, from its summary section; ``None`` when the
    file has no footer, no summary, or a summary that does not say it holds them all.

    A writer may repeat every schema and channel in the summary, and its Statistics record
    counts both. The answer is taken only when the counts match, so a summary that repeats
    some or none of them is never mistaken for the file's channels: the caller walks it.
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            if size < len(MAGIC) + _FOOTER_SIZE + len(MAGIC):
                return None
            fh.seek(size - _FOOTER_SIZE - len(MAGIC))
            tail = fh.read(_FOOTER_SIZE + len(MAGIC))
            if tail[-len(MAGIC):] != MAGIC or tail[0] != OP_FOOTER:
                return None
            summary_start, offsets_start = struct.unpack_from("<QQ", tail, 9)
            end = offsets_start or size - _FOOTER_SIZE - len(MAGIC)
            if not len(MAGIC) <= summary_start < end:
                return None
            fh.seek(summary_start)
            section = fh.read(end - summary_start)
    except OSError:
        return None
    schemas, channels, counts = {}, {}, None
    pos = 0
    while pos + 9 <= len(section):
        op = section[pos]
        (n,) = struct.unpack_from("<Q", section, pos + 1)
        body = section[pos + 9:pos + 9 + n]
        if len(body) < n:
            return None
        if op == OP_STATISTICS:
            if n < 14:
                return None
            _, schema_count, channel_count = struct.unpack_from("<QHI", body)
            counts = (schema_count, channel_count)
        else:
            record = _parse(op, body) if op in (OP_SCHEMA, OP_CHANNEL) else None
            if isinstance(record, Schema):
                schemas[record.id] = record
            elif isinstance(record, Channel):
                channels[record.id] = record
        pos += 9 + n
    if counts != (len(schemas), len(channels)):
        return None
    return schemas, channels


class McapTail:
    """The records of one mcap file, read from where the last read stopped.

    *offset* is where to resume: ``0`` for the start of the file, or a value a previous
    :attr:`offset` returned. Schemas and channels are records like any other, so a reader
    that resumes mid-file must be handed the ones it already saw (*schemas*, *channels*);
    :meth:`read` keeps both maps current.
    """

    def __init__(self, path, offset: int = 0, schemas: Optional[dict] = None,
                 channels: Optional[dict] = None):
        self.path = path
        self.offset = offset
        self.schemas: dict = dict(schemas or {})
        self.channels: dict = dict(channels or {})
        #: Whether the footer has been read: the file is finished and will not grow.
        self.finished = False

    def read(self) -> Iterator:
        """Yield every complete record after :attr:`offset`, advancing it as they are read.

        Schemas and channels are yielded too, after being recorded, so a caller can react
        to a topic appearing. Returns when the file has no further complete record.
        """
        with open(self.path, "rb") as fh:
            if self.offset == 0:
                magic = fh.read(len(MAGIC))
                if len(magic) < len(MAGIC):
                    return
                if magic != MAGIC:
                    raise McapFormatError(f"{self.path} is not an mcap file")
                self.offset = len(MAGIC)
            fh.seek(self.offset)
            pending = b""
            base = self.offset
            while True:
                block = fh.read(_READ_SIZE)
                if not block:
                    return
                buf = pending + block
                pos = 0
                while len(buf) - pos >= 9:
                    op = buf[pos]
                    (n,) = struct.unpack_from("<Q", buf, pos + 1)
                    end = pos + 9 + n
                    if end > len(buf):
                        if n > _READ_SIZE:
                            # A record larger than one read: fetch the rest directly, or
                            # stop at the cut if the file does not hold it yet.
                            rest = fh.read(end - len(buf))
                            if len(rest) < end - len(buf):
                                return
                            buf = buf + rest
                        else:
                            break
                    body = buf[pos + 9:end]
                    yield from self._records(op, body)
                    pos = end
                    self.offset = base + pos
                    if op == OP_FOOTER:
                        self.finished = True
                        return
                pending = buf[pos:]
                base += pos

    def _records(self, op: int, body: bytes) -> Iterator:
        if op == OP_CHUNK:
            records = _chunk_records(body)
        else:
            record = _parse(op, body)
            records = iter(()) if record is None else iter((record,))
        for record in records:
            if isinstance(record, Schema):
                self.schemas[record.id] = record
            elif isinstance(record, Channel):
                self.channels[record.id] = record
            yield record


__all__ = ["Channel", "McapFormatError", "McapTail", "Message", "Metadata", "Schema",
           "has_footer", "summary_channels"]
