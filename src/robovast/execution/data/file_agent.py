#!/usr/bin/env python3
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

"""The file agent of a cluster scenario pod: ships the growth of the run's files while it runs.

A pod's ``/out`` reaches the campaign in one tar once the run is over (the uploader,
``pod_upload``). This script makes the run's log files, line-format files (CSV, JSONL) and
bags visible in the campaign directory **as they grow**: it watches ``/out`` with inotify,
coalesces what changed for one second, and delivers the new bytes of each grown file to the
data plane as a byte range -- a tar member carrying the pax header ``ROBOVAST.offset``,
which the data plane appends when its copy of the file ends at that offset
(``robovast.service.tar_io.extract_stream``).

* **What it ships** (:func:`shipped_as`): line files -- ``*.log`` under a ``logs/``
  directory, ``*.csv`` and ``*.jsonl`` -- as complete lines; the bags' ``*.mcap`` under
  ``rosbag2/``, ``logs/rosout_bag/`` and ``roqsim_bag/`` as every new byte, since a
  write-through bag is readable up to its last complete record; and the small files that
  appear beside a bag (``metadata.yaml``, ``message_definitions.json``) whole, each time
  they are written. Never a data-plane temp file.
* **Complete lines only** for a line file: a delivery ends at the last ``\\n`` of what the
  file holds, so a reader of the campaign's copy never parses half a line. The final drain
  sends the rest.
* **A resync** -- the data plane's copy does not end where this agent's offset says -- is
  repaired by sending the file whole (a member without the header replaces it).
* **Restart-safe**: the delivered offsets persist in ``/ipc/file_agent.json``.
* **Best effort**: a failed delivery is retried on the next cycle with a bounded backoff.
  The uploader's run-end tar is what makes the campaign's copy complete.

It ends when ``done.main`` and every ``done.<sidecar>`` named on its command line exist in
``/ipc`` -- or when ``done.main`` has existed for ``--grace`` seconds -- with a final drain,
then writes ``/ipc/done.agent``, which the uploader waits for. A SIGTERM does the same at
once.

Runs on the sidecar image (``python:3.12-alpine``), so it is standard library only, and it
is importable without side effects: the :class:`Inotify` watcher is reused by the service.
"""

import argparse
import ctypes
import errno
import io
import json
import os
import select
import signal
import struct
import sys
import tarfile
import time
from urllib.parse import quote, urlsplit

# -- names shared with the service ---------------------------------------------------------
# This script is copied into the pod on its own and imports nothing of robovast, so the
# names it shares are stated here; tests/execution/test_file_agent.py asserts each one
# equals its definition.

#: ``robovast.execution.cluster_execution.pod_access.DATA_URL_ENV``
DATA_URL_ENV = "ROBOVAST_DATA_URL"
#: ``robovast.execution.cluster_execution.pod_access.CAMPAIGN_ID_ENV``
CAMPAIGN_ID_ENV = "ROBOVAST_CAMPAIGN_ID"
#: ``robovast.execution.cluster_execution.pod_access.TOKEN_ENV``
TOKEN_ENV = "ROBOVAST_TOKEN"
#: ``robovast.execution.cluster_execution.pod_upload.IPC_DIR_ENV`` / ``OUT_DIR_ENV``
IPC_DIR_ENV = "ROBOVAST_IPC_DIR"
OUT_DIR_ENV = "ROBOVAST_OUT_DIR"
#: ``robovast.common.execution.IPC_DIR`` and ``pod_upload.OUT_DIR``
IPC_DIR = "/ipc"
OUT_DIR = "/out"
#: ``robovast.common.execution.MAIN_CONTAINER``
MAIN_CONTAINER = "main"
#: ``robovast.execution.cluster_execution.pod_upload.AGENT_CONTAINER``
AGENT_CONTAINER = "agent"
#: ``robovast.service.tar_io.OFFSET_HEADER``
OFFSET_HEADER = "ROBOVAST.offset"
#: ``robovast.service.tar_io.INCOMING_SUFFIX``
INCOMING_SUFFIX = ".robovast-incoming"

