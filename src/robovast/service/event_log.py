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

"""What this service did, kept where a restart cannot take it.

The service already has two in-memory rings -- the log and the usage samples -- and both say
so in their docstrings: bounded and volatile, answering "what is it doing *now*". This answers
a different question, and durability is the whole of the difference.

**The gap it closes.** A refusal exists nowhere. A campaign's failure is on its card and in its
``outcome.json``; a *refused action* -- "cannot retrigger X: ..." -- is composed in the request
that refused it, shown once, and then gone. The browser holds it for thirty seconds. Nothing
else ever held it at all, so "why wouldn't it start yesterday?" was unanswerable by anything
but memory.

**Why a database and not a third ring.** The events worth keeping are the ones a restart
destroys, and a restart is when they are most worth having: the investigation that produced
this file began with a service restart that erased what several campaigns had recorded. A ring
would have lost the evidence of its own incident.

**Where it lives, and why not beside the other service state.** ``<workspaces_root>/events.db``
-- inside a directory the cluster deployment *mounts*, next to ``registry.json``, which is
service state kept there for the same reason. The obvious-looking home, a sibling of the
mounted directories, is the trap ``service_deploy`` documents at length: the results root
landed one directory outside what was covered, so every restart discarded it. A durable log
that is not on a volume is a ring with extra steps.

Bounded all the same, by age and by count: this is an operational record, not an archive, and
an unbounded table on a volume somebody has to size is its own kind of failure.
"""

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

#: Schema version, in ``PRAGMA user_version``. Migrations are append-only, exactly as
#: :mod:`robovast.common.store` does it -- one convention for this, not two.
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS event (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    at         REAL NOT NULL,
    kind       TEXT NOT NULL,
    severity   TEXT NOT NULL,
    actor      TEXT NOT NULL DEFAULT '',
    subject_type TEXT NOT NULL DEFAULT '',
    subject_id TEXT NOT NULL DEFAULT '',
    message    TEXT NOT NULL DEFAULT '',
    payload    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS event_at ON event(at);
"""

#: How much to keep. Age first, because "the last week" is what somebody asks for; the count is
#: the backstop for a service busy enough to make a week enormous.
MAX_AGE_S = 30 * 24 * 3600
MAX_ROWS = 20_000

#: How often to prune, in appends. Pruning on every write would put a DELETE in front of every
#: event for a table whose whole point is that appending is cheap.
_PRUNE_EVERY = 200

EVENTS_FILENAME = "events.db"


@dataclass(frozen=True)
class Event:
    """One thing that happened, as recorded.

    ``kind`` is an open string and ``subject`` is typed rather than being a bare campaign id:
    a workspace, an image build or the service itself are all things worth an event later, and
    widening a table is a migration where adding a ``kind`` is not.
    """

    seq: int
    at: float
    kind: str
    severity: str
    actor: str
    subject_type: str
    subject_id: str
    message: str
    payload: dict


class EventLog:
    """Append-only, durable, bounded. Never raises at the caller."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._appends = 0
        self._conn: Optional[sqlite3.Connection] = None
        self._broken = False

    def _connect(self) -> Optional[sqlite3.Connection]:
        """The connection, opened once.

        One long-lived handle rather than one per append: every call here already holds
        ``_lock``, so there is no concurrency to gain from reopening, and a connect-commit-close
        for each event turns an append from cheap into a file open -- which matters for a thing
        whose entire contract is that recording something must never be worth skipping.
        """
        if self._conn is not None or self._broken:
            return self._conn
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), timeout=5.0, check_same_thread=False)
            conn.executescript(_SCHEMA)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()
            self._conn = conn
            return conn
        except (sqlite3.Error, OSError):
            # A log that cannot open must not take the service with it: everything it records
            # is a description of work, not the work. Latched, so a broken path is diagnosed
            # once rather than on every event.
            self._broken = True
            logger.warning("event log unavailable at %s; events will not be recorded",
                           self.path, exc_info=True)
            return None

    def append(self, kind: str, *, message: str = "", severity: str = "info",
               actor: str = "", subject_type: str = "", subject_id: str = "",
               payload: Optional[dict] = None) -> None:
        """Record one event. Best-effort by construction -- see the class docstring."""
        with self._lock:
            conn = self._connect()
            if conn is None:
                return
            try:
                conn.execute(
                    "INSERT INTO event (at, kind, severity, actor, subject_type, subject_id, "
                    "message, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (time.time(), kind, severity, actor, subject_type, subject_id, message,
                     json.dumps(payload or {}, default=str)))
                conn.commit()
                self._appends += 1
                if self._appends % _PRUNE_EVERY == 0:
                    self._prune(conn)
            except sqlite3.Error:
                logger.warning("could not record a %s event", kind, exc_info=True)

    def _prune(self, conn) -> None:
        conn.execute("DELETE FROM event WHERE at < ?", (time.time() - MAX_AGE_S,))
        conn.execute(
            "DELETE FROM event WHERE seq <= (SELECT MAX(seq) FROM event) - ?", (MAX_ROWS,))
        conn.commit()

    def read(self, *, since: int = 0, limit: int = 200) -> list:
        """Events after *since*, oldest first -- the shape a cursor wants.

        Oldest first because the caller is resuming a position, not browsing: a reader holding
        ``seq`` asks for what came after it and appends. A newest-first view is the UI's job.
        """
        with self._lock:
            conn = self._connect()
            if conn is None:
                return []
            try:
                rows = conn.execute(
                    "SELECT seq, at, kind, severity, actor, subject_type, subject_id, message, "
                    "payload FROM event WHERE seq > ? ORDER BY seq LIMIT ?",
                    (int(since), max(1, min(int(limit), 1000)))).fetchall()
            except sqlite3.Error:
                logger.warning("could not read the event log", exc_info=True)
                return []
        out = []
        for row in rows:
            try:
                payload = json.loads(row[8])
            except ValueError:
                payload = {}
            out.append(Event(seq=row[0], at=row[1], kind=row[2], severity=row[3], actor=row[4],
                             subject_type=row[5], subject_id=row[6], message=row[7],
                             payload=payload))
        return out
