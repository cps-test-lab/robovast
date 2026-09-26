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

"""The data plane: tar streams in and out of the results tree, and a run's tables as it records.

Every byte a pod exchanges with the service goes through the routes here -- the inputs a
job extracts into ``/config``, the outputs it delivers when it is done, the scratch tree a
build or exec pod is handed -- plus the campaign download the web UI and ``vast campaign
download`` use, and the one route that is not a tar: a run's tables streamed as they are
decoded while it records (:mod:`robovast.service.live`), here because this is the process
the deliveries land in. They are a
separate FastAPI app for one reason: **bulk bytes must not share a process with the
control plane.** A dozen pods delivering gigabytes at once should slow each other down,
never the run view or the admission loop, and the way to make that structural rather than
a matter of tuning is a second process with its own limits. In the cluster Deployment
that is the ``robovast-data`` container behind an nginx front; a ``vast serve`` on a
developer's machine mounts the same routes into its one process, because there the
isolation buys nothing and a second port costs a client.

The routes are served over :class:`DataPlane`, which knows directories, tokens and tar
and not campaigns: it never imports a transport, holds no registry, and answers every
question from the tree. That is what lets the two processes share nothing but the disk
and the shared secret -- a restart of either changes what the other sees not at all.

What the plane refuses is decided here too. A campaign that is not here is a 404; there
is no route by which a pod creates one. Members a pod may never write -- the campaign's
own store, the driver's logs -- are refused per member and reported, never written
(:mod:`robovast.service.tar_io`). And a scoped token reaches exactly one campaign's or
slot's routes (:func:`robovast.service.auth.scope_allows`), which is why these are
control routes under :data:`~robovast.service.interface.Routes.DATA` rather than write
verbs under ``/results``: the namespace is the permission, and ``/results`` has none.
"""

import base64
import collections
import datetime
import functools
import json
import logging
import math
import os
import threading
import time
from pathlib import Path

from pydantic import BaseModel

from robovast.client.safe_path import UnsafePathError, check_relative, safe_join
from robovast.service.interface import OutputsIngested, Routes

logger = logging.getLogger(__name__)

#: Where the control plane stages scratch trees for pods, under the results root: one
#: place, so the two processes agree without configuration, and beside the campaigns so it
#: shares their disk and their meter. The leading underscore keeps it out of the campaign
#: listing, which recognises campaigns by name.
STAGED_DIRNAME = "_staged"

#: Names under ``_execution/`` only the driver writes. A pod delivering outputs may not
#: replace them: the driver's log is appended to for the campaign's whole life, and a
#: staged snapshot of it landing on top would truncate the record to the moment the pod
#: was given its copy.
DRIVER_OWNED = ("_execution/controller.log", "_execution/variation.log",
                "_execution/build.log")

#: How many extractions run at once in one process. Bounded by the disk, not the CPU:
#: past a handful, concurrent writers only make each other seek.
UPLOAD_WORKERS = 8

#: What the tar routes answer with. A plain tar for every stream a pod reads, a gzip one
#: only for an archive that leaves the cluster -- see
#: :mod:`robovast.execution.campaign_archive`. Uploads may be either: the reader detects it.
TAR_MEDIA_TYPE = "application/x-tar"
GZIP_MEDIA_TYPE = "application/gzip"

#: Seconds of silence after which a live stream sends a ``heartbeat`` event.
LIVE_HEARTBEAT_S = 5.0

#: Most rows one ``batch`` frame of a live stream carries; a larger batch goes out as
#: several frames, so no single frame is the size of a burst.
LIVE_FRAME_ROWS = 2000

#: How many live streams may wait on their subscriptions at once. Their own pool, as the
#: control plane's streams have, so open browser tabs cannot take every worker thread from
#: the tar routes.
LIVE_STREAMS = 64

#: How often, at most, a live stream sends a ``frame`` event per image topic.
LIVE_FRAME_S = 0.25

#: Frame indexes of finished runs kept in this process, one per ``(campaign, run, topic)``.
FRAME_INDEXES = 64

#: Disable proxy/CDN buffering so events are delivered as they are produced: the headers
#: every SSE route of the service sends.
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

_SSE_HEARTBEAT = "event: heartbeat\ndata: {}\n\n"
_SSE_EOF = "event: eof\ndata: {}\n\n"


