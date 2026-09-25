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

"""A job's log as rows, read from the files its containers write.

Every container of a job writes ``logs/system.log`` (the scenario) or
``logs/system_<name>.log`` (a sidecar) into the job's directory in the campaign, and those
files grow while the job runs: locally the campaign directory is the containers' ``/out``,
and on a cluster the pod's file agent delivers each file's growth as it happens. So one
reader serves a running job and a finished one alike, and there is no second source (a
pod's API log, a byte buffer) that could disagree with what the campaign keeps.

A **row** is one log event: a stamped line and the unstamped lines under it (a traceback
under its ERROR line), grouped exactly as the ``run_log`` table groups them
(:func:`robovast_decode.run_log.parse_container_log`). The **cursor** is where each file
has been read to, in bytes, at a record boundary; it is opaque to callers and carried back
unchanged. A file's last record is held back while the file is still being written, since
the next line may still be one of its continuation lines; once the file has been quiet for
:data:`SETTLE_S`, or the job is over, it is sent.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from robovast.service.interface import JobLogRow
from robovast_decode import log_summary
from robovast_decode.run_log import SRC_STDOUT, TIME_EXACT, TIME_NONE, LogRecord, container_of

#: The job directory's log subdirectory.
LOGS_DIR = "logs"

#: The scenario container's log; every other ``system_<name>.log`` is a sidecar's.
MAIN_LOG = "system.log"

#: How long a file must have been left alone before its last record counts as complete.
SETTLE_S = 2.0


def log_files(job_dir: Path) -> List[Tuple[str, str]]:
    """``[(file name, container)]`` of *job_dir*'s logs: the scenario's first, then the
    sidecars' in name order, so a job reads the same live and finished."""
    log_dir = Path(job_dir) / LOGS_DIR
    try:
        names = sorted(os.listdir(log_dir))
    except FileNotFoundError:
        return []
    found = [(n, container_of(n)) for n in names
             if n.startswith("system") and n.endswith(".log")]
    found = [(n, c) for n, c in found if c]
    return sorted(found, key=lambda item: (item[0] != MAIN_LOG, item[0]))


def encode_cursor(offsets: Dict[str, int]) -> str:
    """The opaque cursor for *offsets* (file name -> bytes read)."""
    if not offsets:
        return ""
    raw = json.dumps(offsets, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> Dict[str, int]:
    """The offsets *cursor* carries; ``ValueError`` for one this service did not issue."""
    if not cursor:
        return {}
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        offsets = json.loads(raw)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"not a job-log cursor: {cursor!r}") from exc
    if not isinstance(offsets, dict) or not all(
            isinstance(k, str) and isinstance(v, int) and v >= 0 for k, v in offsets.items()):
        raise ValueError(f"not a job-log cursor: {cursor!r}")
    return offsets


def _records(data: bytes, start: int, container: str) -> List[Tuple[int, LogRecord]]:
    """``[(byte offset where the record starts, record)]`` for the complete lines in *data*.

    The grouping of :func:`~robovast_decode.run_log.parse_container_log`, with each record's
    position kept so the cursor can stop at a record boundary.
    """
    records: List[Tuple[int, LogRecord]] = []
    position = start
    for raw in data.split(b"\n"):
        line_start, position = position, position + len(raw) + 1
        line = raw.decode("utf-8", errors="replace").rstrip("\r")
        if not line.strip():
            continue
        parsed = log_summary.peel_prefixes(line)
        if parsed.wall_ts is not None:
            records.append((line_start, LogRecord(
                wall_ts=parsed.wall_ts, time_source=TIME_EXACT, container=container,
                node=parsed.node, source=SRC_STDOUT, level=parsed.level,
                message=parsed.message)))
        elif records and records[-1][1].time_source == TIME_EXACT:
            records[-1][1].message += "\n" + line
        else:
            records.append((line_start, LogRecord(
                wall_ts=None, time_source=TIME_NONE, container=container,
                node=parsed.node, source=SRC_STDOUT, level=parsed.level,
                message=parsed.message)))
    return records


def _row(record: LogRecord) -> JobLogRow:
    return JobLogRow(wall_ts=record.wall_ts, time_source=record.time_source,
                     container=record.container, node=record.node, level=record.level,
                     severity=record.severity, message=record.message)


