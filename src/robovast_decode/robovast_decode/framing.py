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

A closed file whose chunks carry message indexes is read the other way round
(:class:`IndexedFile`): its summary says where every chunk is and which channels and times it
holds, a chunk's message index says where each message of a channel lies inside it, and a
reader that wants a few messages decompresses only the chunks that hold them. A file that ends
with a footer naming a summary that does not parse is an error (:class:`McapFormatError`),
never a reason to walk it instead.
"""

from __future__ import annotations

import os
import struct
import zlib
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Tuple

MAGIC = b"\x89MCAP0\r\n"

OP_HEADER = 0x01
OP_FOOTER = 0x02
OP_SCHEMA = 0x03
OP_CHANNEL = 0x04
OP_MESSAGE = 0x05
OP_CHUNK = 0x06
OP_MESSAGE_INDEX = 0x07
OP_CHUNK_INDEX = 0x08
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


def chunk_payload(body: bytes) -> bytes:
    """The records a chunk record's *body* holds, decompressed."""
    cur = _Cursor(body)
    cur.u64()  # message_start_time
    cur.u64()  # message_end_time
    size = cur.u64()
    cur.u32()  # uncompressed_crc
    compression = cur.string()
    records = _decompress(compression, cur.prefixed_bytes(8), size)
    if len(records) != size:
        raise McapFormatError(f"a chunk decompresses to {len(records)} bytes, its header says "
                              f"{size}")
    return records


def _chunk_records(body: bytes) -> Iterator:
    return _iter_records(chunk_payload(body))


def _iter_records(records: bytes) -> Iterator:
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


def read_record(fh, offset: int) -> Tuple[int, bytes]:
    """``(opcode, body)`` of the top-level record that starts at *offset* of *fh*."""
    fh.seek(offset)
    head = fh.read(9)
    if len(head) < 9:
        raise McapFormatError(f"no record at offset {offset}: the file ends there")
    op = head[0]
    (n,) = struct.unpack_from("<Q", head, 1)
    body = fh.read(n)
    if len(body) < n:
        raise McapFormatError(f"the record at offset {offset} runs past the end of the file")
    return op, body


def parse_message(body: bytes) -> Message:
    """The message a message record's *body* holds."""
    return _parse(OP_MESSAGE, body)


def chunk_messages(records: bytes) -> Iterator[Message]:
    """The messages of a chunk's decompressed *records*, in order."""
    for record in _iter_records(records):
        if isinstance(record, Message):
            yield record


def message_at(records: bytes, inner: int) -> Message:
    """The message record at offset *inner* of a chunk's decompressed *records*."""
    if inner + 9 > len(records) or records[inner] != OP_MESSAGE:
        raise McapFormatError(f"no message record at offset {inner} of the chunk")
    (n,) = struct.unpack_from("<Q", records, inner + 1)
    if inner + 9 + n > len(records):
        raise McapFormatError(f"the message at offset {inner} runs past the chunk's end")
    return _parse(OP_MESSAGE, records[inner + 9:inner + 9 + n])


def file_identity(st: os.stat_result) -> Tuple[int, int]:
    """What names one file across renames: a file replaced in place has a new identity, one
    that grows keeps its own."""
    return st.st_dev, st.st_ino


@dataclass(frozen=True)
class ChunkIndex:
    """Where one chunk lies, the times its messages span, and where its message indexes are."""
    message_start_time: int
    message_end_time: int
    offset: int
    length: int
    #: ``{channel id: offset of that channel's message index record}``; empty for a chunk
    #: written without message indexes.
    message_index_offsets: Dict[int, int]
    message_index_length: int
    compression: str
    compressed_size: int
    uncompressed_size: int


@dataclass(frozen=True)
class Summary:
    """What a closed file's summary section says about it."""
    schemas: Dict[int, Schema]
    channels: Dict[int, Channel]
    #: Every chunk's index, in file order.
    chunks: Tuple[ChunkIndex, ...]
    #: The Statistics record's message count; ``None`` when the summary has none.
    message_count: Optional[int]

    @property
    def packed(self) -> bool:
        """Whether every message is in a zstd chunk the summary indexes, or there is none."""
        if self.message_count == 0:
            return True
        return bool(self.chunks) and all(c.compression == "zstd" for c in self.chunks)

    @property
    def message_indexed(self) -> bool:
        """Whether every chunk says where each of its messages lies, per channel."""
        return bool(self.chunks) and all(c.message_index_offsets for c in self.chunks)


