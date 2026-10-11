# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A reader waits on a directory tree that may not exist yet."""

import time

from robovast.service.dir_watch import DirWatch


def test_a_tree_that_appears_later_is_watched_from_then_on(tmp_path):
    root = tmp_path / "logs"
    watch = DirWatch(root)
    try:
        # Nothing to watch yet: the wait is bounded by the look-again interval, not the timeout.
        started = time.monotonic()
        watch.wait(5.0)
        assert time.monotonic() - started < 4.0
        root.mkdir()
        watch.wait(0.0)                 # looks again, and the tree is there now
        (root / "system.log").write_text("x\n")
        started = time.monotonic()
        watch.wait(5.0)
        assert time.monotonic() - started < 4.0
    finally:
        watch.close()


def test_a_tree_that_will_never_exist_only_sleeps():
    watch = DirWatch(None)
    try:
        started = time.monotonic()
        watch.wait(0.2)
        assert 0.15 < time.monotonic() - started < 2.0
    finally:
        watch.close()
