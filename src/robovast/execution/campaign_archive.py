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

"""Stream a campaign ``tar.gz`` from a local directory.

* :func:`campaign_tar_stream` / :func:`iter_campaign_tar` produce the archive
  as an on-the-fly ``pigz`` stream with **no tar on disk** — used to push a
  campaign to an external share provider (upload-to-share, cluster) and to serve
  the ``/data/campaigns/{id}/archive`` download, both of which run against ~1TB
  campaigns where materialising a compressed copy would blow the pod's scratch.

**Compression is for bytes that leave the cluster.** A stream a pod fetches or delivers
is a plain tar (``compress=False``): run output is mostly recordings that barely
compress, so gzip there buys almost no size and costs a core per stream -- a single
``gzip`` caps a transfer near 70 MB/s where the plain tar runs at disk speed, and on the
pod side that core is taken from the scenario it belongs to.

All of them read a **local directory** -- the campaign's home is the service's results
tree. Symlinks (the ``<config>/<run>/job`` links) are preserved as
symlink members (``dereference=False``) and not recursed into, so the archive is
navigable without duplicating ``_jobs/`` under every run.

A further stream feeds the pods a campaign runs in, and is what makes a tar the
transport rather than a per-file protocol: :func:`iter_inputs_tar` is what a job pod
extracts into its ``/config``. A tar carries executable bits and symlinks natively, so
nothing has to be restored on the other side.
"""

import contextlib
import io
import json
import logging
import os
import subprocess  # nosec B404 - fixed 'pigz' binary, no shell
import tarfile
import threading
import time

logger = logging.getLogger(__name__)

#: Excluded from every campaign archive by default: ``.cache`` holds the tables built from
#: the campaign's records, which are rebuilt on first use and never belong in a shared or
#: downloaded campaign.
DEFAULT_EXCLUDE = frozenset({".cache"})

#: Read size for the download generator.
_CHUNK = 1024 * 1024

#: Campaign-relative member marking an archive taken while the campaign was still
#: running. Its presence is the whole signal: a snapshot has the shape of a finished
#: campaign and nothing else in it says otherwise, so an importer that did not find this
#: file would register half a campaign as a whole one. Written into ``_execution/``
#: because that is where a campaign keeps what happened to it, and read by
#: :mod:`robovast.service.ingest`.
SNAPSHOT_MEMBER = "_execution/snapshot.json"


def snapshot_marker(campaign_id: str, **facts) -> bytes:
    """The bytes of :data:`SNAPSHOT_MEMBER` for *campaign_id*.

    *facts* are whatever the caller knows about the moment of capture (run tallies, the
    phase). Kept open rather than typed: this file is read by a human deciding whether to
    trust the archive at least as often as by :mod:`~robovast.service.ingest`, and the
    fields worth having differ per caller.
    """
    from datetime import datetime, timezone
    return json.dumps({
        "campaign_id": campaign_id,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "complete": False,
        "note": ("Taken while the campaign was still running: runs that had not finished "
                 "are missing, and derived data has not been computed. Importing this "
                 "registers an incomplete campaign."),
        **facts,
    }, indent=2, sort_keys=True).encode("utf-8") + b"\n"


def add_snapshot_marker(tar: tarfile.TarFile, campaign_id: str, **facts) -> None:
    """Add :data:`SNAPSHOT_MEMBER` for *campaign_id* under its campaign directory."""
    payload = snapshot_marker(campaign_id, **facts)
    info = tarfile.TarInfo(name=f"{campaign_id}/{SNAPSHOT_MEMBER}")
    info.size = len(payload)
    info.mtime = int(time.time())
    tar.addfile(info, io.BytesIO(payload))


