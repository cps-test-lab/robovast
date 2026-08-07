"""The append-only merged log buffer behind a job's live log, shared by both lanes.

A job runs several containers at once (the scenario, and — in the ROS shape — a simulator
and a system under test), and the web UI shows their output as ONE stream so the causal
story is legible: the sim died at t=41, so nav2 timed out at t=43. Two containers' output
side by side in separate panels cannot show that.

The wire protocol underneath is a **byte offset into an append-only stream**
(``LogChunk.next_offset``, echoed by the browser as ``Last-Event-ID``). That is what makes
merging N *concurrently growing* sources delicate: concatenating them per read is wrong,
because an earlier section growing shifts every later section and the client's offset then
points into the middle of a line it has already seen. (The campaign log gets away with
plain concatenation only because its phases are strictly sequential — see
:mod:`robovast.common.campaign_logs`.)

The fix is this class: a buffer that is only ever appended to. Each lane computes its own
*delta* — the lines it has not yet consumed from each container — and hands them here; the
buffer tags, orders and appends them, and the client's offset indexes it directly.

The lanes differ only in how they get that delta, which is why the fetching is NOT here:

* cluster — a trailing ``since_seconds`` window of the kube API, deduped against the last
  line consumed per container, ordered by kubelet's per-line RFC3339 timestamp;
* local — a byte offset per ``logs/system*.log`` file on disk, which has no timestamps at
  all, so ordering is per-poll rather than per-line.
"""

from __future__ import annotations


def tag_width(names) -> int:
    """Column width for :func:`tag_line`'s prefixes, so the log body stays aligned."""
    return max((len(n) for n in names), default=0)


def tag_line(name: str, message: str, width: int) -> str:
    """One log line prefixed with its container, as the web UI expects to parse it.

    The UI matches ``/^(\\[[^\\]]+\\]) ?/`` and colors the prefix by hashing the name, so
    this format is a contract with ``StatusView.tsx`` and must stay identical on both
    lanes — a local run and a cluster run of the same campaign should read the same.
    """
    return f"{f'[{name}]'.ljust(width + 2)} {message}"


class MergedLogBuffer:
    """The bytes of a job's merged log, appended to and never rewritten.

    :attr:`buf` is the whole stream so far; a client's byte offset indexes straight into
    it. :attr:`grew` reports whether the last :meth:`append` added anything, which is how
    a caller decides a log has settled — the local lane needs that to avoid declaring EOF
    while a sidecar is still flushing after the scenario finished.
    """

    def __init__(self):
        self.buf = bytearray()
        self.grew = False

    def append(self, entries, *, multi: bool, width: int = 0) -> None:
        """Append *entries*, tagged with their container when there is more than one.

        *entries* is an iterable of ``(sort_key, container_name, message)``. It is sorted
        by ``sort_key`` — a stable sort, so entries a lane cannot order (the local lane's
        untimestamped lines) keep the order they were produced in.

        ``multi`` is the caller's decision, not ``len(entries) > 1``: it must stay True
        once a job is known to have several containers, even on a poll where only one of
        them wrote. Latching it off would leave part of the stream untagged and the UI
        would color those lines as if they came from the scenario.
        """
        ordered = sorted(entries, key=lambda e: e[0])
        rendered = [tag_line(name, message, width) if multi else message
                    for _, name, message in ordered]
        text = "\n".join(rendered)
        self.grew = bool(text)
        if not text:
            return
        # A source that handed us a delta not ending in a newline must not have the next
        # poll's first line glued onto its last one.
        if self.buf and not self.buf.endswith(b"\n"):
            self.buf += b"\n"
        self.buf += text.encode("utf-8", "replace") + b"\n"

    def slice_from(self, offset: int) -> "tuple[str, int]":
        """``(text, next_offset)`` for a client resuming at *offset*."""
        raw = bytes(self.buf)
        return raw[offset:].decode("utf-8", "replace"), len(raw)
