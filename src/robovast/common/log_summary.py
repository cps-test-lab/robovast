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

"""What a log *line* is, and what a log is *full of*.

Filtering answers "show me the lines matching X". It cannot answer the question
that diagnoses a wedged run — **what is this log full of?** A campaign whose TF was
rejected wholesale emitted one warning 18226 times; a severity ``grep`` returned 25
of them, which read as ordinary noise, while the count that *was* the finding sat in
a field nobody reads. When the flood is the signal, the summary is the diagnosis:
``TF_OLD_DATA … x18226`` is one line instead of thousands.

Three layers, bottom up, all pure (no I/O) so the service, the MCP tools and the CLI
share one set of definitions:

1. **The line prefix** (:func:`collapse_relay`, :func:`strip_stamp`) — RoboVAST logs
   are forwarded, so a line arrives wearing up to two prefixes. Parsing them lives
   here, once, because stripping a redundant prefix for *reading* and stripping it to
   group *counting* are the same parse.
2. **Severity** (:func:`severity_of`) — the one definition in RoboVAST. Callers used
   to invent a severity regex per call site, and two callers with two patterns means
   two answers to "is this run healthy?". :data:`DEFAULT_SEVERITY_PATTERN` is that
   pattern, published.
3. **The summary** (:func:`summarize`) — group lines by their :func:`normalize` d
   shape, count each group, report the most frequent.

``robovast.mcp_server.log_view`` builds its reading view on layers 1–2 rather than
keeping a second copy of the grammar.
"""

import re
from collections import Counter
from typing import NamedTuple

# -- 1. the line prefix -----------------------------------------------------

#: Every level name a stamp may carry. One definition because it is needed in three
#: patterns below and in :data:`_LEVEL_SEVERITY`, and adding a level to some of them is
#: worse than not adding it at all: a level the stamp cannot match loses its timestamp
#: *and* falls through to the keyword scan, so it is silently misclassified rather than
#: merely unrecognised. ``CRITICAL`` is here for Python's stdlib logging, whose
#: ``logger.critical`` is the only common producer of a level rclpy never emits.
_LEVELS = "INFO|WARN|WARNING|ERROR|DEBUG|FATAL|CRITICAL"

#: The level+timestamp+node stamp a ROS node writes ahead of its own message, e.g.
#: ``[INFO] [1785092240.111622055] [scenario_execution_ros]: ``. One definition,
#: used both to recognise a *relay's* stamp and to strip a line's *own*.
_STAMP = (rf"\[(?P<level>{_LEVELS})\]\s+"
          r"\[(?P<t>\d+\.\d+)\]\s+\[(?P<node>[^\]]+)\]:\s+")

#: A relay prefix: one process forwarding another's output stamps its own container
#: tag and stamp onto every line — e.g. ``robovast  | [INFO] [1785092240.111] [x]: ``.
_RELAY_RE = re.compile(rf"^(?P<container>[\w.-]+\s+\|\s+)?(?P<stamp>{_STAMP})")

#: A line's own stamp, anchored at the start (after any relay prefix is gone).
_STAMP_RE = re.compile(rf"^{_STAMP}")

#: A launch-style ``[node-3] `` tag — the other way a line names its producer.
_LAUNCH_TAG_RE = re.compile(r"^\[(?P<node>[\w.-]+?)-\d+\]\s+")

#: An ANSI colour escape at the very start of a line. Producers do this: gz writes
#: ``ESC[1;33mWarning [Utils.cc:132]ESC[0m``. The ESC byte is written as an escape rather
#: than a literal so it stays visible to the next reader of this file.
_SGR_RE = re.compile("^(?:\\x1b\\[[0-9;]*m)+")

#: A stamp or a launch tag — used to tell "this escape hides a marker" from "this escape
#: opens the message", which decides whether :func:`peel_prefixes` may drop it.
_MARKER_RE = re.compile(rf"(?:{_STAMP})|(?:\[[\w.-]+?-\d+\]\s+)")

#: What the payload must itself start with for the relay prefix to be redundant: its
#: own stamp, or a launch tag. Collapsing only then keeps the operation lossless — a
#: line whose payload carries no marker of its own keeps the stamp, since that is the
#: only timestamp it has.
_HAS_OWN_MARKER_RE = re.compile(rf"^(\[(?:{_LEVELS})\]\s+\[|\[[\w.-]+-\d+\]\s)")


