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
postprocessing (rosbags → CSV → the results index). Each phase writes its own file
under the campaign's ``_execution/`` directory. Because the phases are strictly
sequential, an earlier phase's file is frozen before the next phase's file
appears, so concatenating them in phase order yields an **append-only** virtual
log: streaming it with a plain byte offset (poll from ``offset``, append the
returned tail, repeat) is stable across polls.

This module is the single seam every surface (web UI / HTTP service, MCP,
cmdline) reads through, so the phase set and the divider format live in exactly
one place. The byte source is injected (``get_bytes``) so the same
concatenation/offset logic serves a disk read and any other byte source without
duplication.
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

#: The phases that happen exactly once, in this order, at the head of the log. They are
#: the campaign itself: it is built, its inputs are generated, and its runs execute.
HEAD_PHASES: list[tuple[str, str]] = INFRA_PHASES[:4]

#: The phases a campaign can run **again**, any number of times and in any order --
#: postprocess, share, postprocess again. Their position in the stream therefore cannot
#: come from a fixed list: a rerun of one that sits earlier in such a list inserts bytes
#: ahead of a section already written, and the assembled stream stops being append-only.
#: A reader streaming it by byte offset has consumed past that point and never sees them,
#: so a live postprocess appears frozen at whatever ran last.
REPEATABLE_PHASES: dict[str, str] = {"postprocessing.log": "POSTPROCESSING",
                                     "share.log": "SHARE"}

#: Where a finished run of a repeatable phase is kept, so the next run starts an empty
#: file instead of replacing it: ``_execution/sections/<seq>-<phase>.log``. The sequence
#: is allocated when a run starts, so the name records the order things actually happened
#: -- which is the only thing that can order them.
SECTIONS_DIR = "sections"

_SECTION_RE = re.compile(r"^(?P<seq>\d{4})-(?P<base>[a-z_]+\.log)$")


def section_name(seq: int, base: str) -> str:
    """The archived name for run *seq* of the phase whose live file is *base*."""
    return f"{SECTIONS_DIR}/{seq:04d}-{base}"


def next_section_seq(existing: "list[str]") -> int:
    """The next free sequence number, given the section names that already exist."""
    used = [int(m.group("seq")) for m in
            (_SECTION_RE.match(name.rsplit("/", 1)[-1]) for name in existing) if m]
    return max(used, default=0) + 1


def ordered_sections(available: "list[str]") -> list[tuple[str, str]]:
    """``[(banner, filename), ...]`` in the order the stream must present them.

    The head phases first, in their fixed order -- they run once and cannot move. Then
    every archived section by its sequence number, then whichever repeatable phase is
    currently live. That is the order the work happened in, and it is what makes the
    stream append-only: an archived section is finished and immutable, and the live file
    is always last, so new bytes only ever arrive at the end.

    *available* is every filename present under the campaign's ``_execution/``, archived
    ones included as ``sections/<seq>-<phase>.log``; it is read as a set, so overlapping
    listings may simply be concatenated. Anything unrecognised is ignored
    rather than appended: this decides a byte offset's meaning, and a stray file changing
    it would corrupt every reader's position.

    A campaign whose repeatable phases each ran once has no archived sections at all, so
    it assembles in the fixed :data:`INFRA_PHASES` order.
    """
    have = set(available)
    out = [(banner, name) for banner, name in HEAD_PHASES if name in have]

    archived = []
    # Over the set, so a caller may union several listings of the same campaign without
    # deduplicating first -- a name repeated there would otherwise repeat its section, and
    # a section counted twice is bytes inserted mid-stream on the next poll.
    for name in sorted(have):
        match = _SECTION_RE.match(name.rsplit("/", 1)[-1])
        if match and match.group("base") in REPEATABLE_PHASES:
            archived.append((int(match.group("seq")), name,
                             REPEATABLE_PHASES[match.group("base")]))
    out += [(banner, name) for _seq, name, banner in sorted(archived)]

    # The live files last. Where both are present -- a campaign whose runs were never
    # archived -- their fixed order is the only one the files carry.
    out += [(banner, base) for base, banner in REPEATABLE_PHASES.items()
            if base in have]
    return out