def read_summary(path) -> Optional[Summary]:
    """The summary of the closed file *path*; ``None`` while it has no footer (still being
    written, or cut off) and for one whose footer says it has no summary.

    A footer that names a summary which does not parse, or whose checksum does not match,
    raises :class:`McapFormatError`: the file says it is indexed and its index is broken.
    """
    with open(path, "rb") as fh:
        return _read_summary(fh, path)


def _read_summary(fh, path) -> Optional[Summary]:
    size = os.fstat(fh.fileno()).st_size
    if size < len(MAGIC) + _FOOTER_SIZE + len(MAGIC):
        return None
    footer_start = size - _FOOTER_SIZE - len(MAGIC)
    fh.seek(footer_start)
    tail = fh.read(_FOOTER_SIZE + len(MAGIC))
    if tail[-len(MAGIC):] != MAGIC:
        return None
    if tail[0] != OP_FOOTER or struct.unpack_from("<Q", tail, 1)[0] != 20:
        raise McapFormatError(f"{path} ends with the closing magic but no footer before it")
    summary_start, offsets_start, crc = struct.unpack_from("<QQI", tail, 9)
    if summary_start == 0:
        return None
    end = offsets_start or footer_start
    if not len(MAGIC) <= summary_start <= end <= footer_start:
        raise McapFormatError(f"{path}: the footer names a summary at {summary_start}, "
                              f"which is not inside the file")
    fh.seek(summary_start)
    section = fh.read(footer_start - summary_start)
    if crc and zlib.crc32(tail[:9 + 16], zlib.crc32(section)) != crc:
        raise McapFormatError(f"{path}: the summary section does not match its checksum")
    try:
        return _parse_summary(section[:end - summary_start])
    except (struct.error, UnicodeDecodeError, IndexError) as exc:
        raise McapFormatError(f"{path}: the summary section does not parse: {exc}") from exc


def _parse_summary(section: bytes) -> Summary:
    schemas: Dict[int, Schema] = {}
    channels: Dict[int, Channel] = {}
    chunks: List[ChunkIndex] = []
    message_count = None
    pos = 0
    while pos < len(section):
        if pos + 9 > len(section):
            raise McapFormatError("a summary record is cut off")
        op = section[pos]
        (n,) = struct.unpack_from("<Q", section, pos + 1)
        body = section[pos + 9:pos + 9 + n]
        if len(body) < n:
            raise McapFormatError("a summary record runs past the summary's end")
        if op == OP_STATISTICS:
            (message_count,) = struct.unpack_from("<Q", body)
        elif op == OP_CHUNK_INDEX:
            chunks.append(_chunk_index(body))
        elif op in (OP_SCHEMA, OP_CHANNEL):
            record = _parse(op, body)
            if isinstance(record, Schema):
                schemas[record.id] = record
            else:
                channels[record.id] = record
        pos += 9 + n
    chunks.sort(key=lambda c: c.offset)
    return Summary(schemas, channels, tuple(chunks), message_count)


def _chunk_index(body: bytes) -> ChunkIndex:
    cur = _Cursor(body)
    start, end, offset, length = cur.u64(), cur.u64(), cur.u64(), cur.u64()
    stop = cur.pos + 4 + cur.u32()
    offsets = {}
    while cur.pos < stop:
        channel_id = cur.u16()
        offsets[channel_id] = cur.u64()
    return ChunkIndex(start, end, offset, length, offsets, cur.u64(), cur.string(), cur.u64(),
                      cur.u64())


def _message_index(body: bytes) -> Tuple[int, List[Tuple[int, int]]]:
    """``(channel id, [(log_time, offset in the chunk's records)])`` of a message index."""
    cur = _Cursor(body)
    channel_id = cur.u16()
    n = cur.u32()
    if n % 16 or cur.pos + n > len(body):
        raise McapFormatError("a message index's entries do not fit its record")
    entries = [struct.unpack_from("<QQ", body, cur.pos + i) for i in range(0, n, 16)]
    return channel_id, entries


@dataclass(frozen=True)
class Entry:
    """One message of an indexed file: its time, its chunk, and where it lies in the chunk."""
    log_time: int
    chunk: ChunkIndex
    inner: int