class DataPlane:
    """The filesystem behind the data routes: one results root, read and written directly."""

    def __init__(self, results_root):
        self.results_root = Path(results_root)

    # -- addressing --

    def campaign_dir(self, campaign_id: str) -> Path:
        """The campaign's directory, which must exist. ``KeyError`` when it does not."""
        from robovast.common.execution import is_campaign_dir  # pylint: disable=import-outside-toplevel
        if "/" in campaign_id or not is_campaign_dir(campaign_id):
            raise KeyError(f"no campaign {campaign_id!r} on this service")
        path = self.results_root / campaign_id
        if not path.is_dir():
            raise KeyError(f"no campaign {campaign_id!r} on this service")
        return path

    def staged_dir(self, slot: str) -> Path:
        """Where the slot *slot* is, or would be, staged. Confined under the results root."""
        try:
            return safe_join(self.results_root / STAGED_DIRNAME, slot)
        except UnsafePathError as e:
            raise ValueError(f"not a staged slot: {slot!r}") from e

    # -- campaign liveness, from the tree --

    def campaign_is_finished(self, campaign_id: str) -> bool:
        """Whether the campaign's terminal record is written.

        The one fact the tree carries about liveness: the driver writes
        ``_execution/outcome.json`` on every terminal path. Read here rather than asked of
        the control plane, so the two processes share nothing but the disk.
        """
        from robovast.client.status import is_terminal  # pylint: disable=import-outside-toplevel
        from robovast.common.campaign_data import read_execution_outcome  # pylint: disable=import-outside-toplevel
        try:
            status = read_execution_outcome(self.campaign_dir(campaign_id))
        except Exception:  # noqa: BLE001 - an unreadable record is "not over", the safe side
            return False
        return status is not None and is_terminal(status.phase)

    def campaign_archive_name(self, campaign_id: str) -> str:
        from robovast.execution.share_providers.naming import \
            INCOMPLETE, archive_name  # pylint: disable=import-outside-toplevel
        if not self.campaign_is_finished(campaign_id):
            return archive_name(campaign_id, INCOMPLETE)
        return f"{campaign_id}.tar.gz"

    # -- the five operations --

    def campaign_tar_stream(self, campaign_id: str, *, live: "bool | None" = None,
                            facts: "dict | None" = None):
        """The campaign as a tar stream; see the interface method of the same name.

        *live* is whether the campaign is still being written -- a caller that knows says
        so and gives the *facts* the snapshot marker records; left ``None`` the tree
        decides, and a campaign without a terminal record is marked with what the tree
        can say, which is only that it is not over.
        """
        from robovast.execution import campaign_archive  # pylint: disable=import-outside-toplevel
        campaign_dir = self.campaign_dir(campaign_id)
        if live is None:
            live = not self.campaign_is_finished(campaign_id)
        snapshot = dict(facts or {}) if live else None
        return campaign_archive.iter_campaign_tar(
            str(campaign_dir), exclude=campaign_archive.DEFAULT_EXCLUDE, snapshot=snapshot)

    def campaign_inputs_tar_stream(self, campaign_id: str, job_tags: "list[str]",
                                   config_files: "list[tuple[str, str]] | None" = None):
        from robovast.execution import campaign_archive  # pylint: disable=import-outside-toplevel
        campaign_dir = self.campaign_dir(campaign_id)
        for _config_name, rel in config_files or ():
            check_relative(rel)
        return campaign_archive.iter_inputs_tar(str(campaign_dir), job_tags, config_files)

    def ingest_campaign_outputs(self, campaign_id: str, stream) -> OutputsIngested:
        """Extract *stream* into the campaign. A campaign that is over still takes it.

        A stop tears pods down while their uploaders are flushing, and what they flush is
        that run's evidence -- the verdict was drawn from what had landed, and what lands
        after it is the rest of the same record. What the tree never accepts is decided per
        member, not per campaign.
        """
        from robovast.service import tar_io  # pylint: disable=import-outside-toplevel
        campaign_dir = self.campaign_dir(campaign_id)
        result = tar_io.extract_stream(stream, campaign_dir, deny=DRIVER_OWNED)
        if result.refused:
            logger.warning("outputs for %s: refused %d member(s): %s", campaign_id,
                           len(result.refused), ", ".join(result.refused[:5]))
        if result.resync:
            # A range that does not continue the file here is the sender's to repair by
            # sending the file whole; routine after a restart, not a fault.
            logger.info("outputs for %s: resync %d file(s): %s", campaign_id,
                        len(result.resync), ", ".join(result.resync[:5]))
        return OutputsIngested(files=result.files, bytes=result.bytes,
                               refused=result.refused, resync=result.resync)

    def staged_tar_stream(self, slot: str, path: str = ""):
        from robovast.execution import campaign_archive  # pylint: disable=import-outside-toplevel
        root = self.staged_dir(slot)
        if path:
            root = safe_join(root, path)
        if not root.is_dir():
            raise KeyError(f"nothing staged at {slot!r}" + (f"/{path}" if path else ""))
        return campaign_archive.iter_tree_tar(str(root))

    def export_tar_stream(self, campaign_id: str, export_id: str):
        """A finished export's tarball, read from the export's directory; see the interface."""
        from robovast.service import exports  # pylint: disable=import-outside-toplevel
        campaign_dir = self.campaign_dir(campaign_id)
        return exports.iter_file(exports.export_file(campaign_dir, campaign_id, export_id))

    def ingest_staged(self, slot: str, stream) -> OutputsIngested:
        from robovast.service import tar_io  # pylint: disable=import-outside-toplevel
        root = self.staged_dir(slot)
        root.mkdir(parents=True, exist_ok=True)
        result = tar_io.extract_stream(stream, root)
        if result.resync:
            logger.info("staged %s: resync %d file(s): %s", slot,
                        len(result.resync), ", ".join(result.resync[:5]))
        return OutputsIngested(files=result.files, bytes=result.bytes,
                               refused=result.refused, resync=result.resync)

    def discard_staged(self, slot: str) -> bool:
        """Remove the slot's tree; ``False`` when there was none."""
        import shutil  # pylint: disable=import-outside-toplevel
        root = self.staged_dir(slot)
        if not root.exists():
            return False
        shutil.rmtree(root, ignore_errors=True)
        return True

    # -- a run as it records --

    def campaign_live(self, campaign_id: str, run: str, tables):
        """A subscription to *run*'s *tables* as it records (:mod:`robovast.service.live`).

        *run* is ``<config>/<run_id>``. ``KeyError`` for a campaign or run that is not here,
        ``ValueError`` for a run key or table list that is not one. A run that is not live
        gets a subscription at its end already: its rows are all there for a query. The
        watchers behind it are one set per results root in this process, whichever plane
        object asks.
        """
        from robovast.service.live import LiveCampaigns  # pylint: disable=import-outside-toplevel
        campaign_dir = self.campaign_dir(campaign_id)
        return LiveCampaigns.for_root(self.results_root).subscribe(campaign_dir.name, run, tables)

    # -- a run's camera frames --

    def campaign_frame(self, campaign_id: str, run: str, topic: str,
                       t: "float | None" = None) -> "tuple[float, bytes]":
        """``(stamp, JPEG)`` of the frame of *topic* nearest at or before *t* of *run*.

        The newest frame without *t*. A live run answers from the watcher tapping its
        recording (:meth:`LiveCampaigns.newest_frame` for the newest, its index for a
        moment); a finished run from a :class:`~robovast_decode.frames.FrameIndex` built
        on first request and kept per ``(campaign, run, topic)``. ``KeyError`` for a
        campaign or run that is not here, a run without the topic, and a topic with no
        frame yet; ``ValueError`` for a run key that is not one.
        """
        frames = self._frames(campaign_id, run, topic)
        if t is None:
            newest = frames.newest_frame()
            if newest is None:
                raise KeyError(f"no frame of {topic} in run {run!r} yet")
            return newest
        ref = frames.nearest(t)
        if ref is None:
            raise KeyError(f"no frame of {topic} in run {run!r} yet")
        return ref.t, frames.read_frame(ref)

    def campaign_frame_index(self, campaign_id: str, run: str, topic: str) -> "list[float]":
        """The stamp of every frame of *topic* of *run*, in recording order.

        The same sources as :meth:`campaign_frame`; ``KeyError`` for a run without the
        topic, and an empty list for one with the topic and no frame yet.
        """
        return self._frames(campaign_id, run, topic).times

    def campaign_frame_full(self, campaign_id: str, run: str, topic: str,
                           t: "float | None" = None) -> "tuple[float, str, str, str, bytes]":
        """``(stamp, encoding, frame_id, media type, payload)`` of the frame at or before *t*,
        whole: what analysis reads, where :meth:`campaign_frame` is a viewer's preview.

        A raw ``Image`` is its pixels in numpy's ``.npy`` format, in the encoding's own dtype
        (``application/x-npy``); a ``CompressedImage`` is its bytes as recorded, JPEG or PNG.
        The same sources and errors as :meth:`campaign_frame`.
        """
        import io  # pylint: disable=import-outside-toplevel

        import numpy as np  # pylint: disable=import-outside-toplevel
        from robovast_decode import images  # pylint: disable=import-outside-toplevel
        frames = self._frames(campaign_id, run, topic)
        ref = frames.nearest(t)
        if ref is None:
            raise KeyError(f"no frame of {topic} in run {run!r} yet")
        msg = frames.read_message(ref)
        frame_id = str(getattr(getattr(msg, "header", None), "frame_id", ""))
        if frames.typename == "sensor_msgs/msg/CompressedImage":
            fmt = (msg.format or "").lower()
            media = ("image/jpeg" if "jpeg" in fmt or "jpg" in fmt
                     else "image/png" if "png" in fmt else "application/octet-stream")
            return ref.t, str(msg.format), frame_id, media, bytes(msg.data)
        pixels, encoding = images.decode(msg, frames.typename)
        out = io.BytesIO()
        np.save(out, pixels, allow_pickle=False)
        return ref.t, encoding, frame_id, "application/x-npy", out.getvalue()

    def campaign_points(self, campaign_id: str, run: str, topic: str,
                        t: "float | None" = None, after: bool = False
                        ) -> "tuple[float, str, bytes]":
        """``(stamp, frame_id, Arrow IPC stream)`` of the point cloud of *topic* at or before
        *t* (the last without it), or with *after* the first strictly after *t* (the first
        of the run without it). One column per field of the cloud, a field of several
        values per point as a fixed-size list. ``KeyError`` for a run or topic that is not
        here, a topic that is not a point cloud, and a step past the last cloud.
        """
        import io  # pylint: disable=import-outside-toplevel

        import pyarrow as pa  # pylint: disable=import-outside-toplevel
        from robovast_decode import points  # pylint: disable=import-outside-toplevel
        from robovast_decode.bulk import nearest_message  # pylint: disable=import-outside-toplevel
        sample = nearest_message(self._recording(campaign_id, run), topic, t, after=after)
        if sample is None:
            raise KeyError(f"no point cloud of {topic} in run {run!r}"
                           + (f" after {t:g} s" if after and t is not None else ""))
        if sample.typename not in points.POINT_CLOUD_TYPES:
            raise KeyError(f"{topic} of run {run!r} carries {sample.typename}, not a point cloud")
        fields = points.decode(sample.msg, sample.typename)
        columns = {}
        for name, values in fields.items():
            if values.ndim == 1:
                columns[name] = pa.array(values)
            else:
                columns[name] = pa.FixedSizeListArray.from_arrays(
                    pa.array(values.reshape(-1)), values.shape[1])
        table = pa.table(columns)
        out = io.BytesIO()
        with pa.ipc.new_stream(out, table.schema) as writer:
            writer.write_table(table)
        frame_id = str(getattr(getattr(sample.msg, "header", None), "frame_id", ""))
        return sample.t, frame_id, out.getvalue()

    def _recording(self, campaign_id: str, run: str) -> str:
        """The scenario recording of *run*; ``KeyError`` for a run that is not here or has
        none."""
        from robovast.service.live import parse_run  # pylint: disable=import-outside-toplevel
        from robovast_decode.build import find_runs, scenario_recording  # pylint: disable=import-outside-toplevel
        campaign_dir = self.campaign_dir(campaign_id)
        config_name, run_id = parse_run(run)
        key = f"{config_name}/{run_id}"
        match = [r for r in find_runs(str(campaign_dir)) if r.key == key]
        bag_dir = scenario_recording(match[0]) if match else None
        if bag_dir is None:
            raise KeyError(f"run {run!r} of campaign {campaign_id!r} has no scenario recording")
        return bag_dir

    def _frames(self, campaign_id: str, run: str, topic: str):
        """The :class:`~robovast_decode.frames.Frames` of *topic* of *run*: the watcher's tap
        while the run is live, a kept index once it is not."""
        from robovast.service.live import LiveCampaigns, parse_run  # pylint: disable=import-outside-toplevel
        from robovast_decode.runs import is_live  # pylint: disable=import-outside-toplevel
        campaign_dir = self.campaign_dir(campaign_id)
        config_name, run_id = parse_run(run)
        if not topic:
            raise ValueError("topic names the image topic to read")
        if not (campaign_dir / config_name / str(run_id)).is_dir():
            raise KeyError(f"no run {run!r} in campaign {campaign_id!r}")
        key = f"{config_name}/{run_id}"
        if is_live(str(campaign_dir), config_name, run_id):
            tap = LiveCampaigns.for_root(self.results_root).frame_index(campaign_id, key, topic)
            if tap is None:
                raise KeyError(f"no frame of {topic} in run {run!r} yet: its recording has "
                               "not started")
            return tap
        return _frame_index(campaign_dir, key, topic)