def collapse_relay(line: str) -> str:
    """Drop a redundant relay prefix from *line* (see :data:`_HAS_OWN_MARKER_RE`)."""
    m = _RELAY_RE.match(line)
    if not m:
        return line
    rest = line[m.end():]
    if _HAS_OWN_MARKER_RE.match(rest):
        return rest
    # No marker of its own: keep the stamp, drop just the container tag.
    return line[m.end("container"):] if m.group("container") else line


class LogLine(NamedTuple):
    """One log line with its prefixes parsed off — the innermost marker winning.

    ``node``/``level`` are ``""`` and ``wall_ts`` ``None`` for whatever the line did
    not carry, which is the common case for a bash ``echo`` or a gz warning.
    """
    node: str
    level: str
    wall_ts: "float | None"
    message: str


def peel_prefixes(line: str) -> LogLine:
    """Parse *line* into a :class:`LogLine`, stripping every prefix it wears.

    A line reaches a log through up to three layers, each adding its own: the relay's
    stamp, a launch-style process tag, and the producer's own stamp. Nesting is real —
    ``[INFO] [t] [scenario_execution_ros]: [component_container_isolated-8] [ERROR] [t]
    [amcl]: transform failure`` wears all three. So peeling **repeats** until nothing
    matches, and the last marker found wins: it is the producer's own verdict, and the
    outer layers only say who forwarded it.

    Stopping at the first prefix (which this did until it was fixed) reads such a line
    as unmarked, and an ``[ERROR]`` behind a launch tag then classifies as a keyword
    match rather than the error it announces itself to be.
    """
    rest = collapse_relay(line).lstrip()
    node = level = ""
    wall_ts = None
    while True:
        m = _SGR_RE.match(rest)
        if m and _MARKER_RE.match(rest, m.end()):
            # A colour escape *hiding a marker*: every pattern here is anchored, so two
            # invisible bytes in front would cost the line its timestamp while it still
            # looked right on a terminal.
            #
            # Only when a marker actually follows. An escape introducing the message text
            # (gz's `ESC[1;33mWarning [Utils.cc:132]`) is the producer's own colour and stays
            # in the message, because the log panel renders it -- dropping it here would trade
            # one silent loss for another.
            rest = rest[m.end():]
            continue
        m = _STAMP_RE.match(rest)
        if m:
            node, level = m.group("node"), m.group("level")
            wall_ts = float(m.group("t"))
            rest = rest[m.end():]
            continue
        m = _LAUNCH_TAG_RE.match(rest)
        if m:
            # A process tag names a producer but carries no level of its own; a stamp
            # behind it still gets to overwrite this.
            node = m.group("node")
            rest = rest[m.end():]
            continue
        return LogLine(node, level, wall_ts, rest)


def strip_stamp(line: str) -> tuple[str, str]:
    """Split *line* into ``(node, message)``, dropping its stamp and any launch tag.

    ``node`` is ``""`` when the line names no producer. It is kept rather than
    discarded because the same text from two nodes is two findings — and it is the
    *innermost* producer, so a message forwarded through a launch container groups
    with the same message read straight from its node.
    """
    parsed = peel_prefixes(line)
    return parsed.node, parsed.message


# -- 2. severity ------------------------------------------------------------

#: Severity levels, least to most severe. ``other`` means "nothing marked this
#: severe" — not "verified healthy".
SEVERITIES: tuple[str, ...] = ("other", "warn", "error")

#: **The** severity pattern for RoboVAST logs, applied to lines that carry no level
#: marker of their own. Published as a constant so a caller reaching for "just the
#: bad lines" gets the same answer the status does, instead of hand-writing a regex
#: per call site. Prefer the ``min_severity`` control built on it over pasting this
#: into a ``grep``.
DEFAULT_SEVERITY_PATTERN = (
    r"warn|error|fatal|fail|exception|traceback|timed? ?out|"
    r"refus|denied|not found|unavailable")

_SEVERITY_RE = re.compile(DEFAULT_SEVERITY_PATTERN, re.IGNORECASE)

_LEVEL_SEVERITY = {"ERROR": "error", "FATAL": "error", "CRITICAL": "error",
                   "WARN": "warn", "WARNING": "warn",
                   "INFO": "other", "DEBUG": "other"}


