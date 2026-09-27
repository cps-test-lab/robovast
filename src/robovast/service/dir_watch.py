# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A reader that is woken when a directory tree changes, instead of asking on a timer.

What a live log stream waits on, whichever files it reads: the campaign log's phase files
under ``_execution/`` and a job's ``logs/`` both change by being appended to, and both may
not exist yet when the first reader arrives.
"""

import time
from pathlib import Path
from typing import Optional


class DirWatch:
    """Wakes a reader when files under *root* change, instead of it asking on a timer.

    Watches the tree with inotify (:class:`robovast.execution.data.file_agent.Inotify`),
    directories created below it included as they appear. Before *root* exists -- a job or
    a campaign that has not started writing -- :meth:`wait` looks for it again at most once
    a second, and watches it from the moment it appears. ``None`` is a tree that will never
    exist, so :meth:`wait` only sleeps.
    """

    #: How often a root that does not exist yet is looked at again.
    APPEAR_S = 1.0

    def __init__(self, root: Optional[Path]):
        self._root = Path(root) if root is not None else None
        self._inotify = None
        self._attach()

    def _attach(self) -> None:
        if self._inotify is not None or self._root is None or not self._root.is_dir():
            return
        from robovast.execution.data.file_agent import \
            Inotify  # pylint: disable=import-outside-toplevel
        inotify = Inotify()
        try:
            inotify.add_tree(str(self._root))
        except FileNotFoundError:
            inotify.close()
            return
        self._inotify = inotify

    def wait(self, timeout: float) -> None:
        """Return once the tree changed, or after *timeout* seconds."""
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


__all__ = ["DirWatch"]
