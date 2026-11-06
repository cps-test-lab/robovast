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

"""A finished run's recordings, packed: every mcap file chunked, zstd-compressed and indexed.

A run's ROS bag is written through -- no chunks, no compression, no index -- so that it is
readable while it records. Once the run is over that layout only costs: the file is several
times larger than it has to be, and finding one message in it means walking it. **Packing**
rewrites each closed file in place as an indexed mcap file:

* zstd chunks of about :data:`CHUNK_SIZE` bytes of records each;
* after each chunk a message index per channel, saying where each of the channel's messages
  lies in the chunk and when it was logged;
* a summary: the statistics, every schema and channel, the index of every chunk (where it
  is, the times it spans, where its message indexes are) and of the metadata and
  attachments.

A reader that wants one message then reads the summary, the topic's message index and the one
chunk that holds it (:class:`~robovast_decode.framing.IndexedFile`), never the rest.

**Nothing recorded changes.** Every message keeps its channel, sequence number, log and
publish times and bytes, and the messages keep their order; every schema, channel, metadata
record and attachment is carried over with its content, and the header keeps its profile and
library. Schema and channel ids keep their order, and ids numbered from one without a gap --
as rosbag2 and roqsim number them -- are kept as they are. Every schema and channel is
written ahead of the first message. Metadata records and attachments keep their order among
themselves; a chunked file does not interleave them with the messages.

**Only a closed recording is packed, and only once.** A run is packed once it has its verdict,
every recording of it is closed (:func:`~robovast_decode.build.recordings_closed`) and no live
session still reads it (:mod:`robovast_decode.live`), and within it only a file that ends with
its footer: one a killed recorder left is read as it is,
up to its last complete record, and is reported as left. A file whose messages are all in
zstd chunks its summary indexes -- as the simulator's own recording is written -- is already
packed and is not touched.

**There is one copy, and it is whole.** The packed file is written beside the original as
``<name>.mcap.tmp``, read back, synced to disk and renamed over the original. The rename is
atomic, so a reader opens the old file or the new one, never a mixture; a reader that holds
places in the old one refuses to read them in the new one
(:class:`~robovast_decode.framing.McapTail`, :class:`~robovast_decode.frames.FrameIndex`). A
rewrite that fails removes the temporary file and leaves the original as it was, and says
why.

**What was built from it stays current.** A table entry records the size of the recordings it
was built from. Packing changes that size and none of the messages, so the run's entries that
were current against the old size are carried to the new one, under the run's lock, rather
than built again.
"""

from __future__ import annotations

import multiprocessing
import os
import stat
import struct
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from mcap.exceptions import McapError
from mcap.records import Attachment, DataEnd, Header, Message, Metadata
from mcap.stream_reader import StreamReader
from mcap.writer import CompressionType, IndexType, Writer

from .build import Run, find_runs, recordings_closed, source_size
from .framing import Channel as FChannel
from .framing import McapFormatError, McapTail
from .framing import Schema as FSchema
from .framing import has_footer, read_summary, summary_channels
from .live import VERDICT
from .tables import FileCatalog, live_owned, run_lock

#: Bytes of records a chunk holds before it is closed. A message is found by decompressing the
#: one chunk that holds it, which at this size takes milliseconds; much smaller chunks compress
#: worse (a camera frame alone nearly fills one) and lengthen the summary.
CHUNK_SIZE = 4 * 1024 * 1024

#: What a packed file is written as beside the original before it replaces it. Not ``*.mcap``,
#: so nothing that lists a recording's segments sees it.
TMP_SUFFIX = ".tmp"


class PackError(RuntimeError):
    """A file could not be packed; the original is as it was."""


@dataclass(frozen=True)
class PackedFile:
    """One file packed: where it is, and its size before and after."""
    path: str
    before: int
    after: int


@dataclass
class PackReport:
    """What packing a campaign's recordings did."""
    packed: List[PackedFile] = field(default_factory=list)
    #: Files that were packed before, or written packed.
    already: int = 0
    #: ``{run key or file: why}`` of what was left as recorded: a run not over, a file never
    #: closed. Not failures: such a recording is read as it is.
    left: Dict[str, str] = field(default_factory=dict)
    #: ``{file: why}`` of the rewrites that failed, each original intact.
    failed: Dict[str, str] = field(default_factory=dict)

    @property
    def bytes_before(self) -> int:
        return sum(p.before for p in self.packed)

    @property
    def bytes_after(self) -> int:
        return sum(p.after for p in self.packed)

    def merge(self, other: "PackReport") -> None:
        self.packed += other.packed
        self.already += other.already
        self.left.update(other.left)
        self.failed.update(other.failed)


