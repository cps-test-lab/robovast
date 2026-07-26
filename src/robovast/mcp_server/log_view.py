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

"""One log view for every MCP log tool.

Logs are read by an LLM, where every line costs context. The same two controls
therefore apply to all of them — ``grep`` and ``tail`` — so an agent learns them once
and can reach an error without paying for the whole stream.

On top of that, each line of a forwarded log carries a relay prefix stamped on by
whatever passed it along; it is dropped where the payload already says the same thing.

**Nothing is hidden silently.** Every view reports how many lines it left out, so a
filtered read cannot be mistaken for a complete one.
"""

import re

#: A relay prefix: one process forwarding another's output stamps its own container
#: tag, level, timestamp and node name onto every line — e.g.
#: ``robovast  | [INFO] [1785092240.111622055] [scenario_execution_ros]: ``.
_RELAY_RE = re.compile(
    r"^(?P<container>[\w.-]+\s+\|\s+)?"
    r"(?P<stamp>\[(?:INFO|WARN|WARNING|ERROR|DEBUG|FATAL)\]\s+"
    r"\[\d+\.\d+\]\s+\[[^\]]+\]:\s+)")

#: What the payload must itself start with for the relay prefix to be redundant: its
#: own level+timestamp, or a launch-style ``[node-3] `` tag. Collapsing only then keeps
#: the operation lossless — a line whose payload carries no marker of its own keeps the
#: stamp, since that is the only timestamp it has.
_HAS_OWN_MARKER_RE = re.compile(
    r"^(\[(?:INFO|WARN|WARNING|ERROR|DEBUG|FATAL)\]\s+\[|\[[\w.-]+-\d+\]\s)")


def _collapse_relay(line: str) -> str:
    """Drop a redundant relay prefix from *line* (see :data:`_HAS_OWN_MARKER_RE`)."""
    m = _RELAY_RE.match(line)
    if not m:
        return line
    rest = line[m.end():]
    if _HAS_OWN_MARKER_RE.match(rest):
        return rest
    # No marker of its own: keep the stamp, drop just the container tag.
    return line[m.end("container"):] if m.group("container") else line


def view_log(text: str, *, grep: str = "", tail: int = 0,
             collapse_relay: bool = True) -> dict:
    """Filter *text* for reading, reporting what was left out.

    Applied in this order, so ``tail`` means "the last N lines *of what matched*" —
    what a caller chasing an error wants:

    1. ``grep`` keeps only lines matching that regex (case-insensitive).
    2. ``tail`` keeps the last N lines.
    3. ``collapse_relay`` strips a redundant per-line relay prefix.

    Returns:
        ``{content, lines, lines_total, dropped, truncated}`` — ``dropped`` is the
        number of lines ``grep`` excluded, ``truncated`` marks that ``tail`` cut
        earlier lines so the caller can page back with the tool's own ``offset``.

    Raises:
        ValueError: if *grep* is not a valid regex — a silently ignored pattern would
            read as "no such lines in the log".
    """
    lines = text.splitlines()
    total = len(lines)

    dropped = 0
    if grep:
        try:
            pattern = re.compile(grep, re.IGNORECASE)
        except re.error as e:
            raise ValueError(f"grep={grep!r} is not a valid regular expression: {e}") from e
        kept = [ln for ln in lines if pattern.search(ln)]
        dropped = len(lines) - len(kept)
        lines = kept

    truncated = False
    if tail and len(lines) > tail:
        lines = lines[-tail:]
        truncated = True

    if collapse_relay:
        lines = [_collapse_relay(ln) for ln in lines]

    # Preserve a trailing newline: these tools are also polled incrementally, and a
    # caller appending chunk after chunk would otherwise splice the last line of one
    # onto the first line of the next.
    content = "\n".join(lines)
    if content and text.endswith("\n"):
        content += "\n"

    return {
        "content": content,
        "lines": len(lines),
        "lines_total": total,
        "dropped": dropped,
        "truncated": truncated,
    }