def severity_of(line: str) -> str:
    """Classify *line* as ``"error"``, ``"warn"`` or ``"other"``.

    A line's **own level marker wins**: it is the producer's verdict on its own
    message, and it outranks any keyword scan — an ``[INFO]`` line mentioning
    "error" is not an error. Only an unmarked line is classified by
    :data:`DEFAULT_SEVERITY_PATTERN`, and then as ``warn``: without a marker there
    is nothing to separate warn from error, and inventing that distinction would
    report errors a log never claimed.
    """
    # The innermost level is the authoritative one: a relayed line whose payload has its
    # own stamp keeps the inner one (the producer's), and one without keeps the relay's
    # (its only level). :func:`peel_prefixes` is what looks past a launch tag to find it
    # — without that, a nav2 [ERROR] behind ``[component_container_isolated-8]`` reads as
    # unmarked and lands in the keyword branch below as a mere ``warn``.
    level = peel_prefixes(line).level
    if level:
        return _LEVEL_SEVERITY.get(level, "other")
    return "warn" if _SEVERITY_RE.search(line) else "other"


def severity_rank(name: str) -> int:
    """The comparable rank of a severity name, for a ``>=`` filter.

    Raises:
        ValueError: on an unknown name. A silently ignored severity filter would
            read as "no severe lines in this log" — the opposite of the truth.
    """
    try:
        return SEVERITIES.index(name)
    except ValueError:
        raise ValueError(f"unknown severity {name!r}; use one of "
                         f"{', '.join(SEVERITIES)}") from None


# -- 3. the summary ---------------------------------------------------------

#: Substitutions applied to a message before it is used as a grouping key.
#: Everything that varies *per occurrence* of the same message becomes a
#: placeholder — otherwise a message carrying a coordinate or a timestamp yields one
#: group per line and the "summary" is as long as the log. Most specific first: a
#: UUID must not be eaten by the digit rule.
_PLACEHOLDERS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b"), "<uuid>"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<hex>"),
    (re.compile(r"\b[0-9a-fA-F]{12,}\b"), "<hex>"),
    (re.compile(r"-?\d+\.\d+(?:[eE][-+]?\d+)?"), "<n>"),
    (re.compile(r"-?\b\d+\b"), "<n>"),
)

#: How wide a normalized pattern may be. The head of a line identifies the message;
#: a long tail is usually the varying part. Truncation is marked with an ellipsis so
#: a reader knows the pattern is a prefix, not the whole message.
PATTERN_MAX_CHARS = 160

#: Default number of distinct patterns reported — well above what a diagnosis needs,
#: low enough that the summary stays a summary.
DEFAULT_TOP = 20


def normalize(line: str) -> str:
    """Reduce *line* to a grouping key: the same message twice yields the same key."""
    node, message = strip_stamp(collapse_relay(line).strip())
    for pattern, repl in _PLACEHOLDERS:
        message = pattern.sub(repl, message)
    message = " ".join(message.split())
    if len(message) > PATTERN_MAX_CHARS:
        message = message[:PATTERN_MAX_CHARS] + "…"
    return f"[{node}] {message}" if node else message


def summarize(lines: list[str], top: int = DEFAULT_TOP) -> dict:
    """Group *lines* by normalized shape and count each group.

    Args:
        lines: The lines to summarize — already filtered by the caller, if at all.
        top: Report at most this many distinct patterns, most frequent first
            (``0`` = all). ``patterns_total`` always states the true number, so a
            cut list can never be mistaken for the whole picture.

    Returns:
        ``{patterns, patterns_total, severity_counts}``, each pattern being
        ``{pattern, count, severity, example}``. ``example`` is the group's first
        raw line: the pattern says *what* repeats, the example keeps it actionable.
        A group's ``severity`` is that of the most severe line in it.
        ``severity_counts`` counts **lines**, not groups — "18226 warnings" is the
        finding; "1 distinct warning" is only how it is reported.
    """
    counts: Counter = Counter()
    severity_counts: Counter = Counter()
    #: key -> [example line, worst severity rank]
    seen: dict[str, list] = {}

    for line in lines:
        key = normalize(line)
        counts[key] += 1
        rank = SEVERITIES.index(severity_of(line))
        severity_counts[SEVERITIES[rank]] += 1
        entry = seen.get(key)
        if entry is None:
            seen[key] = [line, rank]
        elif rank > entry[1]:
            entry[1] = rank

    return {
        "patterns": [
            {"pattern": key, "count": count,
             "severity": SEVERITIES[seen[key][1]], "example": seen[key][0]}
            for key, count in counts.most_common(top or None)
        ],
        "patterns_total": len(counts),
        "severity_counts": {sev: severity_counts.get(sev, 0) for sev in SEVERITIES},
    }