def is_packed(path: str) -> bool:
    """Whether the closed file *path* is packed: its summary indexes zstd chunks that hold
    every message, or it has none. A file still open or without a summary is not."""
    summary = read_summary(path)
    return summary is not None and summary.packed


def pack_file(path: str, chunk_size: int = CHUNK_SIZE) -> Optional[PackedFile]:
    """Pack the closed file *path* in place; ``None`` when it is packed already.

    :class:`PackError` when it is not closed, or when it could not be rewritten whole; the
    original is then as it was and no temporary file is left.
    """
    if not has_footer(path):
        raise PackError(f"{path} has no footer: its writer has not closed it")
    try:
        summary = read_summary(path)
    except McapFormatError as exc:
        raise PackError(str(exc)) from exc
    if summary is not None and summary.packed:
        return None
    before = os.path.getsize(path)
    tmp = path + TMP_SUFFIX
    try:
        with open(path, "rb") as src, open(tmp, "wb") as dst:
            written = _rewrite(src, dst, chunk_size)
            dst.flush()
            os.fsync(dst.fileno())
        packed = read_summary(tmp)
        expected = summary.message_count if summary and summary.message_count is not None \
            else written
        if packed is None or not packed.packed or packed.message_count != written \
                or written != expected:
            raise PackError(f"{path}: the packed file does not hold the {expected} message(s) "
                            f"the original does")
        os.chmod(tmp, stat.S_IMODE(os.stat(path).st_mode))
        os.replace(tmp, path)
    except BaseException as exc:
        _remove(tmp)
        if isinstance(exc, (OSError, ValueError, KeyError, struct.error, McapError,
                            McapFormatError)):
            raise PackError(f"{path}: {exc}") from exc
        raise
    _sync_dir(os.path.dirname(path))
    return PackedFile(path, before, os.path.getsize(path))


def _rewrite(src, dst, chunk_size: int) -> int:
    """Copy every record of *src*'s data section into an indexed, chunked *dst*; the
    messages written.

    Every schema and channel is registered first, each kind in the order of its ids, so the
    ids keep their order and ids numbered from one without a gap stay what they were.
    """
    schemas, channels = _definitions(src)
    writer = Writer(dst, chunk_size=chunk_size, compression=CompressionType.ZSTD,
                    index_types=IndexType.ALL)
    schema_ids: Dict[int, int] = {0: 0}
    channel_ids: Dict[int, int] = {}
    messages = 0
    src.seek(0)
    records = StreamReader(src, validate_crcs=True).records
    header = next(records, None)
    if not isinstance(header, Header):
        raise PackError("the file does not start with a header")
    writer.start(header.profile, header.library)
    for schema_id, schema in sorted(schemas.items()):
        schema_ids[schema_id] = writer.register_schema(schema.name, schema.encoding, schema.data)
    for channel_id, channel in sorted(channels.items()):
        if channel.schema_id not in schema_ids:
            raise PackError(f"channel {channel.topic} names schema {channel.schema_id}, which "
                            "the file does not define")
        channel_ids[channel_id] = writer.register_channel(
            channel.topic, channel.message_encoding, schema_ids[channel.schema_id],
            dict(channel.metadata))
    for record in records:
        if isinstance(record, Message):
            if record.channel_id not in channel_ids:
                raise PackError(f"a message names channel {record.channel_id}, which the file "
                                "does not define")
            writer.add_message(channel_ids[record.channel_id], record.log_time, record.data,
                               record.publish_time, record.sequence)
            messages += 1
        elif isinstance(record, Metadata):
            writer.add_metadata(record.name, dict(record.metadata))
        elif isinstance(record, Attachment):
            writer.add_attachment(record.create_time, record.log_time, record.name,
                                  record.media_type, record.data)
        elif isinstance(record, DataEnd):
            break
    else:
        raise PackError("the data section has no end record")
    writer.finish()
    return messages


def _definitions(src) -> tuple:
    """``(schemas, channels)`` by id of the file *src*: from its summary where that holds
    them all, otherwise from a walk of its data section. A schema or channel the file
    defines twice, differently, is refused."""
    found = summary_channels(src.name)
    if found is not None:
        return found
    schemas: Dict[int, FSchema] = {}
    channels: Dict[int, FChannel] = {}
    for record in McapTail(src.name).read():
        if isinstance(record, FSchema):
            known = schemas.setdefault(record.id, record)
        elif isinstance(record, FChannel):
            known = channels.setdefault(record.id, record)
        else:
            continue
        if known != record:
            raise PackError(f"{type(record).__name__.lower()} {record.id} is defined twice, "
                            "differently")
    return schemas, channels


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _sync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def run_recordings(run: Run) -> Dict[str, List[str]]:
    """``{directory: [mcap file]}`` of every mcap file below the run's directory and its
    job's, each directory's files in name order."""
    found: Dict[str, List[str]] = {}
    for root in (run.path, run.job_dir):
        if not root or not os.path.isdir(root):
            continue
        for directory, _dirs, files in os.walk(root):
            mcaps = sorted(f for f in files if f.endswith(".mcap"))
            if mcaps:
                found[directory] = [os.path.join(directory, f) for f in mcaps]
    return found