def _make_filter(exclude, on_member=None):
    """Return a ``tarfile.add`` filter dropping any member under an *exclude* name.

    Excluding a *directory* prunes its whole subtree: ``tarfile.add`` does not
    recurse into a member whose filter returns ``None``. That is how the campaign's
    rebuildable table cache is kept out of a downloaded campaign.

    *on_member*, when given, is called with each **kept** member's byte size as it
    is added. It is the source-side counter behind the upload progress bar: the
    archive is gzipped on the fly, so nothing knows the compressed total, and
    counting what goes *in* is the only cheap denominator there is (see
    :func:`campaign_source_bytes`). It rides on the filter because ``tarfile.add``
    already calls that once per member -- a second walk would cost a stat per file
    for a number the first walk has in hand.
    """
    exclude = frozenset(exclude or ())
    if not exclude and on_member is None:
        return None

    def _filter(tarinfo):
        # tarinfo.name is the arcname (``<campaign>/<rel>``); drop the member if any
        # path component matches an excluded name.
        if exclude and exclude.intersection(tarinfo.name.split("/")):
            return None
        if on_member is not None:
            # Directories and symlinks carry size 0, so this counts file payload only --
            # the same bytes `campaign_source_bytes` sums.
            on_member(tarinfo.size)
        return tarinfo

    return _filter


def campaign_source_bytes(campaign_root: str, exclude=DEFAULT_EXCLUDE) -> int:
    """Sum the payload bytes :func:`campaign_tar_stream` would read from *campaign_root*.

    The denominator for a streamed upload's progress. Deliberately a metadata-only walk -- `os.scandir` carries the
    size, so this is one directory read per level and no file is opened -- because it
    runs *before* a transfer that will read every one of those bytes anyway.

    Mirrors the archiver's rules exactly, or the bar would end somewhere other than
    100%: an excluded name prunes its whole subtree, symlinks are members rather than
    paths to follow (``dereference=False``), so they are not recursed into and contribute
    nothing.
    """
    exclude = frozenset(exclude or ())
    total = 0
    root = os.path.normpath(str(campaign_root))
    stack = [(root, "")]
    while stack:
        path, rel = stack.pop()
        try:
            entries = list(os.scandir(path))
        except OSError:
            # A campaign is live until it is not; a directory that vanished under the
            # walk costs the bar some accuracy and must not cost the upload its run.
            continue
        for entry in entries:
            if entry.name in exclude:
                continue
            child = f"{rel}/{entry.name}" if rel else entry.name
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                stack.append((entry.path, child))
                continue
            try:
                total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
    return total


def _add_campaign_tree(tar: tarfile.TarFile, campaign_root: str, exclude,
                       on_member=None) -> None:
    """Add the whole campaign tree under ``<campaign-id>/`` into *tar*.

    Relies on the TarFile's ``dereference=False`` (the default) so ``job`` symlinks
    are stored as symlink members and not followed/recursed.
    """
    arcname = os.path.basename(os.path.normpath(campaign_root))
    tar.add(campaign_root, arcname=arcname,
            filter=_make_filter(exclude, on_member))


class _LiveFile(io.RawIOBase):
    """A file being written, read as exactly the *size* bytes its header promised.

    ``tarfile`` writes a member's header first and then copies exactly ``size`` bytes; a
    file that shrinks or is truncated under the copy makes it raise ``unexpected end of
    data`` — and by then the response's status line is long since 200, so the caller gets
    a truncated body rather than an error it can read. A campaign directory is written to
    continuously while it runs, so that is not a rare race there but the normal case.

    Padding the short tail with zeros keeps the archive structurally valid: one member of
    a snapshot has a garbled tail, and the other hundred thousand arrive intact. A file
    that *grew* needs nothing — the header's size is the truncation.
    """

    def __init__(self, raw, size: int):
        super().__init__()
        self._raw = raw
        self._left = size

    def read(self, size=-1):  # noqa: D102 - RawIOBase's contract
        if self._left <= 0:
            return b""
        want = self._left if size is None or size < 0 else min(size, self._left)
        try:
            chunk = self._raw.read(want)
        except OSError:
            chunk = b""
        if len(chunk) < want:
            chunk += b"\0" * (want - len(chunk))
        self._left -= len(chunk)
        return chunk

    def readable(self) -> bool:
        return True

    def close(self) -> None:
        try:
            self._raw.close()
        finally:
            super().close()


