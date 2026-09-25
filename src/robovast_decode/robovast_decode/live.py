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

"""Follow a recording while it is written, and give its tables in batches as it grows.

:func:`~robovast_decode.build.build` reads a recording whole. This module reads it as it
grows: a :class:`Session` per ``(run, recording directory)`` keeps where it stopped in every
segment, feeds the new records to the same handlers a whole build would use, and *flushes*
them -- each :meth:`Handler.flush` gives the rows buffered since the last one, without
finalising anything a handler keeps beside its rows, so the batches of a session add up to
exactly what one pass over the finished recording gives.

A :class:`Batch` goes two ways: to whoever subscribed to the run's tables, and to a
:class:`PartWriter`, which writes what accumulated as one parquet *part* per table and names
the parts in the manifest with a ``live`` stamp. A query during the run reads the parts
written so far; when the run has its verdict and the recorder closed the bag, the parts are
merged into the run's one file and the entry is ``complete``. The stamp is refreshed on
every write, rows or not, so a table whose run has gone quiet stays the session's; one whose
session died goes stale and a build takes it whole.

:class:`Watcher` is the loop a service runs, one per campaign: it holds the sessions the
demanded ``(run, tables)`` need, advances the ones whose files changed, writes parts on a
period or when a segment closed, and finalises a session when its run is done. It is driven
from an inotify-style watch over the campaign directory; it does not need one to be tested.

Everything here is the decoder's plain Python: no ROS, no execution image.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Set

import pyarrow as pa
import pyarrow.parquet as pq

from .authored import with_yaw
from .build import (BAG_METADATA, Run, find_runs, plugin_groups, recorded_topics,
                    recording_closed, roqsim_recording, scenario_recording)
from .decode import channel_type, segments
from .definitions import TypeCatalog
from .frames import FrameRef, FrameTap
from .framing import Channel, McapTail, Message, Metadata, Schema
from .handlers import Handler, Videos
from .layout import decoder_config
from .registry import INFRA_BAG, ROQSIM_BAG, SCENARIO_BAG, narrow, plan_for
from .tables import (LIVE_STALE_S, cache_root, manifest_lock, read_manifest, record_run_absent,
                     record_run_table, remove_files, run_part_path, run_table_path,
                     write_manifest, write_table)

_LOG = logging.getLogger(__name__)

#: The file a run has once its verdict is written.
VERDICT = "test.xml"


@dataclass(frozen=True)
class Batch:
    """The rows one flush gave for one table."""
    table: str
    rows: pa.Table


class LostOwnership(RuntimeError):
    """The entry a writer was appending to was rewritten by something else.

    A build takes an entry over once its ``live`` stamp is stale, and replaces the parts
    with one file; a writer that finds that has rows on disk nobody names and no way to tell
    which of its rows the build's file holds. Its session starts again from the recording's
    beginning.
    """


def _role(bag_dir: str) -> str:
    """Which recording *bag_dir* is: the run's scenario recording, its simulator's own, or
    its job's infrastructure recording."""
    path = bag_dir.replace(os.sep, "/")
    if path.endswith("/" + INFRA_BAG):
        return INFRA_BAG
    if path.rsplit("/", 1)[-1] == ROQSIM_BAG:
        return ROQSIM_BAG
    return SCENARIO_BAG