def pack_run(campaign_dir: str, run: Run, chunk_size: int = CHUNK_SIZE) -> PackReport:
    """Pack every closed mcap file of *run* (:func:`run_recordings`), holding its lock.

    A run without its verdict, or with a recording still open, is left whole. The run's table
    entries current against a recording's old size are carried to its new one.
    """
    campaign_dir = os.path.abspath(campaign_dir)
    report = PackReport()
    with run_lock(campaign_dir, run.key):
        if not os.path.isfile(os.path.join(run.path, VERDICT)):
            report.left[run.key] = "the run has no verdict yet"
            return report
        if not recordings_closed(run):
            report.left[run.key] = "a recording of the run is still open"
            return report
        if _followed(campaign_dir, run.key):
            report.left[run.key] = ("a live session is still reading its recordings; the next "
                                    "postprocessing packs them")
            return report
        resized: Dict[str, tuple] = {}
        for directory, files in run_recordings(run).items():
            before = source_size(directory)
            changed = False
            for path in files:
                if not has_footer(path):
                    report.left[os.path.relpath(path, campaign_dir)] = (
                        "no footer: its writer never closed it, so it is read as it is")
                    continue
                try:
                    packed = pack_file(path, chunk_size)
                except PackError as exc:
                    report.failed[os.path.relpath(path, campaign_dir)] = str(exc)
                    continue
                if packed is None:
                    report.already += 1
                    continue
                report.packed.append(packed)
                changed = True
            if changed:
                resized[os.path.relpath(directory, campaign_dir)] = (before,
                                                                     source_size(directory))
        if resized:
            _carry_entries(campaign_dir, run.key, resized)
    return report


def _followed(campaign_dir: str, run_key: str) -> bool:
    """Whether a live session still writes a table of the run (its ``live`` stamp is fresh):
    it holds places in the recordings that packing would move."""
    manifest = FileCatalog(campaign_dir).read()
    return any(live_owned(table.get("runs", {}).get(run_key))
               for table in manifest.get("tables", {}).values())


def _carry_entries(campaign_dir: str, run_key: str, resized: Dict[str, tuple]) -> None:
    """Point the run's table entries that were current against a recording's old size at its
    new size: the messages they were built from are the same."""
    with FileCatalog(campaign_dir).update() as manifest:
        for table in manifest.get("tables", {}).values():
            entry = table.get("runs", {}).get(run_key)
            sources = (entry or {}).get("sources")
            if not sources:
                continue
            for rel, (before, after) in resized.items():
                if sources.get(rel) == before:
                    sources[rel] = after


def _pack_one(campaign_dir: str, run: Run, chunk_size: int) -> PackReport:
    return pack_run(campaign_dir, run, chunk_size)


def pack(campaign_dir: str, *, workers: int = 1, chunk_size: int = CHUNK_SIZE,
         progress: Optional[Callable[[int, int], None]] = None,
         should_stop: Optional[Callable[[], bool]] = None) -> PackReport:
    """Pack the recordings of every run of the campaign (:func:`pack_run`).

    *workers* packs that many runs at once in separate processes; *progress* is called with
    ``(done, total)`` in runs. *should_stop* is asked between runs; a stop leaves the runs not
    yet started as they are, each one started finished or untouched.
    """
    campaign_dir = os.path.abspath(campaign_dir)
    runs = find_runs(campaign_dir)
    report = PackReport()
    total = len(runs)
    if workers <= 1 or total <= 1:
        for done, run in enumerate(runs, 1):
            if should_stop is not None and should_stop():
                break
            report.merge(pack_run(campaign_dir, run, chunk_size))
            if progress:
                progress(done, total)
        return report
    with ProcessPoolExecutor(max_workers=min(workers, total),
                             mp_context=multiprocessing.get_context("spawn")) as pool:
        futures = [pool.submit(_pack_one, campaign_dir, run, chunk_size) for run in runs]
        for done, future in enumerate(as_completed(futures), 1):
            report.merge(future.result())
            if progress:
                progress(done, total)
            if should_stop is not None and should_stop():
                for pending in futures:
                    pending.cancel()
                break
    return report


__all__ = ["CHUNK_SIZE", "PackError", "PackReport", "PackedFile", "is_packed", "pack",
           "pack_file", "pack_run", "run_recordings"]
