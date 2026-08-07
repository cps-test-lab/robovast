"""Incremental reader for a local job's merged container logs.

The local-lane counterpart to :class:`~robovast.execution.cluster_execution.
cluster_execution.PodLogTail`, sharing its
:class:`~robovast.common.log_tail.MergedLogBuffer` so a campaign reads the same on both
lanes. Where that one pulls trailing windows from the kube API, this one reads byte deltas
from the files a job's containers write side by side into its artifact dir::

    _jobs[/<batch>]/job-<N>/logs/system.log                 the main `robovast` container
    _jobs[/<batch>]/job-<N>/logs/system_simulation.log      a sidecar, one file each
    _jobs[/<batch>]/job-<N>/logs/system_sut.log

Only the first of those was ever read, so the ROS shape's log panel showed
scenario-execution and neither the simulator nor the system under test -- the two
containers whose output actually explains a failed run.

A byte offset per file is a stronger anchor than the cluster's "last line consumed"
search, which exists only because ``since_seconds`` windows overlap; an append-only file
has no overlap. What is *weaker* here is ordering: these files carry no per-line
timestamps, so lines cannot be merge-sorted the way kubelet's can. Interleaving is
therefore at poll granularity -- 0.5s for the web UI's SSE loop, which is fine to watch,
while a single after-the-fact read returns one contiguous block per container. Adding
timestamps at the source was considered and rejected: it would change the durable
``system.log`` artifact that people read directly.
"""

from __future__ import annotations

import threading
from pathlib import Path

from robovast.common.log_tail import MergedLogBuffer, tag_width

#: The main container's log, and the name to tag its lines with. The runtime container is
#: called ``robovast`` on both lanes (the compose service, and the pod's container in
#: ``manifests.py``) -- NOT ``scenario``, which is the container plan's role name. The two
#: lanes must agree here or the same campaign reads differently depending where it ran.
MAIN_LOG = "system.log"
MAIN_CONTAINER = "robovast"

#: Sidecar logs, written by ``secondary_entrypoint.sh`` as ``system_${CONTAINER_NAME}.log``.
_SIDECAR_PREFIX = "system_"
_SIDECAR_SUFFIX = ".log"


def _sidecar_name(filename: str) -> str:
    """``system_simulation.log`` -> ``simulation`` (i.e. the container's ``CONTAINER_NAME``)."""
    return filename[len(_SIDECAR_PREFIX):-len(_SIDECAR_SUFFIX)]


class LocalJobLogTail:
    """Byte-delta reader over one job's ``logs/system*.log`` files.

    Holds a :class:`MergedLogBuffer` that only grows, so a client's byte offset stays
    valid across polls even though the underlying files grow *concurrently* -- which is
    exactly what plain concatenation cannot promise, since an earlier file growing would
    shift every later one.
    """

    def __init__(self):
        self.merged = MergedLogBuffer()
        self.lock = threading.Lock()      # serialize concurrent reads of the same job
        self._consumed: dict[str, int] = {}   # filename -> bytes already folded into buf
        # Sticky: once a job is known to have several containers, every line stays tagged
        # even on a poll where only one of them wrote. Latching back down would leave part
        # of the stream untagged, and the UI would color those lines as the scenario's.
        self._multi = False

    def read(self, log_dir: Path, *, flush_partial: bool) -> bool:
        """Fold each log file's new bytes into the buffer; return whether anything grew.

        *flush_partial* emits a trailing line that has no newline yet. While a run is
        live that line is withheld, so a half-written line is never interleaved between
        two containers'; once the run is over it must be emitted, or a container killed
        mid-line loses its last words -- often the traceback.
        """
        paths = self._log_files(log_dir)
        self._multi = self._multi or len(paths) > 1
        width = tag_width([name for name, _ in paths]) if self._multi else 0

        entries = []  # ((file_order, line_order), container, message)
        for file_order, (name, path) in enumerate(paths):
            for line_order, line in enumerate(self._delta_lines(path, flush_partial)):
                entries.append(((file_order, line_order), name, line))

        self.merged.append(entries, multi=self._multi, width=width)
        return self.merged.grew

    def _log_files(self, log_dir: Path) -> "list[tuple[str, Path]]":
        """``(container, path)`` for each log this job has, main container first."""
        main = log_dir / MAIN_LOG
        found = [(MAIN_CONTAINER, main)] if main.is_file() else []
        try:
            sidecars = sorted(log_dir.glob(f"{_SIDECAR_PREFIX}*{_SIDECAR_SUFFIX}"))
        except OSError:
            # The dir does not exist yet -- the documented startup race, not an error.
            sidecars = []
        found += [(_sidecar_name(p.name), p) for p in sidecars]
        return found

    def _delta_lines(self, path: Path, flush_partial: bool) -> "list[str]":
        """New lines of *path* since the last read, advancing its anchor."""
        consumed = self._consumed.get(path.name, 0)
        try:
            size = path.stat().st_size
            if size < consumed:
                # Truncated or rotated under us. Clamp rather than re-read from 0: this
                # buffer is append-only, and re-reading would duplicate the whole file
                # into a stream a client is already holding an offset into.
                self._consumed[path.name] = size
                return []
            if size == consumed:
                return []
            with open(path, "rb") as fh:
                fh.seek(consumed)
                raw = fh.read()
        except OSError:
            return []

        # Seek/read the delta rather than re-reading the file: a chatty simulator polled
        # every 0.5s would otherwise re-read megabytes per poll, which is the pathology
        # PodLogTail was written to remove and a liveness cost in its own right.
        if not flush_partial:
            cut = raw.rfind(b"\n")
            if cut < 0:
                return []          # no complete line yet; wait for the rest
            raw = raw[:cut + 1]
        self._consumed[path.name] = consumed + len(raw)

        text = raw.decode("utf-8", errors="replace")
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()            # trailing newline is not a line
        return lines