class Session:
    """One recording of one run, decoded as far as it has been written, batch by batch.

    *tables* narrows the handlers to the ones those tables need (``None``: every table the
    recording can give); a table the recording's topics so far cannot give is listed in
    :attr:`unknown`. *config* is the campaign's decoder configuration. A table whose handler
    failed is in :attr:`failed` with the reason, from then on. *frames* names image topics
    to tap (:class:`~robovast_decode.frames.FrameTap`): every message of one is recorded
    with its place and its bytes as it is met, undeserialized, and never becomes a row.
    """

    def __init__(self, campaign_dir: str, run: Run, bag_dir: str,
                 tables: Optional[Iterable[str]] = None, config: Optional[dict] = None,
                 frames: Optional[Iterable[str]] = None):
        self.campaign_dir = os.path.abspath(campaign_dir)
        self.run = run
        self.bag_dir = os.path.abspath(bag_dir)
        self.role = _role(self.bag_dir)
        self._columns = {"campaign_id": os.path.basename(self.campaign_dir),
                         "config_name": run.config_name, "run_id": run.run_id}
        #: ``{topic: type}`` of everything the recording has been seen to carry.
        self.recorded: Dict[str, str] = recorded_topics(self.bag_dir)
        #: Set when a topic appeared that was not there when the session was planned.
        self.topics_changed = False
        #: The campaign's configured handlers for this recording, as the registry takes them.
        self.plugins = plugin_groups(config).get(self.role)
        plan = plan_for(self.role, self.recorded, self.plugins)
        handlers, self.unknown = narrow(plan, tables)
        self._wanted_tables = None if tables is None else set(tables)
        #: The tables this session gives, in the order their handlers fill them.
        self.tables: List[str] = [t for h in handlers for t in h.tables()
                                  if self._wanted_tables is None or t in self._wanted_tables]
        self.failed: Dict[str, str] = {}
        self.catalog = TypeCatalog()
        self._sidecar = self.catalog.add_sidecar(self.bag_dir) > 0
        self._active: List[Handler] = list(handlers)
        self._readers: Dict[str, List[Handler]] = {}
        for handler in handlers:
            handler.fields_of = self.catalog.fields
            if isinstance(handler, Videos):
                handler.output_dir = run.path
                handler.bag_name = os.path.basename(self.bag_dir)
            for topic in handler.topics():
                self._readers.setdefault(topic, []).append(handler)
        #: ``{topic: tap}`` of the image topics followed frame by frame.
        self.taps: Dict[str, FrameTap] = {t: FrameTap(self.bag_dir, t) for t in (frames or ())}
        self._tails: Dict[str, McapTail] = {}
        self._undecodable: Dict[str, str] = {}
        #: Set by :meth:`finish`: nothing more will come from this session.
        self.finished = False

    # -- state -------------------------------------------------------------------------

    @property
    def closed(self) -> bool:
        """Whether the recorder closed the recording: rosbag2 wrote its ``metadata.yaml``,
        roqsim's writer its footer (:func:`~robovast_decode.build.recording_closed`)."""
        return recording_closed(self.role, self.bag_dir)

    @property
    def bytes_read(self) -> Dict[str, int]:
        """``{segment path: bytes read}``: where the next read of each segment resumes."""
        return {path: tail.offset for path, tail in self._tails.items()}

    @property
    def segments_closed(self) -> int:
        """How many segments have been read to their footer."""
        return sum(1 for tail in self._tails.values() if tail.finished)

    def sources(self) -> Dict[str, int]:
        """What the rows so far were built from, in the manifest's ``sources`` shape.

        While the session runs, the bytes read of the recording; once finished, the size of
        its segments, which is what a whole build records, so a later build finds the entry
        current and leaves it alone.
        """
        rel = os.path.relpath(self.bag_dir, self.campaign_dir)
        if self.finished:
            return {rel: sum(os.path.getsize(p) for p in segments(self.bag_dir))}
        return {rel: sum(self.bytes_read.values())}

    # -- reading -----------------------------------------------------------------------

    def advance(self) -> List[Batch]:
        """Read what every segment has gained, feed the handlers, and flush their new rows.

        Only tables with new rows are returned; a table that gained none since the last
        flush is not a batch.
        """
        if self.finished:
            raise RuntimeError(f"the session on {self.bag_dir} is finished")
        self._read()
        return [b for b in self._flush() if b.rows.num_rows]

    def finish(self) -> List[Batch]:
        """The bag is closed: read to its end, end every handler, flush the rest.

        Every table the session gives is returned, empty or not, so a writer records the
        ones the run came out empty for as well as the ones with rows.
        """
        if self.finished:
            raise RuntimeError(f"the session on {self.bag_dir} is finished")
        if not self.closed:
            what = "no footer" if self.role == ROQSIM_BAG else f"no {BAG_METADATA}"
            raise RuntimeError(f"{self.bag_dir} is still being written: {what} yet")
        self._read()
        recorded = dict(self.recorded)
        recorded.update(recorded_topics(self.bag_dir))
        for handler in list(self._active):
            try:
                handler.end(recorded)
            except Exception as exc:  # noqa: BLE001 - HandlerError, or a handler's own bug
                self._fail(handler, exc)
        batches = self._flush()
        self.finished = True
        return batches

    def _read(self) -> None:
        for path in segments(self.bag_dir):
            tail = self._tails.get(path)
            if tail is None:
                # Schema and channel ids are per file: every segment has its own maps, and
                # rosbag2 writes a segment's own schema and channel records into it.
                tail = self._tails[path] = McapTail(path)
            if not tail.finished:
                self._feed(tail)

    def _feed(self, tail: McapTail) -> None:
        # Where a message lies, for the taps: the tail's offset is the start of the
        # top-level record being yielded, and a chunk's messages are counted from there.
        record_offset, ordinal = -1, 0
        for record in tail.read():
            if isinstance(record, Schema):
                if record.encoding in ("ros2msg", "ros2idl") and record.data:
                    self.catalog.add_definition(record.name, record.encoding,
                                                record.data.decode("utf-8", errors="replace"))
                continue
            if isinstance(record, Channel):
                if record.topic not in self.recorded:
                    self.recorded[record.topic] = channel_type(record, tail.schemas)
                    self.topics_changed = True
                continue
            if isinstance(record, Metadata):
                for handler in list(self._active):
                    try:
                        handler.metadata(record.name, record.metadata)
                    except Exception as exc:  # noqa: BLE001
                        self._fail(handler, exc)
                continue
            if not isinstance(record, Message):
                continue
            if tail.offset != record_offset:
                record_offset, ordinal = tail.offset, 0
            index, ordinal = ordinal, ordinal + 1
            channel = tail.channels.get(record.channel_id)
            if channel is None:
                continue
            topic = channel.topic
            tap = self.taps.get(topic)
            if tap is not None:
                tap.record(FrameRef(record.log_time / 1e9, tail.path, record_offset, index),
                           channel_type(channel, tail.schemas), channel.message_encoding,
                           record.data)
            readers = self._readers.get(topic)
            if not readers or topic in self._undecodable:
                continue
            typename = channel_type(channel, tail.schemas)
            encoding = channel.message_encoding
            if not self._decodable(typename, encoding):
                self._undecodable[topic] = self.catalog.missing([typename])[typename]
                continue
            try:
                msg = self.catalog.deserialize(record.data, typename, encoding)
            except Exception as exc:  # noqa: BLE001 - a message that does not match its schema
                self._undecodable[topic] = f"a message does not decode as {typename}: {exc}"
                continue
            for handler in list(readers):
                try:
                    handler.message(topic, msg, typename, record.log_time)
                except Exception as exc:  # noqa: BLE001
                    self._fail(handler, exc)

    def _decodable(self, typename: str, encoding: str) -> bool:
        if self.catalog.ensure(typename, encoding):
            return True
        # The sidecar is written by the run's container, which may be after the recorder's
        # first records: look for it once more before giving a type up.
        if not self._sidecar and self.catalog.add_sidecar(self.bag_dir):
            self._sidecar = True
            return self.catalog.ensure(typename, encoding)
        return False

    def _fail(self, handler: Handler, exc: Exception) -> None:
        if handler not in self._active:
            return
        self._active.remove(handler)
        reason = f"{type(exc).__name__}: {exc}"
        for table in handler.tables():
            if table in self.tables:
                self.failed[table] = reason
        for readers in self._readers.values():
            if handler in readers:
                readers.remove(handler)

    def _flush(self) -> List[Batch]:
        batches = []
        for handler in self._active:
            for table, rows in handler.flush(context=self._columns).items():
                if table in self.tables:
                    batches.append(Batch(table, with_yaw(rows)))
        return batches


