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

"""Extract a tar stream into a tree, refusing what must not land there.

The data plane's writing half. A pod delivers what it produced as one tar stream, and this
is what puts it on disk: member by member, as the bytes arrive, so a gigabyte of rosbags
never sits in memory and a stream that stops halfway leaves no half-written file behind.

Three refusals, each because the alternative writes somewhere or something the caller did
not ask for:

* **A member that leaves the tree.** ``..``, an absolute name, or a symlink whose target
  resolves outside *dest_root*. The same confinement every caller-supplied path gets
  (:mod:`robovast.client.safe_path`); a tar is a caller-supplied path per member.
* **A hard link or a device.** A hard link into the tree is a second name for a file the
  tree already has, which is what a symlink is for; one to a file outside it is an escape.
  Neither has a use in campaign output.
* **A name the caller may not write.** Given per call as *deny*: the campaign's own
  store, which the driver holds open, and the driver's logs, which have one writer.

Refused members are named in the result rather than raised on: a pod's output is many
files, and one it may not write is not a reason to lose the rest.
"""

from __future__ import annotations

import logging
import os
import queue
import tarfile
from pathlib import Path

from robovast.client.safe_path import UnsafePathError, check_relative

logger = logging.getLogger(__name__)

#: Suffix a regular file is written under until it is complete, then renamed into place.
#: A reader of the tree sees either nothing or the whole file, never a prefix -- and a
#: stream that dies mid-member leaves a file with this suffix, which a sweep can name.
INCOMING_SUFFIX = ".robovast-incoming"

#: What every extraction refuses: the campaign's own SQLite store and its journal. The
#: driver holds it open for the campaign's whole life, and a pod has nothing to say in it.
DENY_ALWAYS = ("campaign.db", "campaign.db-journal", "campaign.db-wal", "campaign.db-shm")

#: Read size for the loop-to-thread bridge, and the bound on how much of an upload waits
#: in memory between the two.
_CHUNK = 64 * 1024
_QUEUE_CHUNKS = 64


class Extracted:
    """What one extraction wrote and refused."""

    def __init__(self):
        self.files = 0
        self.bytes = 0
        self.refused: list[str] = []


def extract_stream(stream, dest_root, *, deny=()) -> Extracted:
    """Extract the tar read from *stream* under *dest_root*; return what happened.

    *stream* is any binary file-like with ``read``; gzip or plain is detected from the
    bytes (``r|*``). *deny* is a set of campaign-relative names, or names of files under
    any directory (a bare file name), that are refused on top of :data:`DENY_ALWAYS`.

    Members are written in stream order and the last one wins, which is how several
    containers of one pod, each contributing its own files to a shared tree, resolve.
    """
    root = Path(dest_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    denied = frozenset(DENY_ALWAYS) | frozenset(deny or ())
    out = Extracted()
    with tarfile.open(fileobj=stream, mode="r|*") as tar:
        for member in tar:
            rel = _member_rel(member.name)
            if rel is None:
                continue
            if rel == "":
                continue  # the tree itself (``./``)
            try:
                check_relative(rel)
            except UnsafePathError:
                out.refused.append(member.name)
                continue
            if rel in denied or os.path.basename(rel) in denied:
                out.refused.append(member.name)
                continue
            target = root / rel
            if _escapes(root, target.parent):
                out.refused.append(member.name)
                continue
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                _chmod(target, member.mode | 0o700)
            elif member.issym():
                if _escapes(root, (target.parent / member.linkname)):
                    out.refused.append(member.name)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                _replace_with_symlink(target, member.linkname)
            elif member.isfile():
                source = tar.extractfile(member)
                if source is None:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                _write_atomic(target, source, member.mode)
                out.files += 1
                out.bytes += member.size
            else:
                # Hard links, devices, FIFOs: nothing campaign output has a use for.
                out.refused.append(member.name)
    return out


def _member_rel(name: str) -> "str | None":
    """A member's name as a relative path, ``""`` for the root, ``None`` for nothing."""
    name = name.strip("/")
    if name in ("", "."):
        return ""
    if name.startswith("./"):
        name = name[2:]
    return name or ""


def _escapes(root: Path, path: Path) -> bool:
    """Whether *path*, with the symlinks that already exist under *root* followed, leaves it."""
    resolved = path.resolve()
    return resolved != root and root not in resolved.parents


def _chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode & 0o7777)
    except OSError:
        pass


def _replace_with_symlink(target: Path, linkname: str) -> None:
    if target.is_symlink() or target.exists():
        if target.is_dir() and not target.is_symlink():
            # A directory where a link should be is a shape conflict, not a file to
            # overwrite; leave it, the link's purpose (a `job` pointer) is served by the
            # directory holding the same names.
            return
        target.unlink()
    os.symlink(linkname, target)


def _write_atomic(target: Path, source, mode: int) -> None:
    """Write *source* to ``target`` through :data:`INCOMING_SUFFIX` and rename into place."""
    incoming = target.with_name(target.name + INCOMING_SUFFIX)
    try:
        with open(incoming, "wb") as fh:
            while True:
                chunk = source.read(_CHUNK)
                if not chunk:
                    break
                fh.write(chunk)
        # The member's mode, always: the executable bit is what the tar carries it for,
        # and a run's own permissions are part of what it produced.
        _chmod(incoming, (mode or 0o644) | 0o600)
        if target.is_dir() and not target.is_symlink():
            # A file arriving where a directory stands: the directory wins, as it would
            # under a mirror that never deletes.
            incoming.unlink()
            return
        os.replace(incoming, target)
    except BaseException:
        try:
            incoming.unlink()
        except OSError:
            pass
        raise


def sweep_incoming(tree) -> int:
    """Remove every :data:`INCOMING_SUFFIX` leftover under *tree*; return how many.

    A stream that stopped mid-member leaves exactly one; a resume, a finish or a delete
    is where it is worth clearing.
    """
    removed = 0
    for path in Path(tree).rglob(f"*{INCOMING_SUFFIX}"):
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


class StreamReader:
    """A blocking, file-like reader over chunks another thread (the event loop) pushes.

    The upload route reads the request body on the event loop and hands each chunk to
    :meth:`push`; :func:`extract_stream` runs on a worker thread and pulls them through
    :meth:`read`. The queue is bounded, so a slow disk holds the socket back rather than
    the whole body piling up in memory.
    """

    def __init__(self, max_chunks: int = _QUEUE_CHUNKS):
        self._queue: "queue.Queue[bytes | None]" = queue.Queue(maxsize=max_chunks)
        self._buffer = b""
        self._eof = False

    def push(self, chunk: bytes) -> None:
        if chunk:
            self._queue.put(chunk)

    def finish(self) -> None:
        """No more chunks: the body ended, or the connection did."""
        self._queue.put(None)

    def read(self, size: int = -1) -> bytes:
        while not self._eof and (size < 0 or len(self._buffer) < size):
            chunk = self._queue.get()
            if chunk is None:
                self._eof = True
                break
            self._buffer += chunk
        if size < 0:
            out, self._buffer = self._buffer, b""
            return out
        out, self._buffer = self._buffer[:size], self._buffer[size:]
        return out