#: Where the delivered offsets persist, under the IPC directory.
STATE_FILE = "file_agent.json"

#: How long events are gathered before one delivery.
COALESCE_S = 1.0
#: The ceiling of the backoff after a failed delivery; it doubles from one second.
MAX_BACKOFF_S = 30.0
#: Bytes one delivery carries at most, so the agent's memory is bounded whatever a file's
#: size. What does not fit goes in the next cycle.
MAX_DELIVERY_BYTES = 8 * 1024 * 1024
#: Seconds one request may take before it counts as failed.
REQUEST_TIMEOUT_S = 60.0


def log(message: str) -> None:
    print(f"[agent] {message}", flush=True)


# -- inotify ----------------------------------------------------------------------------------

IN_MODIFY = 0x00000002
IN_CLOSE_WRITE = 0x00000008
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_Q_OVERFLOW = 0x00004000
IN_IGNORED = 0x00008000
IN_ISDIR = 0x40000000
IN_CLOEXEC = 0o2000000

#: What the watcher asks for: a file written or landed, a directory created or moved in.
WATCH_MASK = IN_MODIFY | IN_CLOSE_WRITE | IN_CREATE | IN_MOVED_TO

_EVENT = struct.Struct("iIII")


class Inotify:
    """A recursive inotify watch over one or more directory trees, via ``ctypes`` on libc.

    :meth:`add_tree` watches a directory and every directory below it; a directory created
    (or moved in) later is watched as it appears, and the files it already holds by then
    are reported as changed, so nothing written before its watch existed is missed.
    :meth:`wait` blocks until something changed and returns the changed paths. An event
    queue overflow reports the watched roots themselves, which a caller rescans.
    """

    def __init__(self):
        # The running interpreter's own symbols, which include libc's -- on glibc and musl
        # alike, where a library lookup by name is not reliable.
        self._libc = ctypes.CDLL(None, use_errno=True)
        self._libc.inotify_init1.argtypes = [ctypes.c_int]
        self._libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        fd = self._libc.inotify_init1(IN_CLOEXEC)
        if fd < 0:
            err = ctypes.get_errno()
            raise OSError(err, f"inotify_init1: {os.strerror(err)}")
        self.fd = fd
        self._wake_r, self._wake_w = os.pipe()
        os.set_blocking(self._wake_w, False)
        self._dirs: "dict[int, str]" = {}
        self._roots: "list[str]" = []

    def close(self) -> None:
        for fd in (self.fd, self._wake_r, self._wake_w):
            try:
                os.close(fd)
            except OSError:
                pass
        self.fd = -1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _watch(self, path: str) -> bool:
        wd = self._libc.inotify_add_watch(self.fd, os.fsencode(path), WATCH_MASK)
        if wd < 0:
            err = ctypes.get_errno()
            if err in (errno.ENOENT, errno.ENOTDIR):
                return False  # gone before it could be watched
            raise OSError(err, f"inotify_add_watch {path}: {os.strerror(err)}")
        self._dirs[wd] = path
        return True

    def _watch_tree(self, top: str) -> "list[str]":
        """Watch *top* and the directories below it; return the files found there."""
        found = []
        if not self._watch(top):
            return found
        for dirpath, dirnames, filenames in os.walk(top):
            for name in dirnames:
                self._watch(os.path.join(dirpath, name))
            found.extend(os.path.join(dirpath, name) for name in filenames)
        return found

    def add_tree(self, root: str) -> None:
        """Watch *root* recursively. It must exist."""
        root = os.path.abspath(root)
        if not os.path.isdir(root):
            raise FileNotFoundError(f"not a directory to watch: {root}")
        self._roots.append(root)
        self._watch_tree(root)

    def wake(self) -> None:
        """Make a pending or the next :meth:`wait` return at once. Safe in a signal handler."""
        try:
            os.write(self._wake_w, b"x")
        except OSError:
            pass

    def wait(self, timeout: "float | None" = None) -> "set[str]":
        """Block up to *timeout* seconds (``None``: until an event) and return what changed.

        Returns the paths of files written, created or moved in, and of directories that
        appeared; an empty set on a timeout or a :meth:`wake`.
        """
        readable, _, _ = select.select([self.fd, self._wake_r], [], [],
                                       None if timeout is None else max(0.0, timeout))
        changed: "set[str]" = set()
        if self._wake_r in readable:
            try:
                os.read(self._wake_r, 4096)
            except BlockingIOError:
                pass
        if self.fd in readable:
            changed |= self._read_events()
        return changed

    def _read_events(self) -> "set[str]":
        changed: "set[str]" = set()
        buf = os.read(self.fd, 64 * 1024)
        pos = 0
        while pos + _EVENT.size <= len(buf):
            wd, mask, _cookie, length = _EVENT.unpack_from(buf, pos)
            name = buf[pos + _EVENT.size:pos + _EVENT.size + length].rstrip(b"\0")
            pos += _EVENT.size + length
            if mask & IN_Q_OVERFLOW:
                changed.update(self._roots)
                continue
            if mask & IN_IGNORED:
                self._dirs.pop(wd, None)
                continue
            parent = self._dirs.get(wd)
            if parent is None:
                continue
            path = os.path.join(parent, os.fsdecode(name)) if name else parent
            changed.add(path)
            if mask & IN_ISDIR and mask & (IN_CREATE | IN_MOVED_TO):
                changed.update(self._watch_tree(path))
        return changed