class PartWriter:
    """Batches of one run's tables into parquet parts the manifest names, then one file.

    The rows are filed under the run's key, whichever of its recordings gave them -- its
    job's infrastructure recording included, since a job runs one run. While parts are
    written the entry is ``live``-stamped and owned by this writer: a write finds the entry's
    files are its own parts and appends, or replaces them when something else wrote the entry
    in between.
    """

    def __init__(self, campaign_dir: str, run: Run):
        self.campaign_dir = os.path.abspath(campaign_dir)
        self.run = run
        self.key = run.key
        self._path = (run.config_name, run.run_id)
        self._pending: Dict[str, List[pa.Table]] = {}
        self._parts: Dict[str, List[str]] = {}
        self._rows: Dict[str, int] = {}
        self._schemas: Dict[str, pa.Schema] = {}
        self._next: Dict[str, int] = {}
        #: Set by :meth:`finalise`.
        self.finalised = False

    @property
    def parts(self) -> Dict[str, List[str]]:
        """``{table: [part paths relative to the cache root]}`` written so far."""
        return {t: list(p) for t, p in self._parts.items()}

    def append(self, batch: Batch) -> None:
        """Hold *batch* for the next :meth:`write`. An empty batch declares its table."""
        schema = self._schemas.get(batch.table)
        self._schemas[batch.table] = (batch.rows.schema if schema is None
                                      else pa.unify_schemas([schema, batch.rows.schema],
                                                            promote_options="permissive"))
        if batch.rows.num_rows:
            self._pending.setdefault(batch.table, []).append(batch.rows)

    def write(self, sources: Optional[dict] = None) -> List[str]:
        """Write what accumulated as one part per table and name the parts in the manifest.

        *sources* is what the rows were built from (:meth:`Session.sources`). Returns the
        parts written, relative to the cache root. With nothing accumulated the entries are
        still stamped ``live`` again: the stamp is what keeps a build from taking over a
        table whose run is quiet. Raises :class:`LostOwnership` when an entry this writer
        appended to before was rewritten by something else in the meantime.
        """
        if self.finalised:
            raise RuntimeError(f"{self.key}: the writer is finalised")
        written = []
        for table, batches in self._pending.items():
            rows = pa.concat_tables(batches, promote_options="permissive")
            index = self._next.get(table, 0)
            rel = run_part_path(self.campaign_dir, table, *self._path, index)
            write_table(self.campaign_dir, rel, rows)
            self._next[table] = index + 1
            written.append((table, rel, rows.num_rows))
        self._pending = {}
        new = {table: (rel, rows) for table, rel, rows in written}
        if not new and not self._parts:
            return []
        now = time.time()
        superseded: List[str] = []
        with manifest_lock(self.campaign_dir):
            manifest = read_manifest(self.campaign_dir)
            for table in sorted(set(new) | set(self._parts)):
                entry = manifest["tables"].get(table, {}).get("runs", {}).get(self.key) or {}
                own = self._parts.get(table, [])
                if own and entry.get("files") != own:
                    raise LostOwnership(
                        f"{self.key}/{table}: the entry names {entry.get('files')}, not the "
                        f"parts this writer wrote; it was rebuilt while the stamp was stale")
                rel, rows = new.get(table, (None, 0))
                if rel is not None:
                    self._parts.setdefault(table, []).append(rel)
                self._rows[table] = self._rows.get(table, 0) + rows
                superseded += record_run_table(
                    manifest, table, self.key, files=self._parts[table], rows=self._rows[table],
                    schema=self._schemas[table], sources=dict(sources or {}), complete=False,
                    live=now)
            write_manifest(self.campaign_dir, manifest)
        remove_files(self.campaign_dir, superseded)
        return [rel for _, rel, _ in written]

    def finalise(self, sources: Optional[dict] = None,
                 failed: Optional[Dict[str, str]] = None) -> List[str]:
        """Merge every table's parts and what is still pending into the run's one file.

        The entry is recorded ``complete`` without a ``live`` stamp, and the parts are
        removed after the manifest is written. *failed* is ``{table: reason}`` for tables
        whose handler failed: entered as absent with that reason, their parts removed.
        Returns the files written.
        """
        if self.finalised:
            raise RuntimeError(f"{self.key}: the writer is finalised")
        failed = dict(failed or {})
        root = cache_root(self.campaign_dir)
        final = []
        for table in list(self._schemas):
            if table in failed:
                continue
            pieces = [pq.read_table(os.path.join(root, rel)) for rel in self._parts.get(table, [])]
            pieces += self._pending.get(table, [])
            rows = (pa.concat_tables(pieces, promote_options="permissive") if pieces
                    else self._schemas[table].empty_table())
            rel = run_table_path(self.campaign_dir, table, *self._path)
            write_table(self.campaign_dir, rel, rows)
            final.append((table, rel, rows))
        self._pending = {}
        superseded: List[str] = []
        with manifest_lock(self.campaign_dir):
            manifest = read_manifest(self.campaign_dir)
            for table, rel, rows in final:
                superseded += record_run_table(manifest, table, self.key, files=[rel],
                                               rows=rows.num_rows, schema=rows.schema,
                                               sources=dict(sources or {}), complete=True)
                superseded += [p for p in self._parts.get(table, []) if p not in superseded]
            for table, reason in failed.items():
                record_run_absent(manifest, table, self.key, sources=dict(sources or {}),
                                  complete=True, reason=reason, known=True)
                superseded += [p for p in self._parts.get(table, []) if p not in superseded]
            write_manifest(self.campaign_dir, manifest)
        remove_files(self.campaign_dir, superseded)
        self._parts = {}
        self.finalised = True
        return [rel for _, rel, _ in final]


