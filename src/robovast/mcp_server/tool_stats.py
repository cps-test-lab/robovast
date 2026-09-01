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

"""What the MCP tools were asked, and what they answered.

Every tool call is recorded once, by one middleware, so no tool carries accounting code
of its own -- see :func:`robovast.mcp_server.server._install_tool_stats`.

The record is per call rather than a counter per tool, and that is the whole design
decision here. Counts, error rates and durations could be kept in ~70 upserted rows; the
arguments and the answer could not, and those are what makes a failure debuggable after
the process that served it is gone. So the ranking on the admin page is an aggregate
*over* the log rather than a thing maintained beside it.

**Recording must never fail a tool call.** Every path here swallows its own failure and
logs at debug -- the same contract :class:`robovast.service.event_log.EventLog` states for
itself, for the same reason: what is recorded is a description of the work, not the work.
An index that is unreachable therefore costs the log, never the call.

Rows go to the central index (:mod:`robovast.common.index_db`), buffered: a Postgres
round-trip in front of every tool call would make the accounting more expensive than
some of the tools. The buffer is written out when it fills, when the next call arrives
after :data:`FLUSH_INTERVAL_S`, before every read, and at process exit -- there is no
timer thread, because a thread that exists only to write a handful of rows is a thread
to shut down cleanly, and the read-side flush already makes anything anybody looks at
current. It is bounded and drops rather than grows if flushing keeps failing, which is
the same trade in the other direction.
"""

import atexit
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

from robovast.common import index_db
from robovast.common.errors import IndexUnreachableError

logger = logging.getLogger(__name__)

TABLE = "mcp_tool_call"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    at          double precision NOT NULL,
    tool        text NOT NULL,
    duration_ms double precision NOT NULL,
    ok          boolean NOT NULL,
    args        text NOT NULL DEFAULT '',
    answer      text NOT NULL DEFAULT '',
    actor       text NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS {TABLE}_at ON {TABLE} (at);
"""

#: How much of a payload the log keeps, for ``args`` and for ``answer`` alike. This is the
#: single place that decides it, which makes it also the place to read when asking what the
#: trail could hold: a `write_file` body or a whole campaign summary is cut here and never
#: reaches the table. Cut text is marked, so a short row is legible as truncated rather than
#: as an empty answer.
MAX_LINES = 5
MAX_CHARS = 2000
ELISION = "…[truncated]"

#: How much to keep, on the terms `event_log` sets for itself. Age is what somebody asks for
#: ("last month"); the row cap is the backstop, because one agent loop emits thousands of
#: calls in an hour and age alone would not bound the table. A burst therefore shortens the
#: retained window below MAX_AGE_S -- deliberate, and the panel says so.
MAX_AGE_S = 30 * 24 * 3600
MAX_ROWS = 200_000

#: Flush thresholds. Whichever comes first -- both are checked as a call is recorded, so
#: the interval bounds how stale the table is *while calls keep arriving*; an idle service
#: is made current by the next read or by the exit flush.
FLUSH_ROWS = 50
FLUSH_INTERVAL_S = 5.0

#: How often to prune, in flushes. A DELETE in front of every flush would cost more than the
#: append it protects.
_PRUNE_EVERY = 20

#: Never let an unreachable index grow the buffer without bound. Past this the oldest
#: pending rows are dropped: losing accounting is acceptable, losing the process is not.
MAX_BUFFER = 5000


def render(value: object) -> str:
    """*value* as the text the log stores, before truncation.

    A string stays a string, keeping its real newlines, because the line cap is only
    meaningful against them -- and a tool's answer is usually already prose or a rendered
    table. Anything else becomes compact JSON, where the character cap does the work.

    Deliberately not ``server._short``: that one exists for a *log line* and collapses
    everything to 400 single-line characters, which would cut every payload before this
    module's caps were ever reached and make "a few lines" a promise nothing kept.
    """
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=repr)
    except Exception:  # noqa: BLE001 - a repr always exists; the record must not care
        return repr(value)


def shorten(value: str) -> str:
    """*value* cut to what the log keeps -- see :data:`MAX_LINES` / :data:`MAX_CHARS`."""
    if not value:
        return ""
    text = str(value)
    lines = text.splitlines()
    cut = len(lines) > MAX_LINES
    text = "\n".join(lines[:MAX_LINES])
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS]
        cut = True
    return text + ELISION if cut else text


@dataclass(frozen=True)
class ToolCall:
    """One recorded call."""

    at: float
    tool: str
    duration_ms: float
    ok: bool
    args: str
    answer: str
    actor: str


@dataclass(frozen=True)
class ToolStat:
    """The aggregate of one tool's calls."""

    tool: str
    calls: int
    errors: int
    mean_ms: float
    max_ms: float
    last_at: Optional[float]


