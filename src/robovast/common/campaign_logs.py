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

"""Assemble a campaign's *infrastructure* log from its per-phase files.

RoboVAST runs three sequential infrastructure phases — variation (config
generation / composition), run (the controller driving batches/runs), and
postprocessing (rosbags → CSV → ``data.db``). Each phase writes its own file
under the campaign's ``_execution/`` directory. Because the phases are strictly
sequential, an earlier phase's file is frozen before the next phase's file
appears, so concatenating them in phase order yields an **append-only** virtual
log: streaming it with a plain byte offset (poll from ``offset``, append the
returned tail, repeat) is stable across polls.

This module is the single seam every surface (web UI / HTTP service, MCP,
cmdline) reads through, so the phase set and the divider format live in exactly
one place. The byte source is injected (``get_bytes``) so the same
concatenation/offset logic serves a local disk read, a cluster pod-scratch read,
and an object-store read without duplication.
"""

import re
from pathlib import Path
from typing import Callable, Optional

#: Infrastructure phases in the order they run, each ``(banner, filename)`` where
#: ``filename`` is relative to the campaign's ``_execution/`` directory. Adding a
#: future infrastructure phase is a one-line change here.
INFRA_PHASES: list[tuple[str, str]] = [
    ("BUILD", "build.log"),
    ("PLUGIN INSTALL", "plugin_install.log"),
    ("VARIATION", "variation.log"),
    ("RUN", "controller.log"),
    ("POSTPROCESSING", "postprocessing.log"),
    ("SHARE", "share.log"),
]

#: Subdirectory under the campaign root holding the phase log files.
EXECUTION_DIR = "_execution"


def phase_banner(name: str) -> str:
    """The textual divider the reader injects before a phase's content."""
    return f"\n===== {name} =====\n"


#: :func:`phase_banner`'s shape, line-anchored, for reading a divider back out of an
#: assembled stream (:func:`split_phases`). Matched against the known phase names, so an
#: ordinary log line of the same shape cannot pass for one.
_BANNER_RE = re.compile(r"^===== (?P<name>.+?) =====$", re.MULTILINE)


def assemble_log(
    get_bytes: Callable[[str], Optional[bytes]],
    offset: int = 0,
    eof: bool = False,
) -> tuple[str, int, bool]:
    """Concatenate the present phase files into one divider-separated stream.

    Args:
        get_bytes: Maps a phase filename (e.g. ``"variation.log"``) to its raw
            bytes, or ``None`` when that phase file does not exist yet. Presence
            is monotonic across a campaign's life (phases appear in order and
            never disappear), which is what keeps the assembled stream
            append-only and the byte offset stable.
        offset: Byte offset into the assembled stream to resume from.
        eof: Whether the campaign is terminal (no phase file will grow further).

    Returns:
        ``(text, next_offset, eof)`` — the slice from *offset* onward, the offset
        to poll from next, and *eof* passed through. Mirrors the ``LogChunk``
        streaming protocol so callers wrap the tuple into their own type.
    """
    segments: list[bytes] = []
    for name, filename in INFRA_PHASES:
        data = get_bytes(filename)
        if data is None:
            continue
        segments.append(phase_banner(name).encode("utf-8"))
        segments.append(data)
    full = b"".join(segments)

    start = max(0, offset)
    tail = full[start:]
    # Decode leniently so a mid-file offset can never raise on a split multi-byte
    # character (it may render one replacement char at a poll boundary — the same
    # tradeoff the previous single-file reader made).
    return tail.decode("utf-8", "replace"), start + len(tail), eof


def split_phases(text: str) -> list[tuple[str, str]]:
    """Split an assembled log into ``[(phase_name, section), ...]``, in order.

    The inverse of :func:`assemble_log`'s divider, kept here beside it so the format has
    exactly one definition — a reader that re-derived the banner would be a second copy to
    drift. Only the known :data:`INFRA_PHASES` names are recognized, so a log line that
    happens to look like a banner cannot invent a phase.

    Each ``section`` is the **exact substring** of *text*, its banner line included, and
    the sections tile *text* end to end: concatenating all of them reproduces the input
    byte for byte, and concatenating a subset splices the log without shifting a single
    line. That is the property callers need — a filtered read that renumbered the lines of
    the parts it kept would report a different total than an unfiltered one.

    Anything before the first divider is returned under the name ``""``, so no bytes are
    dropped even if the stream does not start with a banner.
    """
    known = {phase for phase, _ in INFRA_PHASES}
    marks = []
    for match in _BANNER_RE.finditer(text):
        name = match.group("name")
        if name not in known:
            continue
        # The banner owns the newline in front of it (:func:`phase_banner` writes one), so
        # a normally-assembled stream splits into exactly its phases with no leading
        # remainder — and dropping a section takes its own separator with it.
        start = match.start()
        if start and text[start - 1] == "\n":
            start -= 1
        marks.append((start, name))
    bounds = [m[0] for m in marks] + [len(text)]
    sections = []
    if not marks or marks[0][0] > 0:
        prologue = text[:bounds[0]]
        if prologue:
            sections.append(("", prologue))
    for i, (start, name) in enumerate(marks):
        sections.append((name, text[start:bounds[i + 1]]))
    return sections


def disk_get_bytes(campaign_dir: "Path | str") -> Callable[[str], Optional[bytes]]:
    """A ``get_bytes`` that reads phase files from ``<campaign_dir>/_execution/``.

    Used by every surface that has the campaign on a local filesystem: the local
    service, the cluster service while it is still driving the campaign (pod
    scratch), MCP, and the cmdline. A missing file yields ``None``.
    """
    exec_dir = Path(campaign_dir) / EXECUTION_DIR

    def _read(filename: str) -> Optional[bytes]:
        try:
            return (exec_dir / filename).read_bytes()
        except (FileNotFoundError, NotADirectoryError):
            return None

    return _read


def layered_get_bytes(
    *sources: Callable[[str], Optional[bytes]]
) -> Callable[[str], Optional[bytes]]:
    """A ``get_bytes`` serving each phase file from the first source that HAS it.

    Phases are produced by different processes, which do not all write to the same
    place: on the cluster the controller's phase files land in the service's scratch
    while postprocessing runs against its own fetched campaign root and publishes to
    the object store. Reading one location alone therefore drops whole phases —
    silently, since a missing phase file is also the normal "has not run yet".

    The fallback is on **absence only** (``None``), never on "this copy is shorter".
    A live phase file still being appended to must keep winning over a frozen durable
    copy: the streaming protocol rests on the assembled stream growing monotonically
    (see :func:`assemble_log`), and a poll that returned fewer bytes than the last one
    would leave the client's offset past the end. An existing but empty file is
    *present* and wins for the same reason.
    """
    def _read(filename: str) -> Optional[bytes]:
        for source in sources:
            data = source(filename)
            if data is not None:
                return data
        return None

    return _read


def assemble_log_from_dir(
    campaign_dir: "Path | str", offset: int = 0, eof: bool = False
) -> tuple[str, int, bool]:
    """Convenience wrapper: :func:`assemble_log` over an on-disk campaign dir."""
    return assemble_log(disk_get_bytes(campaign_dir), offset=offset, eof=eof)
