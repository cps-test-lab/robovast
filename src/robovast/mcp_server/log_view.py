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

Logs are read by an LLM, where every line costs context. The same four controls
therefore apply to all of them, so an agent learns them once and can reach a
diagnosis without paying for the whole stream:

* ``grep`` — keep lines matching a regex.
* ``min_severity`` — keep lines the shared classifier rates at least that severe.
  Distinct from ``grep`` on purpose: ``grep`` is free text, this is
  :func:`~robovast.common.log_summary.severity_of`, so a caller stops hand-writing
  a severity regex (and stops getting a different answer than the status does).
* ``tail`` — keep the last N of what survived.
* ``summarize`` — return *distinct patterns with counts* instead of lines.
  Filtering is the wrong instrument when the flood **is** the signal: a run whose TF
  was rejected wholesale matched a severity grep 18226 times and the returned lines
  read as ordinary noise. Summarized, that is one line.

On top of that, each line of a forwarded log carries a relay prefix stamped on by
whatever passed it along; it is dropped where the payload already says the same thing.
The prefix grammar, the severity vocabulary and the pattern counter all live in
:mod:`robovast.common.log_summary`, so the service's campaign health and these tools
cannot disagree about what "an error line" is.

**Nothing is hidden silently.** Every view reports how many lines it left out, so a
filtered read cannot be mistaken for a complete one.
"""

import re

# Imported as a module, not by name, so the parameters below can carry the names a
# caller would choose (``summarize``, ``collapse_relay``) without shadowing them.
from robovast.common import log_summary


def view_log(text: str, *, grep: str = "", min_severity: str = "", tail: int = 0,
             summarize: bool = False, top: int = log_summary.DEFAULT_TOP,
             collapse_relay: bool = True) -> dict:
    """Filter *text* for reading, reporting what was left out.

    Applied in this order, so ``tail`` means "the last N lines *of what matched*" —
    what a caller chasing an error wants:

    1. ``grep`` keeps only lines matching that regex (case-insensitive).
    2. ``min_severity`` (``"warn"`` / ``"error"``) keeps only lines that severe.
    3. ``summarize`` groups and counts what survived — **or**, when it is false,
       ``tail`` keeps the last N lines.
    4. ``collapse_relay`` strips a redundant per-line relay prefix.

    Returns:
        Two shapes, one key apart, because a summary is not a shorter log:

        * lines — ``{content, lines, lines_total, dropped, truncated}``
        * summary — ``{patterns, patterns_total, severity_counts, lines,
          lines_total, dropped}``

        ``content`` is **absent** from a summary rather than empty, which would read
        as "nothing matched". ``dropped`` is how many lines ``grep`` and
        ``min_severity`` together excluded; ``truncated`` marks that ``tail`` cut
        earlier lines, so the caller can page back with the tool's own ``offset``.

    Raises:
        ValueError: if *grep* is not a valid regex, or *min_severity* is not a known
            severity — a silently ignored filter would read as "no such lines in the
            log".
    """
    lines = text.splitlines()
    total = len(lines)

    kept = lines
    if grep:
        try:
            pattern = re.compile(grep, re.IGNORECASE)
        except re.error as e:
            raise ValueError(f"grep={grep!r} is not a valid regular expression: {e}") from e
        kept = [ln for ln in kept if pattern.search(ln)]
    if min_severity:
        floor = log_summary.severity_rank(min_severity)
        kept = [ln for ln in kept
                if log_summary.severity_rank(log_summary.severity_of(ln)) >= floor]
    dropped = total - len(kept)

    if summarize:
        # Counting happens on the *raw* lines: the summarizer normalizes them itself
        # (it needs the prefix to attribute a pattern to its node), so collapsing
        # first would throw that away.
        return {**log_summary.summarize(kept, top=top), "lines": len(kept),
                "lines_total": total, "dropped": dropped}

    truncated = False
    if tail and len(kept) > tail:
        kept = kept[-tail:]
        truncated = True

    if collapse_relay:
        kept = [log_summary.collapse_relay(ln) for ln in kept]

    # Preserve a trailing newline: these tools are also polled incrementally, and a
    # caller appending chunk after chunk would otherwise splice the last line of one
    # onto the first line of the next.
    content = "\n".join(kept)
    if content and text.endswith("\n"):
        content += "\n"

    return {
        "content": content,
        "lines": len(kept),
        "lines_total": total,
        "dropped": dropped,
        "truncated": truncated,
    }