# -- what is shipped ------------------------------------------------------------------------

#: How a file is shipped: as its complete lines, as every new byte, or whole each time it is
#: written.
LINES = "lines"
BYTES = "bytes"
WHOLE = "whole"

#: The files that appear beside a bag: rosbag2's metadata once the bag is closed, and the
#: run's definitions dump. Small by construction, so they are shipped whole.
BAG_SIDECARS = ("metadata.yaml", "message_definitions.json")


def _in_bag(dirs: "list[str]") -> bool:
    """Whether a path through *dirs* lies in a recording directory."""
    if "rosbag2" in dirs or "roqsim_bag" in dirs:
        return True
    return any(part == "logs" and dirs[i + 1] == "rosout_bag"
               for i, part in enumerate(dirs[:-1]))


def shipped_as(rel: str) -> "str | None":
    """How the agent ships the ``/out``-relative path *rel*: :data:`LINES`, :data:`BYTES`,
    :data:`WHOLE`, or ``None`` when it does not."""
    parts = rel.replace(os.sep, "/").strip("/").split("/")
    name = parts[-1]
    dirs = parts[:-1]
    if not name or name.endswith(INCOMING_SUFFIX):
        return None
    if _in_bag(dirs):
        if name.endswith(".mcap"):
            return BYTES
        if name in BAG_SIDECARS:
            return WHOLE
        return None
    if name.endswith(".csv") or name.endswith(".jsonl"):
        return LINES
    if name.endswith(".log") and "logs" in dirs:
        return LINES
    return None


def is_line_file(rel: str) -> bool:
    """Whether *rel* names a file the agent ships as complete lines."""
    return shipped_as(rel) == LINES


# -- the agent core ---------------------------------------------------------------------------

class Send:
    """One member of a delivery: bytes ``[offset, end)`` of *rel*; *offset* ``None`` = whole."""

    __slots__ = ("rel", "offset", "end", "data")

    def __init__(self, rel: str, offset: "int | None", end: int, data: bytes):
        self.rel = rel
        self.offset = offset
        self.end = end
        self.data = data

    def __repr__(self):
        return f"Send({self.rel!r}, offset={self.offset}, end={self.end})"


