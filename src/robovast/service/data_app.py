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

"""The data plane: tar streams in and out of the results tree, and nothing else.

Every byte a pod exchanges with the service goes through the four routes here -- the
inputs a job extracts into ``/config``, the outputs it delivers when it is done, the
campaign a postprocessing pod stages, the scratch tree a build or exec pod is handed --
plus the campaign download the web UI and ``vast campaign download`` use. They are a
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

import logging
import os
from pathlib import Path

from robovast.client.safe_path import UnsafePathError, check_relative, safe_join
from robovast.service.interface import ArchiveSelection, OutputsIngested, Routes

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

    def campaign_tar_stream(self, campaign_id: str, selection: "ArchiveSelection | None" = None,
                            *, live: "bool | None" = None, facts: "dict | None" = None):
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
        include = None
        if selection is not None and (selection.stage or selection.skip_bags
                                      or selection.batch_jobs or selection.part):
            include = campaign_archive.stage_include(skip_bags=selection.skip_bags,
                                                     batch_jobs=selection.batch_jobs)
            if selection.part:
                staged = include
                in_part = campaign_archive.part_include(str(campaign_dir), selection.part)

                def include(rel, is_dir):  # pylint: disable=function-redefined
                    return staged(rel, is_dir) and in_part(rel, is_dir)
        return campaign_archive.iter_campaign_tar(
            str(campaign_dir),
            exclude=campaign_archive.DEFAULT_EXCLUDE | {"_postproc"},
            snapshot=snapshot, include=include,
            compress=not (selection is not None and selection.uncompressed))

    def campaign_inputs_tar_stream(self, campaign_id: str,
                                   config_files: "list[tuple[str, str]] | None" = None):
        from robovast.execution import campaign_archive  # pylint: disable=import-outside-toplevel
        campaign_dir = self.campaign_dir(campaign_id)
        for _config_name, rel in config_files or ():
            check_relative(rel)
        return campaign_archive.iter_inputs_tar(str(campaign_dir), config_files)

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
        return OutputsIngested(files=result.files, bytes=result.bytes,
                               refused=result.refused)

    def staged_tar_stream(self, slot: str, path: str = ""):
        from robovast.execution import campaign_archive  # pylint: disable=import-outside-toplevel
        root = self.staged_dir(slot)
        if path:
            root = safe_join(root, path)
        if not root.is_dir():
            raise KeyError(f"nothing staged at {slot!r}" + (f"/{path}" if path else ""))
        return campaign_archive.iter_tree_tar(str(root))

    def ingest_staged(self, slot: str, stream) -> OutputsIngested:
        from robovast.service import tar_io  # pylint: disable=import-outside-toplevel
        root = self.staged_dir(slot)
        root.mkdir(parents=True, exist_ok=True)
        result = tar_io.extract_stream(stream, root)
        return OutputsIngested(files=result.files, bytes=result.bytes,
                               refused=result.refused)

    def discard_staged(self, slot: str) -> bool:
        """Remove the slot's tree; ``False`` when there was none."""
        import shutil  # pylint: disable=import-outside-toplevel
        root = self.staged_dir(slot)
        if not root.exists():
            return False
        shutil.rmtree(root, ignore_errors=True)
        return True


def data_router(source):
    """The four data routes plus the archive, over *source*.

    *source* is whatever answers the five interface methods -- a :class:`DataPlane` in
    the standalone process, the control plane's transport when the routes are mounted
    into ``vast serve``'s one app. Both spell the same paths (``Routes``), so a pod, the
    web UI and the CLI reach them at one address whichever process answers.
    """
    import anyio  # pylint: disable=import-outside-toplevel
    from fastapi import APIRouter, HTTPException, Query, Request  # pylint: disable=import-outside-toplevel
    from fastapi.responses import StreamingResponse  # pylint: disable=import-outside-toplevel

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
    def download_campaign_archive(campaign_id: str, stage: bool = False,
                                  skip_bags: bool = False, batch_jobs: str = "",
                                  uncompressed: bool = False, part: str = ""):
        """Stream the campaign as a ``tar.gz``, or a plain tar with ``uncompressed``.

        Backs ``vast campaign download``, the web UI's download button and the
        postprocessing pod's stage, which asks for the plain tar, being in the cluster.
        What comes out is the campaign as this service holds
        it -- postprocessed if it has been, raw if it has not; derived data is an addition
        to a campaign, never the condition for reading one. ``stage``, ``skip_bags``,
        ``batch_jobs`` and ``part`` narrow it to what a postprocessing pod reads
        (:class:`ArchiveSelection`).

        Nothing is buffered and no scratch is used: the tree is tarred into the response
        as it is read. Decisive for campaigns that run to terabytes.
        """
        selection = ArchiveSelection(stage=stage, skip_bags=skip_bags, batch_jobs=batch_jobs,
                                     uncompressed=uncompressed, part=part)
        # The name before the stream: a running campaign is offered as
        # `<id>.incomplete.tar.gz`, and the header is the only place that reaches a browser
        # -- which saves whatever this says and never sees the marker inside the archive.
        name = _guard(lambda: source.campaign_archive_name(campaign_id))
        if uncompressed:
            name = name.removesuffix(".gz")
        return StreamingResponse(
            _guard(lambda: source.campaign_tar_stream(campaign_id, selection)),
            media_type=TAR_MEDIA_TYPE if uncompressed else GZIP_MEDIA_TYPE,
            headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @router.get(Routes.campaign_inputs("{campaign_id}"))
    def download_campaign_inputs(campaign_id: str,
                                 config_file: list[str] = Query(default=[])):
        """Stream the tar a job pod extracts into its ``/config``.

        ``config_file`` names a cell's own input as ``<config_name>:<rel>``, repeated
        once per file; each lands at ``<rel>`` on top of the campaign's copy.
        """
        pairs = []
        for item in config_file:
            config_name, sep, rel = item.partition(":")
            if not sep or not config_name or not rel:
                raise HTTPException(status_code=400,
                                    detail=f"config_file must be <config_name>:<rel>, got {item!r}")
            pairs.append((config_name, rel))
        return StreamingResponse(
            _guard(lambda: source.campaign_inputs_tar_stream(campaign_id, pairs)),
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

    return router


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