def read_rows(job_dir: Path, cursor: str = "", *, final: bool,
              now: Optional[float] = None) -> Tuple[List[JobLogRow], str, bool]:
    """``(rows, cursor, pending)``: what *job_dir*'s logs hold after *cursor*.

    *final* says the job is over: every byte is sent, the last partial line included.
    Otherwise a file's last record is held back until the file has been quiet for
    :data:`SETTLE_S`, and a line without its newline until the newline arrives. *pending*
    is whether anything was held back.
    """
    now = time.time() if now is None else now
    offsets = decode_cursor(cursor)
    out: List[Tuple[Tuple, JobLogRow]] = []
    pending = False
    for order, (name, container) in enumerate(log_files(job_dir)):
        path = Path(job_dir) / LOGS_DIR / name
        try:
            with open(path, "rb") as handle:
                stat = os.fstat(handle.fileno())
                start = offsets.get(name, 0)
                if stat.st_size < start:
                    start = 0          # replaced by a shorter file: it is read again whole
                handle.seek(start)
                data = handle.read(stat.st_size - start)
        except FileNotFoundError:
            continue
        cut = data.rfind(b"\n") + 1
        complete, partial = data[:cut], data[cut:]
        settled = final or now - stat.st_mtime >= SETTLE_S
        records = _records(complete, start, container)
        end = start + len(complete)
        if partial:
            if final:
                records += _records(partial, end, container)
                end += len(partial)
            else:
                pending = True
        if records and not settled:
            end = records[-1][0]
            records = records[:-1]
            pending = True
        offsets[name] = end
        last_ts = float("-inf")
        for index, (_pos, record) in enumerate(records):
            if record.wall_ts is not None:
                last_ts = record.wall_ts
            out.append(((last_ts, order, index), _row(record)))
    out.sort(key=lambda item: item[0])
    return [row for _key, row in out], encode_cursor(offsets), pending


def runs_of_job(links: dict, job_rel: str) -> List[str]:
    """The ``<config>/<run>`` keys the job-link manifest *links* places in *job_rel*."""
    job_rel = os.path.normpath(job_rel)
    runs = []
    for link, target in links.items():
        if not link.endswith("/job"):
            continue
        run = link[:-len("/job")]
        if os.path.normpath(os.path.join(run, target)) == job_rel:
            runs.append(run)
    return sorted(runs)


def runs_finished(campaign_dir: Path, runs: List[str], now: Optional[float] = None) -> bool:
    """Whether every run in *runs* has its verdict, written at least :data:`SETTLE_S` ago.

    The settle time is the containers' stop grace: a sidecar flushes its last lines after
    the scenario writes ``test.xml``, and ending the log on the verdict alone would cut
    exactly the shutdown output that says whether a recording was saved.
    """
    if not runs:
        return False
    now = time.time() if now is None else now
    for run in runs:
        try:
            mtime = (Path(campaign_dir) / run / "test.xml").stat().st_mtime
        except FileNotFoundError:
            return False
        if now - mtime < SETTLE_S:
            return False
    return True


class LogWatch:
    """Wakes a reader when a job's log files change, instead of it asking on a timer.

    Watches the job's ``logs/`` directory with inotify
    (:class:`robovast.execution.data.file_agent.Inotify`). Before the directory exists -- a job
    that has not started writing -- :meth:`wait` looks for it again at most once a second,
    and watches it from the moment it appears.
    """

    #: How often a job whose ``logs/`` does not exist yet is looked at again.
    APPEAR_S = 1.0

    def __init__(self, job_dir: Optional[Path]):
        self._logs = Path(job_dir) / LOGS_DIR if job_dir is not None else None
        self._inotify = None
        self._attach()

    def _attach(self) -> None:
        if self._inotify is not None or self._logs is None or not self._logs.is_dir():
            return
        from robovast.execution.data.file_agent import \
            Inotify  # pylint: disable=import-outside-toplevel
        inotify = Inotify()
        try:
            inotify.add_tree(str(self._logs))
        except FileNotFoundError:
            inotify.close()
            return
        self._inotify = inotify

    def wait(self, timeout: float) -> None:
        """Return once the logs changed, or after *timeout* seconds."""
        self._attach()
        if self._inotify is None:
            time.sleep(min(timeout, self.APPEAR_S))
            self._attach()
            return
        self._inotify.wait(timeout)

    def close(self) -> None:
        if self._inotify is not None:
            self._inotify.close()
            self._inotify = None


__all__ = ["LOGS_DIR", "MAIN_LOG", "SETTLE_S", "LogWatch", "decode_cursor", "encode_cursor", "log_files",
           "read_rows", "runs_finished", "runs_of_job"]