_indexes: "collections.OrderedDict" = collections.OrderedDict()
_indexes_lock = threading.Lock()


def _frame_index(campaign_dir: Path, run: str, topic: str):
    """The index of *topic* of the finished run *run*, built once per process and kept.

    Bounded to :data:`FRAME_INDEXES`, least recently asked for first out. The index is
    extended on every request, which costs nothing on a closed recording and follows one
    a run left open. ``KeyError`` when the run's scenario recording does not carry the
    topic.
    """
    from robovast_decode.build import find_runs, scenario_recording  # pylint: disable=import-outside-toplevel
    from robovast_decode.frames import IMAGE_TYPES, FrameIndex  # pylint: disable=import-outside-toplevel
    key = (str(campaign_dir), run, topic)
    with _indexes_lock:
        index = _indexes.get(key)
        if index is not None:
            _indexes.move_to_end(key)
    if index is None:
        match = [r for r in find_runs(str(campaign_dir)) if r.key == run]
        bag_dir = scenario_recording(match[0]) if match else None
        if bag_dir is None:
            raise KeyError(f"run {run!r} has no scenario recording")
        index = FrameIndex(bag_dir, topic)
        if index.typename is None:
            raise KeyError(f"run {run!r} recorded no topic {topic}")
        if index.typename not in IMAGE_TYPES:
            raise KeyError(f"{topic} of run {run!r} carries {index.typename}, not an image")
        with _indexes_lock:
            _indexes[key] = index
            while len(_indexes) > FRAME_INDEXES:
                _indexes.popitem(last=False)
    else:
        index.extend()
    return index