class Agent:
    """What has been delivered, what to deliver next, and the tar that carries it.

    *transport* is called with a function writing the tar body to a binary file object and
    returns the data plane's reply (the ``OutputsIngested`` document as a dict); it raises
    when the delivery failed. :func:`http_transport` is the pod's.
    """

    def __init__(self, out_dir: str, state_path: str, transport,
                 *, max_bytes: int = MAX_DELIVERY_BYTES):
        self.out_dir = os.path.abspath(out_dir)
        self.state_path = state_path
        self.transport = transport
        self.max_bytes = max_bytes
        self.offsets: "dict[str, int]" = {}
        self.whole: "set[str]" = set()
        self.dirty: "set[str]" = set()
        self._load()

    # state

    def _load(self) -> None:
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                state = json.load(fh)
        except FileNotFoundError:
            return
        self.offsets = {str(k): int(v) for k, v in state.get("offsets", {}).items()}
        self.whole = set(state.get("whole", []))

    def _save(self) -> None:
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"offsets": self.offsets, "whole": sorted(self.whole)}, fh)
        os.replace(tmp, self.state_path)

    # what changed

    def _rel(self, path: str) -> "str | None":
        path = os.path.abspath(path)
        if path == self.out_dir:
            return ""
        if not path.startswith(self.out_dir + os.sep):
            return None
        return os.path.relpath(path, self.out_dir).replace(os.sep, "/")

    def notice(self, paths) -> None:
        """Take changed paths (files or directories, absolute) into the next delivery."""
        for path in paths:
            rel = self._rel(path)
            if rel is None:
                continue
            if os.path.isdir(path):
                self._notice_tree(path)
            elif rel and shipped_as(rel):
                self.dirty.add(rel)

    def scan(self) -> None:
        """Take every shipped file under ``/out`` into the next delivery."""
        self._notice_tree(self.out_dir)

    def _notice_tree(self, top: str) -> None:
        for dirpath, _dirnames, filenames in os.walk(top):
            for name in filenames:
                rel = self._rel(os.path.join(dirpath, name))
                if rel and shipped_as(rel):
                    self.dirty.add(rel)

    @property
    def pending(self) -> bool:
        """Whether a delivery has something to look at."""
        return bool(self.dirty)

    # what to send

    def plan(self, *, final: bool = False) -> "list[Send]":
        """The members of the next delivery, within the byte budget.

        A file's range starts at its delivered offset (or at 0, sent whole, after a resync
        or when the file is now shorter than what was delivered) and ends after its last
        complete line for a line file -- or at its end, when *final* -- and at its end for a
        bag. A file shipped whole is sent from 0 to its end every time it was written,
        whatever the budget: it is small by construction and a range of it is not a file.
        """
        sends = []
        budget = self.max_bytes
        for rel in sorted(self.dirty):
            if budget <= 0:
                break
            mode = shipped_as(rel)
            path = os.path.join(self.out_dir, rel)
            try:
                with open(path, "rb") as fh:
                    size = os.fstat(fh.fileno()).st_size
                    start = self.offsets.get(rel, 0)
                    whole = mode == WHOLE or rel in self.whole or size < start
                    if whole:
                        start = 0
                    if size <= start:
                        continue
                    fh.seek(start)
                    data = fh.read(size if mode == WHOLE else min(size - start, budget))
            except (FileNotFoundError, IsADirectoryError, PermissionError):
                continue
            complete = len(data) == size - start
            if mode == LINES and not (final and complete):
                cut = data.rfind(b"\n") + 1
                if cut:
                    data = data[:cut]
                elif complete or len(data) < budget:
                    continue  # a partial line only: withheld until it ends
                # else: one line longer than the whole budget goes in pieces
            if not data:
                continue
            budget -= len(data)
            offset = None if whole else start
            sends.append(Send(rel, offset, start + len(data), data))
        return sends

    @staticmethod
    def write_tar(sends, fileobj) -> None:
        """Write *sends* as a stream-mode tar to *fileobj*: one member each."""
        now = time.time()
        with tarfile.open(fileobj=fileobj, mode="w|", format=tarfile.PAX_FORMAT) as tar:
            for send in sends:
                info = tarfile.TarInfo(send.rel)
                info.size = len(send.data)
                info.mode = 0o644
                info.mtime = now
                if send.offset is not None:
                    info.pax_headers = {OFFSET_HEADER: str(send.offset)}
                tar.addfile(info, io.BytesIO(send.data))

    def deliver(self, *, final: bool = False) -> bool:
        """Send what grew. ``True`` when nothing needed sending or the data plane took it.

        On failure the state is unchanged, so the same ranges go in the next attempt.
        """
        sends = self.plan(final=final)
        if not sends:
            self.dirty.clear()
            return True
        try:
            reply = self.transport(lambda fh: self.write_tar(sends, fh))
        except Exception as exc:  # pylint: disable=broad-exception-caught
            log(f"delivery of {len(sends)} file(s) failed: {exc}")
            return False
        self.apply(sends, reply or {})
        return True

    def apply(self, sends, reply: dict) -> None:
        """Record a delivery the data plane answered with *reply*."""
        resync = set(reply.get("resync") or ())
        refused = reply.get("refused") or []
        if refused:
            log(f"the data plane refused: {', '.join(refused[:5])}")
        sent = set()
        for send in sends:
            sent.add(send.rel)
            if send.rel in resync:
                self.whole.add(send.rel)
                self.offsets[send.rel] = 0
                continue
            self.offsets[send.rel] = send.end
            self.whole.discard(send.rel)
        if resync:
            log(f"resync, sending whole next: {', '.join(sorted(resync)[:5])}")
        # A file stays in the next delivery while it may hold more than was sent: a range
        # cut by the budget or by a partial line, or a resync.
        self.dirty = {rel for rel in self.dirty
                      if rel not in sent or rel in resync or self._has_more(rel)}
        self._save()

    def _has_more(self, rel: str) -> bool:
        try:
            size = os.stat(os.path.join(self.out_dir, rel)).st_size
        except OSError:
            return False
        return size > self.offsets.get(rel, 0)