class IndexedFile:
    """A closed file whose chunks carry message indexes, read through them.

    *summary* is the file's :func:`read_summary`, taken from the same file: the file's
    identity is taken with it, and every later read refuses a file replaced since
    (:class:`McapFormatError`), so a reader never mixes the index of one file with the bytes
    of another. One decompressed chunk is kept, so consecutive messages of one chunk cost
    one decompression.
    """

    def __init__(self, path, summary: Summary, identity: Tuple[int, int]):
        if not summary.message_indexed:
            raise ValueError(f"{path} has no message index in every chunk")
        self.path = path
        self.summary = summary
        self.identity = identity
        self._chunk: Optional[Tuple[int, bytes]] = None

    @classmethod
    def open(cls, path) -> Optional["IndexedFile"]:
        """The file at *path* as an :class:`IndexedFile`; ``None`` when it has no summary
        or a chunk without a message index, which is a file read by walking it."""
        with open(path, "rb") as fh:
            identity = file_identity(os.fstat(fh.fileno()))
            summary = _read_summary(fh, path)
        if summary is None or not summary.message_indexed:
            return None
        return cls(path, summary, identity)

    def channel(self, topic: str) -> Optional[Channel]:
        """The channel of *topic*; the newest, the highest id, where several carry it, as a
        walk that keeps the last channel record of the topic ends up with."""
        ids = [cid for cid, ch in self.summary.channels.items() if ch.topic == topic]
        return self.summary.channels[max(ids)] if ids else None

    def _open(self):
        fh = open(self.path, "rb")  # pylint: disable=consider-using-with
        if file_identity(os.fstat(fh.fileno())) != self.identity:
            fh.close()
            raise McapFormatError(f"{self.path} was replaced after its index was read")
        return fh

    def entries(self, channel_id: int, start_ns: Optional[int] = None) -> Iterator[Entry]:
        """Every message of *channel_id*, in file order, read from the message indexes
        alone; a chunk whose messages all end before *start_ns* is not looked into."""
        with self._open() as fh:
            for chunk in self.summary.chunks:
                offset = chunk.message_index_offsets.get(channel_id)
                if offset is None or (start_ns is not None
                                      and chunk.message_end_time < start_ns):
                    continue
                op, body = read_record(fh, offset)
                if op != OP_MESSAGE_INDEX:
                    raise McapFormatError(f"{self.path}: no message index at offset {offset}")
                indexed, pairs = _message_index(body)
                if indexed != channel_id:
                    raise McapFormatError(f"{self.path}: the message index at offset {offset} "
                                          f"is channel {indexed}'s, not {channel_id}'s")
                for log_time, inner in sorted(pairs, key=lambda p: p[1]):
                    yield Entry(log_time, chunk, inner)

    def records(self, chunk: ChunkIndex) -> bytes:
        """The decompressed records of *chunk*."""
        if self._chunk is not None and self._chunk[0] == chunk.offset:
            return self._chunk[1]
        with self._open() as fh:
            op, body = read_record(fh, chunk.offset)
        if op != OP_CHUNK:
            raise McapFormatError(f"{self.path}: no chunk at offset {chunk.offset}")
        records = chunk_payload(body)
        self._chunk = (chunk.offset, records)
        return records

    def message(self, entry: Entry) -> Message:
        """The message *entry* names, its chunk decompressed and nothing else read."""
        return message_at(self.records(entry.chunk), entry.inner)


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
        #: The identity of the file the first read opened (:func:`file_identity`).
        self.identity: Optional[Tuple[int, int]] = None

    def read(self) -> Iterator:
        """Yield every complete record after :attr:`offset`, advancing it as they are read.

        Schemas and channels are yielded too, after being recorded, so a caller can react
        to a topic appearing. Returns when the file has no further complete record. A file
        replaced since the first read is refused (:class:`McapFormatError`): its offsets are
        another file's.
        """
        with open(self.path, "rb") as fh:
            identity = file_identity(os.fstat(fh.fileno()))
            if self.identity is None:
                self.identity = identity
            elif identity != self.identity:
                raise McapFormatError(f"{self.path} was replaced after {self.offset} bytes of "
                                      "it were read")
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


__all__ = ["Channel", "ChunkIndex", "Entry", "IndexedFile", "McapFormatError", "McapTail",
           "Message", "Metadata", "Schema", "Summary", "chunk_messages", "chunk_payload",
           "file_identity", "has_footer", "message_at", "parse_message", "read_record",
           "read_summary", "summary_channels"]
