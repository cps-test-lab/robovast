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

"""The service's own recent log, held in memory so a client can read it back.

A service writes to stderr, and stderr is not readable back -- so "what has this thing
been doing?" had no answer from any client at all. Several failures in this codebase are
annotated with exactly that dead end (a scene cache retrying forever, a build whose reason
"lived only in the service log", a generator whose own message never left the process).
This is the source those comments were missing.

It is a bounded ring the ``robovast`` logger fills as records are emitted, sliced by byte
offset so it serves the same :class:`~robovast.service.interface.LogChunk` protocol every
other live log here already speaks -- which is what lets ``app.py``'s ``_sse_log_stream``
and the browser's ``Last-Event-ID`` resume work on it unchanged.

**Bounded and volatile, deliberately.** It holds the last :data:`_MAX_BYTES` and nothing
survives a restart. A durable service log is a real system -- rotation, a disk budget, a
retention policy -- and this exists to answer "what is it doing *now*", which a few hundred
kilobytes answers. ``kubectl logs -p deploy/robovast-service`` remains the way to read a
container that has already died, and the only way: a ring inside a process cannot outlive
the process it is inside.

Not read from the pod log, though in the pod the two hold the same records. Kubernetes
serves a pod log as a sliding ``since_seconds`` window rather than a stream, which is why
:class:`~robovast.execution.cluster_execution.PodLogTail` needs per-container anchors, a
re-anchor when the window slides, and a documented branch where lines are lost. All of that
exists because a *job* log has no other source. A ring the process fills itself is honestly
append-only, so ``next_offset`` means what it says on both lanes with one implementation.
"""

import logging
import threading

from robovast.service.interface import LogChunk

#: How much log to keep. Sized to hold a busy few minutes rather than a session: the ring
#: is interleaved across every campaign the service drives (the per-thread filter is only
#: on the per-campaign file handlers), so during a large campaign this is a short window.
#: Raise it if that bites; do not add filtering, which would make the service log lie about
#: what the service did.
_MAX_BYTES = 512 * 1024

#: Loggers to record. ``robovast`` catches every ``robovast.*`` record by propagation and
#: leaves ``botocore``/``urllib3``/``kubernetes`` chatter out, exactly as
#: ``add_campaign_log_handler`` does and for the same reason. ``uvicorn.error`` is here
#: because a request that blew up in the framework is a thing this service did, and it is
#: the half of the stderr stream that is not ours.
_LOGGERS = ("robovast", "uvicorn.error")

#: Its own format, not the console's. ``setup_logging``'s INFO format is bare
#: ``%(message)s`` -- right for a terminal someone is watching, useless in a log read
#: minutes later, where the question is always *when*.
_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_DATEFMT = "%H:%M:%S"


class _Ring:
    """The bytes, and the absolute offset the first of them sits at."""

    def __init__(self):
        self._lock = threading.Lock()
        self._buf = bytearray()
        #: Absolute offset of ``_buf[0]`` in the whole stream. Advances as bytes are
        #: evicted, which is what keeps offsets meaningful after the window has slid.
        self._base = 0

    def append(self, text: str) -> None:
        data = text.encode("utf-8", "replace")
        with self._lock:
            self._buf += data
            excess = len(self._buf) - _MAX_BYTES
            if excess > 0:
                del self._buf[:excess]
                self._base += excess

    def read(self, offset: int) -> LogChunk:
        with self._lock:
            end = self._base + len(self._buf)
            if offset < self._base:
                # Resuming below the window: those bytes are gone. Serve from the window's
                # start, so the reader sees a gap rather than a duplicate -- the same trade
                # PodLogTail makes when kubelet rotates its anchor away, and for the same
                # reason: a consistent stream beats a complete one.
                start = 0
            elif offset > end:
                # Ahead of everything we have, which means this is not the stream that
                # reader was following: the process restarted and offsets began again at 0.
                # Resync from the window's start rather than returning empty forever, which
                # is what a naive clamp would do and would read as a service gone silent.
                start = 0
            else:
                start = offset - self._base
            return LogChunk(text=self._buf[start:].decode("utf-8", "replace"),
                            next_offset=end,
                            # Never True: a running service's log has no end, and claiming
                            # one would tell _sse_log_stream to close a stream that should
                            # keep tailing.
                            eof=False)


_RING = _Ring()


class _RingHandler(logging.Handler):
    """Formats a record into :data:`_RING`. Never raises into the emitting call."""

    def emit(self, record):
        try:
            _RING.append(self.format(record) + "\n")
        except Exception:  # noqa: BLE001  pylint: disable=broad-except
            self.handleError(record)


_INSTALLED = False


def install() -> None:
    """Attach the ring to :data:`_LOGGERS`. Idempotent.

    Called from ``build_app`` rather than from ``setup_logging``, which runs in every
    ``vast`` invocation: a ring is only worth filling where something serves it.

    The handler's level is left at ``NOTSET`` on purpose, so each logger's effective level
    gates it and the ring holds exactly what the console held -- one place to change the
    verbosity of both.
    """
    global _INSTALLED  # pylint: disable=global-statement
    if _INSTALLED:
        return
    handler = _RingHandler()
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
    for name in _LOGGERS:
        logging.getLogger(name).addHandler(handler)
    _INSTALLED = True


def read(offset: int = 0) -> LogChunk:
    """The log from byte *offset* on, in the ``fetch(offset) -> LogChunk`` shape.

    That signature is the point: it is what ``app.py``'s ``_sse_log_stream`` already takes,
    so streaming this needs no new machinery.
    """
    return _RING.read(max(0, offset))
