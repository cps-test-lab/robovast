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

"""A campaign's infrastructure log as rows, read from its phase files.

Every infrastructure phase writes its own file under the campaign's ``_execution/``
(:mod:`robovast.common.campaign_logs` names and orders them), and those files grow while
the campaign runs: locally the driver writes them in place, and on a cluster the driving
pod's file agent delivers each file's growth as it happens. So one reader serves a running
campaign and a finished one alike, and there is no second source that could disagree with
what the campaign keeps.

A **row** is one log event: a stamped line and the unstamped lines under it (a traceback
under its ERROR line), grouped as :mod:`robovast.service.job_log` groups a job's log. Two
stamps are read: the campaign log handler's ``<date> <level> <logger>: <message>``
(:func:`robovast.client.logging_config.add_campaign_log_handler`) and the ``[<level>] [<t>]
[<node>]:`` form a run's containers relay into ``controller.log``. An unstamped line with no
stamped record above it is its own row at level ``NOTE`` -- image-build and pip output.

The **cursor** is where each file has been read to, in bytes, at a record boundary, and how
many rows it has yielded; it is opaque to callers and carried back unchanged. A file's last
record is held back while the file is still being written, since the next line may be one
of its continuation lines; once the file has been quiet for :data:`SETTLE_S`, or the
campaign is over, it is sent.

The **filters** are applied while reading: a filtered read still advances the cursor over
the rows it skipped, so a reader narrowing to one phase and one widening to all continue
from the same place.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import time
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

from robovast.common.campaign_logs import (EXECUTION_DIR, INFRA_PHASES, SECTIONS_DIR,
                                           disk_section_names, ordered_sections)
from robovast.service.interface import CampaignLogRow
from robovast_decode import log_summary

#: How long a file must have been left alone before its last record counts as complete.
SETTLE_S = 2.0

#: The level of a row that is an unstamped line: output the phase relayed as it came.
NOTE = "NOTE"

#: The level names a row may carry and their rank, Python logging's. ``WARN`` and
#: ``FATAL`` are the spellings a relayed ROS line uses for two of them.
LEVEL_RANK: Dict[str, int] = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "WARN": 30,
                              "ERROR": 40, "CRITICAL": 50, "FATAL": 50}

#: What the shared keyword classifier's verdict on an unstamped line ranks as.
_SEVERITY_RANK = {"other": LEVEL_RANK["INFO"], "warn": LEVEL_RANK["WARNING"],
                  "error": LEVEL_RANK["ERROR"]}

#: The campaign log handler's line: ``2026-01-01 12:00:00 INFO robovast.x: message``. The
#: level set is closed so a message that happens to open with a date cannot pass for one.
_STAMP_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?P<frac>[,.]\d{1,6})?\s+"
    r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+(?P<logger>\S+): ?(?P<message>.*)$")


class Read(NamedTuple):
    """What one :func:`read_rows` produced."""
    rows: List[CampaignLogRow]
    cursor: str
    #: Whether anything was held back: a record still being written, a partial line.
    pending: bool
    #: Every phase the log has, in log order, whatever the filters kept.
    phases: List[str]


def level_rank(name: str) -> int:
    """The rank of a level name for a ``>=`` filter; ``ValueError`` for one this reader
    does not know. ``warn`` and ``error`` are accepted, so the severity vocabulary the
    other log tools use names the same floor here."""
    rank = LEVEL_RANK.get(name.strip().upper())
    if rank is None:
        raise ValueError(f"unknown level {name!r}; use one of DEBUG, INFO, WARNING, ERROR, "
                         f"CRITICAL")
    return rank


def phase_filter(phase: Optional[str]) -> Optional[str]:
    """The phase name *phase* selects, or ``None`` for every phase.

    ``""``, ``None`` and ``"all"`` select every phase. Raises ``ValueError`` for a name no
    phase has: a silently ignored selector would read as "that phase produced nothing".
    """
    wanted = (phase or "").strip().lower()
    if not wanted or wanted == "all":
        return None
    known = {name.lower(): name for name, _ in INFRA_PHASES}
    if wanted not in known:
        raise ValueError(f"unknown phase {phase!r}; use one of {', '.join(sorted(known))} "
                         f"-- or 'all'")
    return known[wanted]


def encode_cursor(state: Dict[str, List[int]]) -> str:
    """The opaque cursor for *state* (file name -> ``[bytes read, rows yielded]``)."""
    if not state:
        return ""
    raw = json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> Dict[str, List[int]]:
    """The state *cursor* carries; ``ValueError`` for one this service did not issue."""
    if not cursor:
        return {}
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        state = json.loads(raw)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"not a campaign-log cursor: {cursor!r}") from exc
    if not isinstance(state, dict) or not all(
            isinstance(k, str) and isinstance(v, list) and len(v) == 2
            and all(isinstance(n, int) and n >= 0 for n in v) for k, v in state.items()):
        raise ValueError(f"not a campaign-log cursor: {cursor!r}")
    return state


def row_rank(row: CampaignLogRow) -> int:
    """The level rank a *min_level* floor compares against.

    A stamped row's is its level's. A ``NOTE`` row has no level of its own, so it ranks by
    :func:`robovast_decode.log_summary.severity_of` -- the one keyword classifier every
    log surface uses, which rates an unmarked line ``warn`` at most -- so a pip line
    saying ``ERROR: failed`` clears a ``WARNING`` floor and, like everywhere else, is not
    claimed as an error the log never marked.
    """
    if row.level == NOTE:
        return _SEVERITY_RANK[log_summary.severity_of(row.message)]
    return LEVEL_RANK[row.level]


def _stamped(line: str, phase: str) -> Optional[CampaignLogRow]:
    """*line* as a row when it carries a stamp of either form; ``None`` otherwise."""
    match = _STAMP_RE.match(line)
    if match:
        wall_ts = time.mktime(time.strptime(match.group("date"), "%Y-%m-%d %H:%M:%S"))
        if match.group("frac"):
            wall_ts += float("0." + match.group("frac")[1:])
        return CampaignLogRow(phase=phase, wall_ts=wall_ts, level=match.group("level"),
                              logger=match.group("logger"), message=match.group("message"))
    parsed = log_summary.peel_prefixes(line)
    if parsed.wall_ts is None:
        return None
    return CampaignLogRow(phase=phase, wall_ts=parsed.wall_ts,
                          level=parsed.level or "INFO", logger=parsed.node,
                          message=parsed.message)


def _records(data: bytes, start: int, phase: str) -> List[Tuple[int, CampaignLogRow]]:
    """``[(byte offset where the record starts, row)]`` for the complete lines in *data*.

    An unstamped line continues the stamped record above it; with none above it -- a
    file that opens with build output, or output under another ``NOTE`` -- it is its own
    row, since a ``NOTE`` has no stamp to say where it ends.
    """
    records: List[Tuple[int, CampaignLogRow]] = []
    position = start
    for raw in data.split(b"\n"):
        line_start, position = position, position + len(raw) + 1
        line = raw.decode("utf-8", errors="replace").rstrip("\r")
        if not line.strip():
            continue
        row = _stamped(line, phase)
        if row is not None:
            records.append((line_start, row))
        elif records and records[-1][1].wall_ts is not None:
            records[-1][1].message += "\n" + line
        else:
            records.append((line_start, CampaignLogRow(phase=phase, level=NOTE,
                                                       message=line)))
    return records


def _adopt_archived(state: Dict[str, List[int]], sections: List[Tuple[str, str]]) -> None:
    """Carry a live file's cursor entry over to the section it was archived as.

    A finished run of a repeatable phase is moved from its live name to
    ``sections/<seq>-<phase>.log`` before the next run starts. A section the cursor does
    not know whose live name it does know is that move: the entry follows the bytes, so
    they are not read twice, and the live name -- a new run, when it is present again --
    starts from zero. In sequence order, so two moves missed in a row resolve to the
    older section first.
    """
    for _phase, name in sections:
        if not name.startswith(f"{SECTIONS_DIR}/") or name in state:
            continue
        base = name.rsplit("-", 1)[-1]
        if base in state:
            state[name] = state.pop(base)


def read_rows(campaign_dir: Path, cursor: str = "", *, final: bool,
              phase: Optional[str] = None, min_level: Optional[str] = None,
              grep: Optional[str] = None, now: Optional[float] = None) -> Read:
    """What *campaign_dir*'s phase files hold after *cursor*, as rows.

    *final* says the campaign is over: every byte is sent, the last partial line included.
    Otherwise a file's last record is held back until the file has been quiet for
    :data:`SETTLE_S`, and a line without its newline until the newline arrives;
    :attr:`Read.pending` is whether anything was held back.

    Every file is read whether or not the filters keep its rows, because a row's ``seq``
    is its place in the whole log and the cursor must advance over what was skipped.
    *phase*, *min_level* and *grep* are as :meth:`RobovastInterface.get_campaign_logs`
    documents them; ``ValueError`` for a value this reader does not know.
    """
    now = time.time() if now is None else now
    wanted = phase_filter(phase)
    floor = level_rank(min_level) if min_level else None
    try:
        pattern = re.compile(grep, re.IGNORECASE) if grep else None
    except re.error as exc:
        raise ValueError(f"grep={grep!r} is not a valid regular expression: {exc}") from exc
    state = decode_cursor(cursor)
    sections = ordered_sections(disk_section_names(campaign_dir))
    _adopt_archived(state, sections)
    exec_dir = Path(campaign_dir) / EXECUTION_DIR

    out: List[CampaignLogRow] = []
    pending = False
    seq_base = 0
    for phase_name, name in sections:
        offset, emitted = state.get(name, [0, 0])
        try:
            with open(exec_dir / name, "rb") as handle:
                stat = os.fstat(handle.fileno())
                if stat.st_size < offset:
                    offset, emitted = 0, 0     # replaced by a shorter file: read again whole
                handle.seek(offset)
                data = handle.read(stat.st_size - offset)
        except FileNotFoundError:
            continue                           # listed, then archived: the next read adopts it
        # A line without its newline is still being written unless the campaign is over,
        # in which case it is the log's last line and belongs to the record above it.
        cut = len(data) if final else data.rfind(b"\n") + 1
        settled = final or now - stat.st_mtime >= SETTLE_S
        records = _records(data[:cut], offset, phase_name)
        end = offset + cut
        if cut < len(data):
            pending = True
        held = 0
        if records and not settled:
            end = records[-1][0]
            records = records[:-1]
            pending = True
            held = 1
        for index, (_pos, row) in enumerate(records):
            row.seq = seq_base + emitted + index
            if wanted is not None and phase_name != wanted:
                continue
            if floor is not None and row_rank(row) < floor:
                continue
            if pattern is not None and not (pattern.search(row.message)
                                            or pattern.search(row.logger)):
                continue
            out.append(row)
        emitted += len(records)
        state[name] = [end, emitted]
        # The held-back record keeps its number, so the next file's rows start past it.
        seq_base += emitted + held
    phases = list(dict.fromkeys(phase_name for phase_name, _ in sections))
    return Read(out, encode_cursor(state), pending, phases)


class CampaignLogWatch:
    """Wakes a reader when a campaign's phase files change, instead of it asking on a timer.

    Watches the campaign's ``_execution/`` tree with inotify
    (:class:`robovast.execution.data.file_agent.Inotify`), ``sections/`` included as it
    appears. Before the directory exists -- a campaign that has not started writing --
    :meth:`wait` looks for it again at most once a second, and watches it from the moment
    it appears.
    """

    #: How often a campaign whose ``_execution/`` does not exist yet is looked at again.
    APPEAR_S = 1.0

    def __init__(self, campaign_dir: Path):
        self._exec_dir = Path(campaign_dir) / EXECUTION_DIR
        self._inotify = None
        self._attach()

    def _attach(self) -> None:
        if self._inotify is not None or not self._exec_dir.is_dir():
            return
        from robovast.execution.data.file_agent import \
            Inotify  # pylint: disable=import-outside-toplevel
        inotify = Inotify()
        try:
            inotify.add_tree(str(self._exec_dir))
        except FileNotFoundError:
            inotify.close()
            return
        self._inotify = inotify

    def wait(self, timeout: float) -> None:
        """Return once the phase files changed, or after *timeout* seconds."""
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


__all__ = ["LEVEL_RANK", "NOTE", "SETTLE_S", "CampaignLogWatch", "Read", "decode_cursor",
           "encode_cursor", "level_rank", "phase_filter", "read_rows", "row_rank"]