# -- transport --------------------------------------------------------------------------------

class _ChunkedWriter:
    """A write-only file object sending each write as one HTTP/1.1 chunk."""

    def __init__(self, conn):
        self._conn = conn

    def write(self, data) -> int:
        if data:
            self._conn.send(b"%x\r\n" % len(data) + bytes(data) + b"\r\n")
        return len(data)

    def flush(self) -> None:
        pass


def http_transport(data_url: str, campaign_id: str, token: str,
                   timeout: float = REQUEST_TIMEOUT_S):
    """The pod's transport: a chunked ``PUT <data_url>/campaigns/<id>/outputs``."""
    import http.client  # pylint: disable=import-outside-toplevel

    url = urlsplit(data_url)
    if url.scheme not in ("http", "https") or not url.hostname:
        raise ValueError(f"not a data plane address: {data_url!r}")
    path = f"{url.path.rstrip('/')}/campaigns/{quote(campaign_id, safe='')}/outputs"

    def send(write_body) -> dict:
        cls = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
        conn = cls(url.hostname, url.port, timeout=timeout)
        try:
            conn.putrequest("PUT", path, skip_accept_encoding=True)
            conn.putheader("Authorization", f"Bearer {token}")
            conn.putheader("Content-Type", "application/x-tar")
            conn.putheader("Transfer-Encoding", "chunked")
            conn.endheaders()
            write_body(_ChunkedWriter(conn))
            conn.send(b"0\r\n\r\n")
            resp = conn.getresponse()
            body = resp.read()
            if not 200 <= resp.status < 300:
                raise RuntimeError(f"HTTP {resp.status}: {body[:200].decode(errors='replace')}")
            return json.loads(body or b"{}")
        finally:
            conn.close()

    return send


# -- the process ------------------------------------------------------------------------------

def _finished(ipc_dir: str, wait_for, main_seen_at, grace_s: float, now: float):
    """``(done, main_seen_at)``: whether every marker exists, or the grace ran out."""
    main = os.path.join(ipc_dir, f"done.{MAIN_CONTAINER}")
    if not os.path.exists(main):
        return False, None
    if main_seen_at is None:
        main_seen_at = now
    missing = [n for n in wait_for if not os.path.exists(os.path.join(ipc_dir, f"done.{n}"))]
    if not missing:
        return True, main_seen_at
    if now - main_seen_at >= grace_s:
        log(f"WARNING: ending {grace_s:.0f}s after the scenario finished without a marker "
            f"from: {' '.join(missing)}")
        return True, main_seen_at
    return False, main_seen_at


