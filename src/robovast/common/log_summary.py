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

# -- 1. the line prefix -----------------------------------------------------

#: The level+timestamp+node stamp a ROS node writes ahead of its own message, e.g.
#: ``[INFO] [1785092240.111622055] [scenario_execution_ros]: ``. One definition,
#: used both to recognise a *relay's* stamp and to strip a line's *own*.
_STAMP = (r"\[(?P<level>INFO|WARN|WARNING|ERROR|DEBUG|FATAL)\]\s+"
          r"\[\d+\.\d+\]\s+\[(?P<node>[^\]]+)\]:\s+")

#: A relay prefix: one process forwarding another's output stamps its own container
#: tag and stamp onto every line — e.g. ``robovast  | [INFO] [1785092240.111] [x]: ``.
_RELAY_RE = re.compile(rf"^(?P<container>[\w.-]+\s+\|\s+)?(?P<stamp>{_STAMP})")

#: A line's own stamp, anchored at the start (after any relay prefix is gone).
_STAMP_RE = re.compile(rf"^{_STAMP}")

#: A launch-style ``[node-3] `` tag — the other way a line names its producer.
_LAUNCH_TAG_RE = re.compile(r"^\[(?P<node>[\w.-]+?)-\d+\]\s+")

#: What the payload must itself start with for the relay prefix to be redundant: its
#: own stamp, or a launch tag. Collapsing only then keeps the operation lossless — a
#: line whose payload carries no marker of its own keeps the stamp, since that is the
#: only timestamp it has.
_HAS_OWN_MARKER_RE = re.compile(
    r"^(\[(?:INFO|WARN|WARNING|ERROR|DEBUG|FATAL)\]\s+\[|\[[\w.-]+-\d+\]\s)")


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


def strip_stamp(line: str) -> tuple[str, str]:
    """Split *line* into ``(node, message)``, dropping its own stamp or launch tag.

    ``node`` is ``""`` when the line names no producer. It is kept rather than
    discarded because the same text from two nodes is two findings.
    """
    for pattern in (_STAMP_RE, _LAUNCH_TAG_RE):
        m = pattern.match(line)
        if m:
            return m.group("node"), line[m.end():]
    return "", line


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

_LEVEL_SEVERITY = {"ERROR": "error", "FATAL": "error",
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
    # After collapsing, the leading stamp is the *authoritative* one: a relayed line
    # whose payload has its own stamp keeps the inner one (the producer's), and one
    # without keeps the relay's (its only level). Either way it is now at the front.
    m = _STAMP_RE.match(collapse_relay(line).lstrip())
    if m:
        return _LEVEL_SEVERITY.get(m.group("level"), "other")
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