#: Subdirectory under the campaign root holding the phase log files.
EXECUTION_DIR = "_execution"


def disk_section_names(campaign_dir: "Path | str") -> list[str]:
    """The phase filenames present under ``<campaign_dir>/_execution/``.

    Names an archived section as ``sections/<seq>-<phase>.log``, the same relative form
    :func:`section_name` produces and :func:`ordered_sections` reads, so a caller can union
    a disk listing with names it knows from elsewhere without translating either. Only the
    two levels that hold phase files are walked -- everything a campaign keeps under
    ``_execution/`` is one of them, and a recursive walk would pay for the run tree.

    A directory that is not there yields ``[]``: a campaign this service never had on disk
    is the normal case for a reader, not an error.
    """
    exec_dir = Path(campaign_dir) / EXECUTION_DIR
    names = []
    for path in (exec_dir, exec_dir / SECTIONS_DIR):
        try:
            entries = sorted(p.name for p in path.iterdir() if p.is_file())
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            continue
        prefix = "" if path == exec_dir else f"{SECTIONS_DIR}/"
        names += [f"{prefix}{name}" for name in entries]
    return names


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
    sections: "Optional[list[tuple[str, str]]]" = None,
) -> tuple[str, int, bool]:
    """Concatenate the present phase files into one divider-separated stream.

    Args:
        get_bytes: Maps a phase filename (e.g. ``"variation.log"``) to its raw
            bytes, or ``None`` when that phase file does not exist yet. Presence
            is monotonic across a campaign's life -- a section appears and never
            disappears -- and *sections* orders them so that the only one still
            growing is last. Together that is what keeps the assembled stream
            append-only and the byte offset stable, which the streaming protocol
            requires: bytes inserted ahead of a section a reader has already
            consumed are bytes it can never be shown.
        offset: Byte offset into the assembled stream to resume from.
        eof: Whether the campaign is terminal (no phase file will grow further).
        sections: The ``(banner, filename)`` order to present, from
            :func:`ordered_sections`. Omitted, the fixed :data:`INFRA_PHASES` order is
            used -- correct for a campaign whose repeatable phases each ran once, and
            for every caller reading a finished campaign in one pass.

    Returns:
        ``(text, next_offset, eof)`` — the slice from *offset* onward, the offset
        to poll from next, and *eof* passed through. Mirrors the ``LogChunk``
        streaming protocol so callers wrap the tuple into their own type.
    """
    segments: list[bytes] = []
    for name, filename in (INFRA_PHASES if sections is None else sections):
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

    Used by every surface that has the campaign on a local filesystem: the service,
    MCP, and the cmdline. A missing file yields ``None``.
    """
    exec_dir = Path(campaign_dir) / EXECUTION_DIR

    def _read(filename: str) -> Optional[bytes]:
        try:
            return (exec_dir / filename).read_bytes()
        except (FileNotFoundError, NotADirectoryError):
            return None

    return _read


def assemble_log_from_dir(
    campaign_dir: "Path | str", offset: int = 0, eof: bool = False
) -> tuple[str, int, bool]:
    """:func:`assemble_log` over an on-disk campaign dir, archived sections included.

    The order comes from what is on disk (:func:`disk_section_names` ->
    :func:`ordered_sections`) rather than the fixed :data:`INFRA_PHASES` list, because a
    campaign that ran a repeatable phase twice has a section the fixed list cannot place:
    it names the two live filenames and nothing under ``sections/``. Reading it that way
    would drop every earlier run of a phase from the stream -- and shorten the stream the
    moment one of them was archived, under a reader that had already consumed past it.

    A campaign that never repeated a phase has no sections, and the order this produces is
    then exactly the fixed one, so its offsets are the same whichever order is asked for.
    """
    return assemble_log(disk_get_bytes(campaign_dir),
                        offset=offset, eof=eof,
                        sections=ordered_sections(disk_section_names(campaign_dir)))
