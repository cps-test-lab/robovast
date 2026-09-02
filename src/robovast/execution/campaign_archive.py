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

"""Build a campaign ``tar.gz`` from a local directory — as a file, or streamed.

One place produces the campaign archive for both directions of "share":

* :func:`make_campaign_tarball` writes ``<archive_dir>/<campaign>.tar.gz`` — the
  local backend's upload-to-share deliverable (there is no external provider
  locally, so the file *is* the artifact).
* :func:`campaign_tar_stream` / :func:`iter_campaign_tar` produce the same archive
  as an on-the-fly ``pigz`` stream with **no tar on disk** — used to push a
  campaign to an external share provider (upload-to-share, cluster) and to serve
  the ``/campaigns/{id}/archive`` download, both of which run against ~1TB
  campaigns where materialising a compressed copy would blow the pod's scratch.

Both read a **local directory**: upload-to-share streams the campaign already on
the driver's scratch; the download streams the dir ``fetch_campaign`` materialises
from the object store. Symlinks (the ``<config>/<run>/job`` links) are preserved as
symlink members (``dereference=False``) and not recursed into, so the archive is
navigable without duplicating ``_jobs/`` under every run.
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

#: Excluded from every campaign archive by default: the postprocessing hash-cache
#: (``.cache``) is derived/rebuildable and never belongs in a shared or downloaded
#: campaign.
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
    fields worth having differ per lane.
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
    recurse into a member whose filter returns ``None``. That is how a rebuildable
    cache, or a legacy staging copy of derived data, is kept out of a downloaded
    campaign.

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

    The denominator for a streamed upload's progress. Deliberately a metadata-only
    walk -- `os.scandir` carries the size, so this is one directory read per level and
    no file is opened -- because it runs *before* an upload that will read every one of
    those bytes anyway.

    Mirrors the archiver's two rules exactly, or the bar would end somewhere other
    than 100%: an excluded name prunes its whole subtree, and symlinks are members
    rather than paths to follow (``dereference=False``), so they are not recursed
    into and contribute nothing.
    """
    exclude = frozenset(exclude or ())
    total = 0
    stack = [os.path.normpath(str(campaign_root))]
    while stack:
        try:
            entries = list(os.scandir(stack.pop()))
        except OSError:
            # A campaign is live until it is not; a directory that vanished under the
            # walk costs the bar some accuracy and must not cost the upload its run.
            continue
        for entry in entries:
            if entry.name in exclude:
                continue
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                stack.append(entry.path)
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
    tar.add(campaign_root, arcname=arcname, filter=_make_filter(exclude, on_member))


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


def make_campaign_tarball(campaign_root: str, archive_dir: str,
                          exclude=DEFAULT_EXCLUDE, name: "str | None" = None,
                          on_member=None) -> str:
    """Write the campaign at *campaign_root* into *archive_dir*; return its path.

    *name* is the file name to write, defaulting to ``<campaign>.tar.gz``. The local
    lane passes the variant-carrying name a share uses
    (:func:`~robovast.execution.share_providers.naming.archive_name`) so its
    ``_archives/`` dir and a real share are readable by the same parser.

    Uses Python's built-in gzip (no ``pigz`` dependency) since this runs on the
    local host where ``pigz`` may be absent; the stream variants use ``pigz`` on the
    driver/service image where it is present.
    """
    campaign_root = os.path.normpath(str(campaign_root))
    arcname = os.path.basename(campaign_root)
    os.makedirs(archive_dir, exist_ok=True)
    out_path = os.path.join(archive_dir, name or f"{arcname}.tar.gz")
    with tarfile.open(out_path, "w:gz") as tar:
        _add_campaign_tree(tar, campaign_root, exclude, on_member)
    logger.info("Wrote campaign archive %s", out_path)
    return out_path


class _TarPipe:
    """A running ``tar | pigz`` pipe: a writer thread tars into ``pigz`` stdin, and
    ``stdout`` is a readable stream of the compressed archive.

    *add_members* is a callable ``(tarfile.TarFile) -> None`` that adds every member —
    from a local directory (upload-to-share) or streamed from the object store
    (download). Neither source ever materialises a compressed copy on disk.
    """

    def __init__(self, add_members):
        self._add_members = add_members
        self._error: list = []
        # nosec B603 B607 - fixed binary, no shell, trusted args
        self._pigz = subprocess.Popen(
            ["pigz", "-c"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self._writer = threading.Thread(
            target=self._write_tar, name="campaign-tar-writer", daemon=True)
        self._writer.start()

    @property
    def stdout(self):
        return self._pigz.stdout

    def _write_tar(self) -> None:
        try:
            with tarfile.open(fileobj=self._pigz.stdin, mode="w|") as tar:
                self._add_members(tar)
        except BaseException as exc:  # pylint: disable=broad-except
            self._error.append(exc)
        finally:
            try:
                self._pigz.stdin.close()
            except OSError:
                pass

    def close(self) -> None:
        """Join the writer, reap ``pigz``, and re-raise any producer/compressor error."""
        try:
            self._pigz.stdout.close()
        except OSError:
            pass
        self._writer.join()
        self._pigz.wait()
        if self._error:
            raise self._error[0]
        if self._pigz.returncode not in (0, None):
            raise RuntimeError(f"pigz exited with code {self._pigz.returncode}")


@contextlib.contextmanager
def tar_stream(add_members):
    """Context manager yielding a **readable** gzip stream produced by *add_members*.

    No archive is written to disk. The yielded object is a binary file-like
    (``pigz`` stdout); read it to completion inside the ``with`` block. On exit the
    writer thread is joined and any tar/pigz failure is re-raised.
    """
    pipe = _TarPipe(add_members)
    try:
        yield pipe.stdout
    finally:
        pipe.close()


def iter_tar(add_members, chunk_size: int = _CHUNK):
    """Generator yielding gzip-archive bytes produced by *add_members*.

    Owns the pipe lifecycle across the whole iteration — cleanup (and error
    propagation) happens when the generator is exhausted or closed, which is what a
    streaming HTTP response needs (the body is produced after the route returns).
    """
    pipe = _TarPipe(add_members)
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
    """Generator yielding gzip-archive bytes of the local directory *campaign_root*.

    *snapshot* — a dict of facts, possibly empty — says the campaign is **still running**:
    the tree is then read tolerantly (:func:`_add_live_tree`) and :data:`SNAPSHOT_MEMBER`
    is added carrying those facts, so what lands can never be mistaken for a finished
    campaign. ``None`` is the finished campaign, added the strict way.
    """
    campaign_id = os.path.basename(os.path.normpath(str(campaign_root)))

    def _add(tar):
        if snapshot is None:
            _add_campaign_tree(tar, campaign_root, exclude)
            return
        _add_live_tree(tar, campaign_root, exclude)
        add_snapshot_marker(tar, campaign_id, **snapshot)

    return iter_tar(_add, chunk_size)
