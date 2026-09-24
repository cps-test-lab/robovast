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

"""The layout of a campaign's *infrastructure* log: one file per phase.

RoboVAST runs its infrastructure phases in sequence -- an import where the campaign came
from an archive, the image build, plugin install, variation (config generation), run (the
controller driving batches and runs), and then postprocessing, share and a table build,
each of which can run again. Each phase writes its own file under the campaign's
``_execution/`` directory, and a phase that ran again keeps every earlier run under
``_execution/sections/``. This module is the single seam that names those files and orders
them; :mod:`robovast.service.campaign_log` reads them as rows.
"""

import re
from pathlib import Path

#: Infrastructure phases in the order they run, each ``(phase, filename)`` where
#: ``filename`` is relative to the campaign's ``_execution/`` directory. Adding a
#: future infrastructure phase is a one-line change here.
INFRA_PHASES: list[tuple[str, str]] = [
    ("IMPORT", "import.log"),
    ("BUILD", "build.log"),
    ("PLUGIN INSTALL", "plugin_install.log"),
    ("VARIATION", "variation.log"),
    ("RUN", "controller.log"),
    ("POSTPROCESSING", "postprocessing.log"),
    ("SHARE", "share.log"),
    ("TABLES", "tables.log"),
]

#: The phases that happen exactly once, in this order, at the head of the log. They are
#: the campaign itself: it is taken in or built, its inputs are generated, and its runs
#: execute.
HEAD_PHASES: list[tuple[str, str]] = INFRA_PHASES[:5]

#: The phases a campaign can run **again**, any number of times and in any order --
#: postprocess, share, postprocess again. Their position in the log therefore cannot come
#: from a fixed list: it is the order the runs happened in, which only the archived
#: sections record.
REPEATABLE_PHASES: dict[str, str] = {"postprocessing.log": "POSTPROCESSING",
                                     "share.log": "SHARE",
                                     "tables.log": "TABLES"}
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
    log append-only: an archived section is finished and immutable, and the live file
    is always last, so new rows only ever arrive at the end.

    *available* is every filename present under the campaign's ``_execution/``, archived
    ones included as ``sections/<seq>-<phase>.log``; it is read as a set, so overlapping
    listings may simply be concatenated. Anything unrecognised is ignored rather than
    appended: this decides where every reader's row sequence continues, and a stray file
    changing it would move every reader's position.

    A campaign whose repeatable phases each ran once has no archived sections at all, so
    it assembles in the fixed :data:`INFRA_PHASES` order.
    """
    have = set(available)
    out = [(banner, name) for banner, name in HEAD_PHASES if name in have]

    archived = []
    # Over the set, so a caller may union several listings of the same campaign without
    # deduplicating first -- a name repeated there would otherwise repeat its section, and
    # a section counted twice is rows inserted mid-stream on the next read.
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