#: Deliveries a final drain makes at most: each carries up to the byte budget.
FINAL_DRAIN_ROUNDS = 64


def drain(agent: Agent) -> bool:
    """Deliver everything every shipped file holds, partial last lines included."""
    agent.scan()
    for _ in range(FINAL_DRAIN_ROUNDS):
        if not agent.deliver(final=True):
            return False
        if not agent.pending:
            return True
    return not agent.pending


def _end(agent: Agent, ipc_dir: str, why: str) -> int:
    log(f"{why}: final drain")
    if not drain(agent):
        log("the final drain did not complete; the uploader's tar carries the rest")
    with open(os.path.join(ipc_dir, f"done.{AGENT_CONTAINER}"), "w", encoding="utf-8"):
        pass
    return 0


def run(agent: Agent, ino: Inotify, ipc_dir: str, wait_for, grace_s: float, stop) -> int:
    """The agent's loop; *stop* is a one-element list a signal handler sets."""
    backoff = 0.0
    next_try = 0.0
    main_seen_at = None
    agent.scan()
    while True:
        now = time.monotonic()
        if stop[0]:
            return _end(agent, ipc_dir, "terminated")
        done, main_seen_at = _finished(ipc_dir, wait_for, main_seen_at, grace_s, now)
        if done:
            return _end(agent, ipc_dir, "every container has finished writing")
        if agent.pending and now >= next_try:
            if agent.deliver():
                backoff = 0.0
            else:
                backoff = min(MAX_BACKOFF_S, max(1.0, backoff * 2))
                next_try = now + backoff
                log(f"retrying in {backoff:.0f}s")
        timeouts = []
        if agent.pending:
            timeouts.append(max(0.0, next_try - time.monotonic()))
        if main_seen_at is not None:
            timeouts.append(max(0.0, main_seen_at + grace_s - time.monotonic()))
        changed = ino.wait(min(timeouts) if timeouts else None)
        if changed and not stop[0]:
            deadline = time.monotonic() + COALESCE_S
            while not stop[0] and (left := deadline - time.monotonic()) > 0:
                changed |= ino.wait(left)
        agent.notice(changed)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Ship the growth of a pod's files to the data plane.")
    parser.add_argument("--grace", type=float, default=90.0,
                        help="seconds after done.main to end without a missing marker")
    parser.add_argument("wait_for", nargs="*",
                        help="sidecar names whose done.<name> markers end the agent")
    args = parser.parse_args(argv)

    missing = [n for n in (DATA_URL_ENV, CAMPAIGN_ID_ENV, TOKEN_ENV) if not os.environ.get(n)]
    if missing:
        log(f"ERROR: missing environment: {' '.join(missing)}")
        return 2
    out_dir = os.environ.get(OUT_DIR_ENV) or OUT_DIR
    ipc_dir = os.environ.get(IPC_DIR_ENV) or IPC_DIR
    transport = http_transport(os.environ[DATA_URL_ENV], os.environ[CAMPAIGN_ID_ENV],
                               os.environ[TOKEN_ENV])
    agent = Agent(out_dir, os.path.join(ipc_dir, STATE_FILE), transport)
    stop = [False]
    with Inotify() as ino:
        ino.add_tree(out_dir)
        ino.add_tree(ipc_dir)

        def on_term(_signum, _frame):
            stop[0] = True
            ino.wake()

        signal.signal(signal.SIGTERM, on_term)
        signal.signal(signal.SIGINT, on_term)
        names = [n for n in args.wait_for if n != MAIN_CONTAINER]
        log(f"watching {out_dir}; ending on done.{MAIN_CONTAINER}"
            + "".join(f" done.{n}" for n in names))
        return run(agent, ino, ipc_dir, names, args.grace, stop)


if __name__ == "__main__":
    sys.exit(main())