class ToolCallLog:
    """Buffered writer and reader for :data:`TABLE`. Never raises at the caller."""

    def __init__(self):
        self._lock = threading.Lock()
        self._buffer: list[ToolCall] = []
        self._flushes = 0
        self._last_flush = time.monotonic()
        self._schema_ready = False

    # -- writing

    def record(self, tool: str, duration_ms: float, ok: bool, *,
               args: str = "", answer: str = "", actor: str = "") -> None:
        """Buffer one call, flushing when the buffer or the clock says to."""
        call = ToolCall(time.time(), tool, duration_ms, ok,
                        shorten(args), shorten(answer), actor or "")
        with self._lock:
            self._buffer.append(call)
            if len(self._buffer) > MAX_BUFFER:
                dropped = len(self._buffer) - MAX_BUFFER
                del self._buffer[:dropped]
                logger.debug("tool call log dropped %d pending rows", dropped)
            due = (len(self._buffer) >= FLUSH_ROWS
                   or time.monotonic() - self._last_flush >= FLUSH_INTERVAL_S)
        if due:
            self.flush()

    def flush(self) -> int:
        """Write the buffer to the index. Returns the number of rows written."""
        with self._lock:
            pending, self._buffer = self._buffer, []
            self._last_flush = time.monotonic()
        if not pending:
            return 0
        try:
            with index_db.connect() as conn:
                self._ensure_schema(conn)
                statement = (f"COPY {TABLE} (at, tool, duration_ms, ok, args, answer, actor) "
                             "FROM STDIN")
                with conn.cursor().copy(statement) as copy:
                    for call in pending:
                        copy.write_row((call.at, call.tool, call.duration_ms, call.ok,
                                        call.args, call.answer, call.actor))
                self._flushes += 1
                if self._flushes % _PRUNE_EVERY == 0:
                    self._prune(conn)
        except IndexUnreachableError as exc:
            # The lane may have no index at all; that is a supported way to run the
            # service, so this is not even a warning the first time.
            logger.debug("tool call log not written: %s", exc)
            return 0
        except Exception:  # noqa: BLE001 - see the module docstring
            logger.debug("could not write %d tool call rows", len(pending), exc_info=True)
            return 0
        return len(pending)

    def _ensure_schema(self, conn) -> None:
        if self._schema_ready:
            return
        conn.execute(_SCHEMA)
        self._schema_ready = True

    def _prune(self, conn) -> None:
        conn.execute(f"DELETE FROM {TABLE} WHERE at < %s", (time.time() - MAX_AGE_S,))
        conn.execute(
            f"DELETE FROM {TABLE} WHERE ctid IN ("
            f"  SELECT ctid FROM {TABLE} ORDER BY at DESC OFFSET %s)", (MAX_ROWS,))

    # -- reading

    def read_stats(self) -> list[ToolStat]:
        """One row per tool that has been called, busiest first."""
        rows = self._query(
            f"SELECT tool, COUNT(*), COUNT(*) FILTER (WHERE NOT ok), AVG(duration_ms), "
            f"MAX(duration_ms), MAX(at) FROM {TABLE} GROUP BY tool ORDER BY COUNT(*) DESC")
        return [ToolStat(tool=r[0], calls=r[1], errors=r[2], mean_ms=float(r[3] or 0.0),
                         max_ms=float(r[4] or 0.0), last_at=r[5]) for r in rows]

    def read_calls(self, *, limit: int = 200, tool: str = "",
                   failed_only: bool = False) -> list[ToolCall]:
        """The log, newest first -- what a person opening the panel wants to see."""
        where, params = [], []
        if tool:
            where.append("tool = %s")
            params.append(tool)
        if failed_only:
            where.append("NOT ok")
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        params.append(max(1, min(int(limit), 2000)))
        rows = self._query(
            f"SELECT at, tool, duration_ms, ok, args, answer, actor FROM {TABLE}"
            f"{clause} ORDER BY at DESC LIMIT %s", tuple(params))
        return [ToolCall(*r) for r in rows]

    def _query(self, sql: str, params: tuple = ()) -> list:
        """Read, treating an absent table as an empty log.

        The table is created by the first flush, so a service that has served no tool call
        yet has none -- that is an empty log, not an error, and it must read as one.
        """
        self.flush()
        with index_db.connect(readonly=True) as conn:
            if not self._table_exists(conn):
                return []
            return conn.execute(sql, params).fetchall()

    @staticmethod
    def _table_exists(conn) -> bool:
        return bool(conn.execute("SELECT to_regclass(%s)", (TABLE,)).fetchone()[0])


#: The one log the middleware writes to and the admin routes read from. Both reach it
#: through this module rather than through the service, because the MCP server is mounted
#: into the service and not the other way round.
LOG = ToolCallLog()

# The buffer is written on a read, so anything anyone looks at is current -- but a service
# that serves a few calls and is then rolled would take them down with it, and a tool call
# nobody can find afterwards is exactly the one somebody goes looking for.
atexit.register(LOG.flush)