class FrameTimes(BaseModel):
    """The stamps of every frame of one image topic of a run, in seconds of the run's clock."""
    topic: str
    times: list[float]


def data_router(source):
    """The four data routes plus the archive, over *source*.

    *source* is whatever answers the five interface methods -- a :class:`DataPlane` in
    the standalone process, the control plane's transport when the routes are mounted
    into ``vast serve``'s one app. Both spell the same paths (``Routes``), so a pod, the
    web UI and the CLI reach them at one address whichever process answers.
    """
    import anyio  # pylint: disable=import-outside-toplevel
    from fastapi import APIRouter, HTTPException, Query, Request  # pylint: disable=import-outside-toplevel
    from fastapi.responses import Response, StreamingResponse  # pylint: disable=import-outside-toplevel

    from robovast.common.errors import (  # pylint: disable=import-outside-toplevel
        STORAGE_FULL_DETAIL, is_storage_full)
    from robovast.service.tar_io import StreamReader  # pylint: disable=import-outside-toplevel

    router = APIRouter(tags=["data"])
    limiter = anyio.CapacityLimiter(UPLOAD_WORKERS)

    def _guard(fn):
        try:
            return fn()
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e.args[0]) if e.args else str(e)) from e
        except (ValueError, UnsafePathError) as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    async def _ingest(request: Request, take):
        """Stream the request body into *take* on a worker thread, bounded in memory.

        Reading and writing are two threads joined by a bounded queue: the body is read
        on the event loop as it arrives, and the extraction pulls from the queue, so a
        slow disk holds the socket back instead of the body piling up in memory. The
        worker slot is taken under *limiter* so a burst of uploads queues rather than
        starving whatever else this process serves.
        """
        reader = StreamReader()

        async def _pump():
            try:
                async for chunk in request.stream():
                    await anyio.to_thread.run_sync(reader.push, chunk)
            finally:
                reader.finish()

        result = None
        error = None

        async def _extract():
            nonlocal result, error
            try:
                result = await anyio.to_thread.run_sync(lambda: _guard(lambda: take(reader)),
                                                        limiter=limiter)
            except BaseException as e:  # noqa: BLE001 - reported below, once
                error = e
                # Drain what the pump still sends, or it blocks on a full queue forever.
                await anyio.to_thread.run_sync(lambda: _drain(reader))

        async with anyio.create_task_group() as tg:
            tg.start_soon(_extract)
            tg.start_soon(_pump)
        if error is not None:
            if isinstance(error, HTTPException):
                raise error
            # Three answers a sender acts on differently. A full disk is 507 -- the request
            # is fine, and it lands once space is freed. A stream that is not a readable tar
            # is 400 -- sending it again changes nothing. Any other failure to write is the
            # service's, and 500 says so: an uploader retries it, where a 400 would have it
            # give up on output that was never the problem.
            if is_storage_full(error):
                raise HTTPException(status_code=507, detail=STORAGE_FULL_DETAIL) from error
            if _is_unreadable_upload(error):
                raise HTTPException(status_code=400,
                                    detail=f"the upload is not a readable tar: {error}") from error
            if isinstance(error, OSError):
                raise HTTPException(
                    status_code=500,
                    detail=f"the service could not write the upload: {error}") from error
            raise error
        return result

    @router.get(Routes.campaign_archive("{campaign_id}"))
    def download_campaign_archive(campaign_id: str):
        """Stream the campaign as a ``tar.gz``.

        Backs ``vast campaign download`` and the web UI's download button. What comes out
        is the campaign's records as this service holds them -- postprocessed if it has
        been, raw if it has not -- never its table cache: derived data is an addition to a
        campaign, never the condition for reading one.

        Nothing is buffered and no scratch is used: the tree is tarred into the response
        as it is read. Decisive for campaigns that run to terabytes.
        """
        # The name before the stream: a running campaign is offered as
        # `<id>.incomplete.tar.gz`, and the header is the only place that reaches a browser
        # -- which saves whatever this says and never sees the marker inside the archive.
        name = _guard(lambda: source.campaign_archive_name(campaign_id))
        return StreamingResponse(
            _guard(lambda: source.campaign_tar_stream(campaign_id)),
            media_type=GZIP_MEDIA_TYPE,
            headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @router.get(Routes.campaign_export_download("{campaign_id}", "{export_id}"))
    def download_campaign_export(campaign_id: str, export_id: str):
        """Stream a finished export as a ``tar.gz``.

        Backs ``vast campaign export`` and the web UI's Export dialog. A 404 until the
        export is done (the status route on the control plane says how far it is), a 409
        carrying the reason once it failed. Answered from the export's files alone, so the
        standalone data container serves what the control plane built.
        """
        from robovast.service.exports import export_file_name  # pylint: disable=import-outside-toplevel
        try:
            stream = _guard(lambda: source.export_tar_stream(campaign_id, export_id))
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        return StreamingResponse(
            stream, media_type=GZIP_MEDIA_TYPE,
            headers={"Content-Disposition":
                     f'attachment; filename="{export_file_name(campaign_id, export_id)}"'})

    @router.get(Routes.campaign_inputs("{campaign_id}"))
    def download_campaign_inputs(campaign_id: str,
                                 job: list[str] = Query(),
                                 config_file: list[str] = Query(default=[])):
        """Stream the tar a job pod extracts into its ``/config``.

        ``job`` names the tag whose documents the pod reads, repeated once per tag; every
        other job's are left out. ``config_file`` names a cell's own input as
        ``<config_name>:<rel>``, repeated once per file; each lands at ``<rel>`` on top of
        the campaign's copy.
        """
        pairs = []
        for item in config_file:
            config_name, sep, rel = item.partition(":")
            if not sep or not config_name or not rel:
                raise HTTPException(status_code=400,
                                    detail=f"config_file must be <config_name>:<rel>, got {item!r}")
            pairs.append((config_name, rel))
        return StreamingResponse(
            _guard(lambda: source.campaign_inputs_tar_stream(campaign_id, job, pairs)),
            media_type=TAR_MEDIA_TYPE)

    @router.put(Routes.campaign_outputs("{campaign_id}"), response_model=OutputsIngested)
    async def upload_campaign_outputs(campaign_id: str, request: Request) -> OutputsIngested:
        """Take a tar of run outputs into the campaign. Streamed, never buffered.

        A campaign that is not here is a 404, reached before any member is written: the
        source resolves the directory before it reads the stream.
        """
        return await _ingest(request,
                             lambda reader: source.ingest_campaign_outputs(campaign_id, reader))

    @router.get(Routes.staged("{slot:path}"))
    def download_staged(slot: str, path: str = ""):
        """Stream a staged slot, or *path* within it, as a plain tar."""
        return StreamingResponse(
            _guard(lambda: source.staged_tar_stream(slot, path)),
            media_type=TAR_MEDIA_TYPE)

    @router.put(Routes.staged("{slot:path}"), response_model=OutputsIngested)
    async def upload_staged(slot: str, request: Request) -> OutputsIngested:
        """Take a tar into the staged slot *slot*, creating it."""
        return await _ingest(request, lambda reader: source.ingest_staged(slot, reader))

    live_limiter = anyio.CapacityLimiter(LIVE_STREAMS)

    async def _sse_live(request: Request, campaign_id: str, run: str, tables: str,
                        frames: str = ""):
        """SSE generator over a run's tables as they are decoded (``campaign_live``).

        The subscription is taken on a worker thread, and each wait on it too, bounded by
        :data:`LIVE_HEARTBEAT_S` so a client that went away is noticed within that and a
        quiet run keeps sending heartbeats. A batch is turned into frames off the loop as
        well: a big one is thousands of rows of Python objects to encode. With image
        *frames* to follow, the wait is bounded by :data:`LIVE_FRAME_S` instead and every
        wake looks for a newer frame of each topic (``campaign_frame``), sent when its
        stamp changed; the heartbeat still comes after :data:`LIVE_HEARTBEAT_S` of nothing
        sent.
        """
        from robovast.service.live import EOF, Dropped  # pylint: disable=import-outside-toplevel
        yield ": open\n\n"
        names = [t.strip() for t in tables.split(",")]
        topics = [t.strip() for t in frames.split(",") if t.strip()]
        try:
            subscription = await anyio.to_thread.run_sync(
                lambda: source.campaign_live(campaign_id, run, names), limiter=live_limiter)
        except (KeyError, ValueError, UnsafePathError) as exc:
            yield _sse_error(str(exc.args[0]) if exc.args else str(exc))
            yield _SSE_EOF
            return
        sent: dict = {}
        quiet_since = time.monotonic()
        wait_s = min(LIVE_HEARTBEAT_S, LIVE_FRAME_S) if topics else LIVE_HEARTBEAT_S
        try:
            while True:
                if await request.is_disconnected():
                    return
                try:
                    item = await anyio.to_thread.run_sync(
                        subscription.next, wait_s, abandon_on_cancel=True,
                        limiter=live_limiter)
                except Dropped as exc:
                    yield _sse_error(str(exc))
                    yield _SSE_EOF
                    return
                if item is EOF:
                    yield _SSE_EOF
                    return
                if item is not None:
                    for frame in await anyio.to_thread.run_sync(
                            functools.partial(_live_frames, item), limiter=live_limiter):
                        yield frame
                    quiet_since = time.monotonic()
                for topic in topics:
                    event = await anyio.to_thread.run_sync(
                        functools.partial(_frame_event, source, campaign_id, run, topic, sent),
                        limiter=live_limiter)
                    if event:
                        yield event
                        quiet_since = time.monotonic()
                if time.monotonic() - quiet_since >= LIVE_HEARTBEAT_S:
                    yield _SSE_HEARTBEAT
                    quiet_since = time.monotonic()
        finally:
            subscription.close()

    @router.get(Routes.campaign_live("{campaign_id}"))
    async def stream_campaign_live(
            request: Request, campaign_id: str,
            run: str = Query(description="the run, as <config>/<run_id>"),
            tables: str = Query(description="the tables to follow, comma-separated"),
            frames: str = Query(default="", description="image topics whose newest frame "
                                "to send as it changes, comma-separated")):
        """Stream a run's tables as they are decoded while it records, as server-sent events.

        A ``batch`` event carries ``{"table": name, "rows": [...]}`` -- at most
        :data:`LIVE_FRAME_ROWS` rows, so one decoded batch may be several events -- and
        a table's batches add up to what a query of the finished run gives. A table the
        run's recordings do not carry yet is followed from the moment a topic that gives it
        appears. With ``frames``, a ``frame`` event per named image topic carries
        ``{"topic", "t", "jpeg_base64"}``, the newest frame, at most every
        :data:`LIVE_FRAME_S` seconds and only while it changes. ``heartbeat`` after
        :data:`LIVE_HEARTBEAT_S` seconds of silence. ``eof`` once the run has its verdict
        and its recordings are closed and read to their end; at once for a run that is not
        live, since its rows are all there for a query. ``streamerror`` then ``eof`` for a
        campaign or run that is not here, a run key or table list that is not one, and a
        client that fell :data:`~robovast.service.live.QUEUE_MAX` batches behind, which is
        dropped rather than buffered without bound. A non-finite float is ``null`` in a
        row, a timestamp its ISO text, and bytes base64.
        """
        return StreamingResponse(_sse_live(request, campaign_id, run, tables, frames),
                                 media_type="text/event-stream", headers=SSE_HEADERS)

    @router.get(Routes.campaign_frame("{campaign_id}"), response_class=Response,
                responses={200: {"content": {"image/jpeg": {}, "image/png": {},
                                             "application/x-npy": {}}}, 404: {}})
    def get_campaign_frame(
            campaign_id: str,
            run: str = Query(description="the run, as <config>/<run_id>"),
            topic: str = Query(description="the image topic"),
            t: "float | None" = Query(default=None, description="a moment in seconds of "
                                      "the run's clock; the newest frame without it"),
            full: bool = Query(default=False, description="the whole frame instead of the "
                               "JPEG preview: a raw image as its pixels in numpy's .npy "
                               "format, a compressed one as recorded")):
        """One camera frame of a run: as ``image/jpeg`` no wider than 640 px, or whole.

        The last frame at or before ``t``, the first when none is; the newest without
        ``t``. Its stamp, in the seconds every table of the run uses, is the
        ``X-Frame-Time`` header. With ``full`` the frame is what the camera produced --
        ``application/x-npy`` holding the pixels in the encoding's own dtype (the encoding
        in ``X-Frame-Encoding``), or a compressed image's own bytes -- and its frame is
        ``X-Frame-Id``. A live run's frame comes from the watcher following its recording,
        a finished run's from an index built on first request. ``404`` for a run without
        the topic or with no frame of it yet, with the reason.
        """
        if full:
            stamp, encoding, frame_id, media, payload = _guard(
                lambda: source.campaign_frame_full(campaign_id, run, topic, t))
            return Response(content=payload, media_type=media,
                            headers={"X-Frame-Time": repr(float(stamp)),
                                     "X-Frame-Encoding": encoding, "X-Frame-Id": frame_id,
                                     "Cache-Control": "no-cache"})
        stamp, jpeg = _guard(lambda: source.campaign_frame(campaign_id, run, topic, t))
        return Response(content=jpeg, media_type="image/jpeg",
                        headers={"X-Frame-Time": repr(float(stamp)),
                                 "Cache-Control": "no-cache"})

    @router.get(Routes.campaign_points("{campaign_id}"), response_class=Response,
                responses={200: {"content": {"application/vnd.apache.arrow.stream": {}}},
                           404: {}})
    def get_campaign_points(
            campaign_id: str,
            run: str = Query(description="the run, as <config>/<run_id>"),
            topic: str = Query(description="the point cloud topic"),
            t: "float | None" = Query(default=None, description="a moment in seconds of "
                                      "the run's clock; the last cloud without it"),
            after: bool = Query(default=False, description="the first cloud strictly after "
                                "t (the run's first without t), to step through the topic")):
        """One point cloud of a run as an Arrow IPC stream, one column per field.

        The cloud at or before ``t`` (the last without it), or with ``after`` the first one
        after ``t``. Its stamp is ``X-Frame-Time``, its frame ``X-Frame-Id``. ``404`` for a
        run or topic that is not here, a topic that is not a point cloud, and a step past
        the last cloud, with the reason.
        """
        stamp, frame_id, payload = _guard(
            lambda: source.campaign_points(campaign_id, run, topic, t, after))
        return Response(content=payload, media_type="application/vnd.apache.arrow.stream",
                        headers={"X-Frame-Time": repr(float(stamp)), "X-Frame-Id": frame_id,
                                 "Cache-Control": "no-cache"})

    @router.get(Routes.campaign_frame_index("{campaign_id}"), response_model=FrameTimes)
    def get_campaign_frame_index(
            campaign_id: str,
            run: str = Query(description="the run, as <config>/<run_id>"),
            topic: str = Query(description="the image topic")) -> FrameTimes:
        """The stamp of every frame of a run's image topic, in recording order.

        What a scrubber steps through: the moments ``GET .../frame?t=`` answers exactly.
        The same sources as the frame route; ``404`` for a run without the topic.
        """
        times = _guard(lambda: source.campaign_frame_index(campaign_id, run, topic))
        return FrameTimes(topic=topic, times=list(times))

    return router


def _frame_event(source, campaign_id: str, run: str, topic: str, sent: dict) -> str:
    """The ``frame`` event for *topic* when its newest frame is newer than the one *sent*,
    else ``""``. A topic the run has no frame of yet is nothing to send, not an error."""
    try:
        stamp, jpeg = source.campaign_frame(campaign_id, run, topic, None)
    except KeyError:
        return ""
    if sent.get(topic) == stamp:
        return ""
    sent[topic] = stamp
    payload = {"topic": topic, "t": stamp, "jpeg_base64": base64.b64encode(jpeg).decode("ascii")}
    return f"event: frame\ndata: {json.dumps(payload)}\n\n"


def _sse_error(message: str) -> str:
    return f"event: streamerror\ndata: {json.dumps(message)}\n\n"


def _json_default(value):
    """JSON for what a table row holds beside numbers and text."""
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    return str(value)


def _json_row(row: dict) -> dict:
    """*row* with its non-finite floats as ``None``: JSON has no spelling for them that a
    browser's parser takes."""
    return {key: (None if isinstance(value, float) and not math.isfinite(value) else value)
            for key, value in row.items()}


def _live_frames(batch) -> "list[str]":
    """The ``batch`` events one decoded batch goes out as, :data:`LIVE_FRAME_ROWS` rows each."""
    rows = batch.rows.to_pylist()
    frames = []
    for start in range(0, len(rows), LIVE_FRAME_ROWS):
        payload = {"table": batch.table,
                   "rows": [_json_row(r) for r in rows[start:start + LIVE_FRAME_ROWS]]}
        frames.append(f"event: batch\ndata: {json.dumps(payload, default=_json_default)}\n\n")
    return frames


def _drain(reader) -> None:
    while reader.read(1 << 20):
        pass


def _is_unreadable_upload(error: BaseException) -> bool:
    """Whether *error* says the uploaded bytes are not a tar this route can read.

    A stream cut short surfaces here too, as a tar that ends mid-member. gzip's own error is
    an ``OSError`` subclass, which is why it is named rather than left to the ``OSError``
    every failure to write also is.
    """
    import gzip  # pylint: disable=import-outside-toplevel
    import tarfile  # pylint: disable=import-outside-toplevel
    import zlib  # pylint: disable=import-outside-toplevel
    return isinstance(error, (tarfile.TarError, EOFError, gzip.BadGzipFile, zlib.error))


def build_data_app(results_root, auth_token: "str | None" = None):
    """The standalone data-plane app: the routes over a :class:`DataPlane`, gated.

    The same gate as the control plane (:class:`~robovast.service.auth.AuthMiddleware`)
    with the same secret, so a scoped token minted there verifies here with nothing
    shared but the secret. ``/healthz`` is the one public route, for the container's
    probe.
    """
    from fastapi import FastAPI  # pylint: disable=import-outside-toplevel

    from robovast.service import auth  # pylint: disable=import-outside-toplevel

    token, _ephemeral = auth.resolve_token(auth_token)
    app = FastAPI(title="robovast-data", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.auth_token = token
    plane = DataPlane(results_root)
    app.state.data_plane = plane

    @app.get(Routes.HEALTHZ)
    def healthz():
        return {"status": "ok", "results_root": str(plane.results_root)}

    app.include_router(data_router(plane))
    app.add_middleware(auth.AuthMiddleware, token=token)
    return app


def serve_data(results_root, *, uds: "str | None" = None, host: str = "127.0.0.1",
               port: "int | None" = None, log_level: str = "info") -> None:
    """Run the standalone data plane in the foreground (blocking) via uvicorn.

    Listens on a Unix socket (*uds*) behind a front, or on *host*:*port* by itself. The
    secret is :data:`~robovast.service.auth.TOKEN_ENV_VAR`, exactly as for ``vast
    serve``; a data plane with no configured secret would mint one nobody else knows,
    so it refuses to start rather than answer no caller.
    """
    import uvicorn  # pylint: disable=import-outside-toplevel

    from robovast.service import auth  # pylint: disable=import-outside-toplevel

    configured = os.environ.get(auth.TOKEN_ENV_VAR, "").strip()
    if not configured:
        raise ValueError(f"{auth.TOKEN_ENV_VAR} is not set: the data plane verifies the "
                         "control plane's tokens, so it must be given the same secret")
    app = build_data_app(results_root, configured)
    if uds:
        logger.info("robovast-data listening on %s (results at %s)", uds, results_root)
        config = uvicorn.Config(app, uds=uds, log_level=log_level, timeout_graceful_shutdown=5)
    else:
        if port is None:
            raise ValueError("either --uds or --port is required")
        logger.info("robovast-data listening on %s:%d (results at %s)", host, port, results_root)
        config = uvicorn.Config(app, host=host, port=port, log_level=log_level,
                                timeout_graceful_shutdown=5)
    uvicorn.Server(config).run()