@dataclass
class _Following:
    """One session and the writer its batches go to; no writer for a session that only
    taps frames, since it has no rows to file."""
    session: Session
    writer: Optional[PartWriter]
    last_write: float
    segments_closed: int = 0


@dataclass
class _LiveRun:
    run: Run
    following: List[_Following] = field(default_factory=list)
    #: When the run's verdict was first seen with its recording still open.
    verdict_seen: Optional[float] = None

    @property
    def served(self) -> Set[str]:
        return {t for f in self.following for t in f.session.tables}


@dataclass
class _Listener:
    run_key: str
    tables: Set[str]
    callback: Callable[[Batch], None]
    #: Called once, after the run's last batch, when nothing more will come for it.
    finished: Optional[Callable[[], None]] = None


class Watcher:
    """The sessions a campaign's demanded ``(run, tables)`` need, driven by file changes.

    :meth:`demand` says which tables of which run to follow; :meth:`subscribe` does that and
    hands every batch of those tables to a callback as it is decoded; :meth:`changed` is told
    which files changed and advances the sessions concerned. Parts are written every
    *part_s* seconds (:data:`PART_S` by default) and when a segment closed; a session is
    finalised once its run has its verdict and its bag is closed. :meth:`run_forever` runs
    that from an inotify watch. Every method may be called from any thread.

    A run demanded before its directory or its recording exists is followed from the
    moment it appears; a table demanded that the recording's topics cannot give yet is
    started, from the recording's beginning, once a topic that gives it appears.

    A run is *done* for its subscribers when no session of it is left and nothing of it is
    pending: its sessions were finalised, or dropped because the run had its verdict for
    :data:`~robovast_decode.tables.LIVE_STALE_S` seconds with a recording still open or a
    table its recording never gave. A subscriber's ``finished`` callback is called then, once.
    """

    #: How often, at most, accumulated batches become parts on disk, by default.
    PART_S = 10.0

    def __init__(self, campaign_dir: str, config: Optional[dict] = None,
                 part_s: float = PART_S):
        self.campaign_dir = os.path.abspath(campaign_dir)
        self.config = config if config is not None else decoder_config(self.campaign_dir)
        self.part_s = part_s
        self._lock = threading.RLock()
        self._runs: Dict[str, _LiveRun] = {}
        self._pending: Dict[str, Set[str]] = {}
        #: ``{run key: image topics}`` asked for and not yet tapped by a session.
        self._pending_frames: Dict[str, Set[str]] = {}
        self._listeners: List[_Listener] = []
        self._stop = threading.Event()
        self._inotify = None

    # -- what is asked for -------------------------------------------------------------

    def demand(self, run: str, tables: Iterable[str]) -> None:
        """Follow *tables* of the run ``config/run_id`` *run*; those it already does, it keeps."""
        with self._lock:
            wanted = set(tables)
            live = self._runs.get(run)
            if live is not None:
                wanted -= live.served
            if wanted:
                self._pending.setdefault(run, set()).update(wanted)
                self._start(run)

    def subscribe(self, run: str, tables: Iterable[str],
                  callback: Callable[[Batch], None],
                  finished: Optional[Callable[[], None]] = None) -> Callable[[], None]:
        """Call *callback* with every batch of *tables* of *run*; returns how to stop.

        *finished*, if given, is called once after the run's last batch, when the watcher
        is done with the run (see the class); a subscriber that stopped before is not told.
        """
        listener = _Listener(run, set(tables), callback, finished)
        with self._lock:
            self._listeners.append(listener)
        self.demand(run, tables)

        def unsubscribe():
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)
        return unsubscribe

    def following(self, run: str) -> Set[str]:
        """The tables of *run* a session is following right now."""
        with self._lock:
            live = self._runs.get(run)
            return set(live.served) if live else set()

    def demand_frames(self, run: str, topics: Iterable[str]) -> None:
        """Tap the image *topics* of *run* frame by frame; those it already does, it keeps.

        A tap reads the run's scenario recording from its beginning in a session of its
        own, so every frame recorded before the demand is in the index too.
        """
        with self._lock:
            wanted = set(topics)
            live = self._runs.get(run)
            if live is not None:
                wanted -= {t for f in live.following for t in f.session.taps}
            if wanted:
                self._pending_frames.setdefault(run, set()).update(wanted)
                self._start(run)

    def frame_index(self, run: str, topic: str) -> Optional[FrameTap]:
        """The tap on *topic* of *run*, demanded if it was not; ``None`` while the run or
        its scenario recording is not on disk, or once the run is done."""
        self.demand_frames(run, [topic])
        with self._lock:
            live = self._runs.get(run)
            if live is None:
                return None
            for following in live.following:
                tap = following.session.taps.get(topic)
                if tap is not None:
                    return tap
            return None

    def newest_frame(self, run: str, topic: str):
        """``(stamp, JPEG)`` of the newest frame of *topic* of *run*, or ``None`` yet."""
        tap = self.frame_index(run, topic)
        return None if tap is None else tap.newest_frame()

    # -- what changed ------------------------------------------------------------------

    def changed(self, paths: Iterable[str]) -> None:
        """Files at *paths* changed: advance the sessions they concern, start those due."""
        with self._lock:
            keys = set()
            for path in paths:
                keys.update(self._runs_of(os.path.abspath(path)))
            for key in keys:
                if self._pending.get(key) or self._pending_frames.get(key):
                    self._start(key)
                live = self._runs.get(key)
                if live is not None:
                    self._advance(live)

    def tick(self) -> None:
        """Write the parts that are due, whether or not anything changed."""
        with self._lock:
            for live in list(self._runs.values()):
                self._advance(live, read=False)

    def _runs_of(self, path: str) -> Set[str]:
        """The run keys a changed file concerns: the run whose directory or whose job's
        directory (``_jobs/[<batch>/]job-N/``) holds it."""
        rel = os.path.relpath(path, self.campaign_dir)
        if rel == os.curdir:
            # The watch overflowed and reports its root: everything may have changed.
            return set(self._runs) | set(self._pending)
        parts = rel.split(os.sep)
        if parts[0] == "_jobs":
            return {k for k, live in self._runs.items() if live.run.job_dir
                    and (path == live.run.job_dir
                         or path.startswith(live.run.job_dir + os.sep))}
        if len(parts) >= 2 and parts[1].isdigit():
            return {f"{parts[0]}/{int(parts[1])}"}
        return set()

    # -- sessions ----------------------------------------------------------------------

    def _start(self, key: str) -> None:
        pending = self._pending.get(key)
        frames = self._pending_frames.get(key)
        if not pending and not frames:
            return
        live = self._runs.get(key)
        if live is None:
            runs = {r.key: r for r in find_runs(self.campaign_dir)}
            run = runs.get(key)
            if run is None:
                return
            live = self._runs[key] = _LiveRun(run)
        if frames:
            # Cameras record into the scenario recording. A tap reads it from the start in
            # a session of its own, with no tables: a session already under way has read
            # past the frames recorded before the demand.
            bag_dir = scenario_recording(live.run)
            if bag_dir is not None:
                session = Session(self.campaign_dir, live.run, bag_dir, [], self.config,
                                  frames=sorted(frames))
                live.following.append(_Following(session, None, time.monotonic()))
                self._pending_frames.pop(key, None)
        if not pending:
            self._advance(live)
            return
        # In the build's order: the simulator's own recording comes last and takes only the
        # tables the earlier ones did not (its clock map, where the infrastructure
        # recording's is the run's).
        recordings = [scenario_recording(live.run)]
        if live.run.job_dir:
            infra = os.path.join(live.run.job_dir, INFRA_BAG)
            recordings.append(infra if os.path.isdir(infra) else None)
        recordings.append(roqsim_recording(live.run))
        for bag_dir in recordings:
            if bag_dir is None or not pending:
                continue
            session = Session(self.campaign_dir, live.run, bag_dir, sorted(pending),
                              self.config)
            if not session.tables:
                continue
            writer = PartWriter(self.campaign_dir, live.run)
            live.following.append(_Following(session, writer, time.monotonic()))
            pending.difference_update(session.tables)
        if not pending:
            self._pending.pop(key, None)
        self._advance(live)

    def _advance(self, live: _LiveRun, read: bool = True) -> None:
        now = time.monotonic()
        verdict = os.path.isfile(os.path.join(live.run.path, VERDICT))
        for following in list(live.following):
            session = following.session
            if session.finished or following not in live.following:
                continue      # finalised by a nested advance while this loop held it
            writer = following.writer
            if read:
                batches = session.advance()
                self._push(live.run.key, batches)
                for batch in batches:
                    writer.append(batch)
                if session.topics_changed:
                    session.topics_changed = False
                    # A pending table a new topic gives: its own session, from the start.
                    self._retry(live)
            closed = session.segments_closed
            if closed > following.segments_closed:
                following.segments_closed = closed
                following.last_write = 0.0
            if verdict and session.closed:
                final = session.finish()
                self._push(live.run.key, [b for b in final if b.rows.num_rows])
                if writer is not None:
                    for batch in final:
                        writer.append(batch)
                    writer.finalise(session.sources(), failed=session.failed)
                live.following.remove(following)
                continue
            if writer is not None and now - following.last_write >= self.part_s:
                try:
                    following.writer.write(session.sources())
                except LostOwnership as exc:
                    # The stamp went stale long enough for a build to take the table: the
                    # rows are decoded again from the recording's start, into fresh parts.
                    _LOG.warning("%s; following %s again from the start", exc,
                                 session.bag_dir)
                    live.following.remove(following)
                    self._pending.setdefault(live.run.key, set()).update(session.tables)
                    self._start(live.run.key)
                    continue
                following.last_write = now
        key = live.run.key
        if verdict and (live.following or self._pending.get(key) or self._pending_frames.get(key)):
            live.verdict_seen = live.verdict_seen or now
            if now - live.verdict_seen >= LIVE_STALE_S:
                # The run is over and the recorder never closed its bag: the recording is
                # cut where it stopped, and a whole build makes what it can of it.
                for following in live.following:
                    _LOG.warning("%s: %s is still open after the run's verdict; the session "
                                 "is dropped and the table built whole", key,
                                 following.session.bag_dir)
                live.following = []
                # A table still pending after the verdict is one the run's recordings never
                # gave; it is not coming, and a whole build will say why. Frames of a
                # recording that never started are not coming either.
                self._pending.pop(key, None)
                self._pending_frames.pop(key, None)
        if (not live.following and not self._pending.get(key)
                and not self._pending_frames.get(key)):
            self._runs.pop(key, None)
            self._finished(key)

    def _retry(self, live: _LiveRun) -> None:
        pending = self._pending.get(live.run.key)
        if not pending:
            self._pending.pop(live.run.key, None)
            return
        givable = set()
        for following in live.following:
            session = following.session
            givable.update(plan_for(session.role, session.recorded, session.plugins).tables)
        if pending & givable:
            self._start(live.run.key)

    def _push(self, key: str, batches: List[Batch]) -> None:
        if not batches:
            return
        for listener in list(self._listeners):
            if listener.run_key != key:
                continue
            for batch in batches:
                if batch.table in listener.tables:
                    listener.callback(batch)

    def _finished(self, key: str) -> None:
        """Tell the run's subscribers that nothing more will come, and forget them."""
        for listener in list(self._listeners):
            if listener.run_key != key:
                continue
            self._listeners.remove(listener)
            if listener.finished is not None:
                listener.finished()

    # -- the loop ----------------------------------------------------------------------

    def run_forever(self, inotify) -> None:
        """Drive :meth:`changed` and :meth:`tick` from *inotify* until :meth:`stop`.

        *inotify* watches directory trees recursively (``add_tree``), blocks in ``wait``
        until something changed and returns the paths, and returns at once on ``wake``.
        """
        self._inotify = inotify
        inotify.add_tree(self.campaign_dir)
        try:
            while not self._stop.is_set():
                paths = inotify.wait(timeout=self.part_s)
                if paths:
                    self.changed(paths)
                self.tick()
        finally:
            self._inotify = None

    def stop(self) -> None:
        """Make :meth:`run_forever` return after its current step."""
        self._stop.set()
        inotify = self._inotify
        if inotify is not None:
            inotify.wake()


__all__ = ["BAG_METADATA", "Batch", "LostOwnership", "PartWriter", "Session", "VERDICT",
           "Watcher"]