def _add_live_tree(tar: tarfile.TarFile, campaign_root: str, exclude) -> None:
    """Add a campaign that is **still being written** into *tar*, member by member.

    ``TarFile.add`` walks the tree itself and lets an ``OSError`` from any single file
    abort the whole archive. Here every member is added on its own and a file that has
    vanished since the directory was read is skipped *before* its header is written — the
    only point at which skipping is still free, because a member whose header is out
    cannot be taken back out of a stream.

    Sizes are taken from the open descriptor rather than from the directory entry, so the
    header cannot describe a different moment than the payload; :class:`_LiveFile` covers
    what changes after that.
    """
    exclude = frozenset(exclude or ())
    root = os.path.normpath(str(campaign_root))
    base = os.path.basename(root)
    tar.add(root, arcname=base, recursive=False)
    stack = [(root, base)]
    while stack:
        path, arc = stack.pop()
        try:
            entries = sorted(os.scandir(path), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            if entry.name in exclude:
                continue
            child = f"{arc}/{entry.name}"
            try:
                if entry.is_symlink() or entry.is_dir(follow_symlinks=False):
                    # Both are payload-free members: a symlink is stored as a link (the
                    # ``<config>/<run>/job`` links) and a directory as an entry, so neither
                    # can fail half-written.
                    tar.addfile(tar.gettarinfo(entry.path, arcname=child))
                    if not entry.is_symlink():
                        stack.append((entry.path, child))
                    continue
                with open(entry.path, "rb") as raw:
                    info = tar.gettarinfo(arcname=child, fileobj=raw)
                    tar.addfile(info, _LiveFile(raw, info.size))
            except OSError:
                logger.debug("Skipping %s: it changed while the snapshot was taken",
                             entry.path)
                continue


class _TarPipe:
    """A running tar stream: a writer thread tars into a pipe, and ``stdout`` reads it.

    With *compress* the pipe is ``pigz``, so ``stdout`` is a gzip stream compressed on
    every core; without, it is an OS pipe carrying the plain tar.

    *add_members* is a callable ``(tarfile.TarFile) -> None`` that adds every member
    from a local directory. No source ever materialises a copy on disk.
    """

    def __init__(self, add_members, *, compress: bool = True):
        self._add_members = add_members
        self._error: list = []
        self._pigz = None
        if compress:
            # nosec B603 B607 - fixed binary, no shell, trusted args
            self._pigz = subprocess.Popen(
                ["pigz", "-c"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            self._sink, self._stdout = self._pigz.stdin, self._pigz.stdout
        else:
            read_fd, write_fd = os.pipe()
            self._sink, self._stdout = os.fdopen(write_fd, "wb"), os.fdopen(read_fd, "rb")
        self._writer = threading.Thread(
            target=self._write_tar, name="campaign-tar-writer", daemon=True)
        self._writer.start()

    @property
    def stdout(self):
        return self._stdout

    def _write_tar(self) -> None:
        try:
            with tarfile.open(fileobj=self._sink, mode="w|") as tar:
                self._add_members(tar)
        except BaseException as exc:  # pylint: disable=broad-except
            self._error.append(exc)
        finally:
            try:
                self._sink.close()
            except OSError:
                pass

    def close(self) -> None:
        """Join the writer, reap ``pigz`` if any, and re-raise a producer/compressor error.

        Closing the read end first is what unblocks a writer the reader abandoned: its
        next write fails with a broken pipe instead of waiting for a reader that is gone.
        """
        try:
            self._stdout.close()
        except OSError:
            pass
        self._writer.join()
        if self._pigz is not None:
            self._pigz.wait()
        if self._error:
            error = self._error[0]
            if isinstance(error, BrokenPipeError):
                return  # the reader stopped early -- its choice, not a failure here
            raise error
        if self._pigz is not None and self._pigz.returncode not in (0, None):
            raise RuntimeError(f"pigz exited with code {self._pigz.returncode}")


@contextlib.contextmanager
def tar_stream(add_members, *, compress: bool = True):
    """Context manager yielding a **readable** tar stream produced by *add_members*.

    gzip-compressed with *compress*, plain without. No archive is written to disk. The
    yielded object is a binary file-like; read it to completion inside the ``with``
    block. On exit the writer thread is joined and any tar/pigz failure is re-raised.
    """
    pipe = _TarPipe(add_members, compress=compress)
    try:
        yield pipe.stdout
    finally:
        pipe.close()


def iter_tar(add_members, chunk_size: int = _CHUNK, *, compress: bool = True):
    """Generator yielding the bytes of a tar produced by *add_members*.

    gzip-compressed with *compress*, plain without. Owns the pipe lifecycle across the
    whole iteration — cleanup (and error propagation) happens when the generator is
    exhausted or closed, which is what a streaming HTTP response needs (the body is
    produced after the route returns).
    """
    pipe = _TarPipe(add_members, compress=compress)
    try:
        while True:
            chunk = pipe.stdout.read(chunk_size)
            if not chunk:
                break
            yield chunk
    finally:
        pipe.close()


def campaign_tar_stream(campaign_root: str, exclude=DEFAULT_EXCLUDE, on_member=None):
    """CM yielding a readable gzip stream of the local directory *campaign_root*.

    *on_member* is the source-side progress counter described in :func:`_make_filter`.
    """
    return tar_stream(lambda tar: _add_campaign_tree(tar, campaign_root, exclude, on_member))


def iter_campaign_tar(campaign_root: str, exclude=DEFAULT_EXCLUDE, chunk_size: int = _CHUNK,
                      snapshot: "dict | None" = None):
    """Generator yielding the ``tar.gz`` of the local directory *campaign_root*.

    Compressed, because a download leaves the cluster.

    *snapshot* — a dict of facts, possibly empty — says the campaign is **still running**:
    the tree is then read tolerantly (:func:`_add_live_tree`) and :data:`SNAPSHOT_MEMBER`
    is added carrying those facts, so what lands can never be mistaken for a finished
    campaign. ``None`` is the finished campaign, added the strict way.

    The tree is always read tolerantly (:func:`_add_live_tree`), whether or not it is
    marked: a finished campaign is re-postprocessed in place, and a file that changes
    under the walk must cost one member rather than the download -- past the first byte
    the status line is already 200 and a failure reaches the caller as a truncated body.
    On a tree nothing is writing to, the tolerant walk produces the same archive.
    """
    campaign_id = os.path.basename(os.path.normpath(str(campaign_root)))

    def _add(tar):
        _add_live_tree(tar, campaign_root, exclude)
        if snapshot is not None:
            add_snapshot_marker(tar, campaign_id, **snapshot)

    return iter_tar(_add, chunk_size)


def iter_tree_tar(root: str, chunk_size: int = _CHUNK):
    """Generator yielding a plain tar of *root*'s contents, relative to *root*.

    No top-level segment: what a pod extracts into a mount point lands at the mount
    point. Tolerant like :func:`iter_inputs_tar`, and for the same reason. Uncompressed,
    because its reader is always a pod in the cluster.
    """
    return iter_tar(lambda tar: _add_tree_flat(tar, os.path.normpath(str(root))), chunk_size,
                    compress=False)


#: The campaign's input tree and the composer's per-job files: what a job pod is given.
INPUT_DIRS = ("_config", "_transient")

#: The suffixes of a job's own documents in ``_transient/``: its scenario parameters and
#: its simulator overrides, named by the job's tag (:func:`job_documents`).
JOB_DOCUMENT_SUFFIXES = (".params.yaml", ".sim.yaml")


def job_documents(tag: str) -> "tuple[str, str]":
    """The names of job *tag*'s scenario-parameter and simulator-override documents.

    The one place these names are made: the composer writes them into ``_transient/``, a
    pod reads them from ``/config``, and :func:`iter_inputs_tar` tells a job's own
    documents from every other job's by them.
    """
    params, sim = (tag + suffix for suffix in JOB_DOCUMENT_SUFFIXES)
    return params, sim


def _job_document_tag(name: str):
    """The tag *name* is a job document of, or ``None`` for a campaign-wide file."""
    for suffix in JOB_DOCUMENT_SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[:-len(suffix)]
    return None


def iter_inputs_tar(campaign_root: str, job_tags, config_files=None,
                    chunk_size: int = _CHUNK):
    """Generator yielding the plain tar a job pod extracts into its ``/config``.

    The campaign's :data:`INPUT_DIRS` with that leading segment stripped, so ``_config/x``
    lands at ``x``; then, for each ``(config_name, rel)`` in *config_files*, the cell's
    ``<config>/_config/<rel>`` as ``<rel>``. Later members win on extraction, which is
    what makes a cell's copy land on the campaign's -- the packer keeps one file-owning
    configuration per job, so which copy wins is never in question.

    Of the job documents in ``_transient/`` (:func:`job_documents`), only those of
    *job_tags* are sent: the composer writes one pair per job of the campaign, and a pod
    reads its own, so sending every job's would make each pod's download grow with the
    campaign. Every other file there is campaign-wide and sent to every pod. A tag with no
    parameter document raises ``KeyError`` before any byte is streamed -- a pod asking for
    one would otherwise start with nothing to run.

    Named per declared path rather than the cell's ``_config/`` wholesale, because that
    directory also holds the cell's *records* -- ``config.yaml``, ``scenario.config``,
    ``sim.config``, ``sut.config`` -- and ``scenario.config`` at ``/config/scenario.config``
    is the entrypoint's default parameter file. Composition knows exactly which paths are
    inputs, so they are named rather than filtered out by a list that would have to grow
    with every new record.

    Members are added tolerantly (:func:`_add_live_tree`'s rules): the composer writes a
    batch's files as the campaign runs, and a file that vanished between the listing and
    the read costs one member, not the pod.
    """
    root = os.path.normpath(str(campaign_root))
    tags = set(job_tags)
    if not tags:
        raise ValueError("a job pod's inputs name the job they are for: pass at least one tag")
    transient = os.path.join(root, "_transient")
    for tag in sorted(tags):
        if not tag or "/" in tag or tag in (".", ".."):
            raise ValueError(f"a job tag is one path segment, got {tag!r}")
        if not os.path.isfile(os.path.join(transient, job_documents(tag)[0])):
            raise KeyError(f"no parameter document for job {tag!r} in this campaign")

    def _keep(arc: str) -> bool:
        tag = None if "/" in arc else _job_document_tag(arc)
        return tag is None or tag in tags

    def _add(tar):
        for top in INPUT_DIRS:
            src = os.path.join(root, top)
            if not os.path.isdir(src):
                continue
            _add_tree_flat(tar, src, keep=_keep if src == transient else None)
        for config_name, rel in (config_files or ()):
            src = os.path.join(root, config_name, "_config", rel)
            try:
                with open(src, "rb") as raw:
                    info = tar.gettarinfo(arcname=rel, fileobj=raw)
                    tar.addfile(info, _LiveFile(raw, info.size))
            except OSError:
                logger.debug("Skipping %s: not present when the inputs were staged", src)

    return iter_tar(_add, chunk_size, compress=False)


def _add_tree_flat(tar: tarfile.TarFile, src: str, keep=None) -> None:
    """Add every entry under *src* into *tar* relative to *src* itself (no top segment).

    *keep*, when given, is called with a file's relative name and leaves it out on false.
    """
    stack = [(src, "")]
    while stack:
        path, arc = stack.pop()
        try:
            entries = sorted(os.scandir(path), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            child = f"{arc}/{entry.name}" if arc else entry.name
            if keep is not None and entry.is_file(follow_symlinks=False) and not keep(child):
                continue
            try:
                if entry.is_symlink() or entry.is_dir(follow_symlinks=False):
                    tar.addfile(tar.gettarinfo(entry.path, arcname=child))
                    if not entry.is_symlink():
                        stack.append((entry.path, child))
                    continue
                with open(entry.path, "rb") as raw:
                    info = tar.gettarinfo(arcname=child, fileobj=raw)
                    tar.addfile(info, _LiveFile(raw, info.size))
            except OSError:
                logger.debug("Skipping %s: it changed while the inputs were staged",
                             entry.path)
