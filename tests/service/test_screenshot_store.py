# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""The kept-render store is bounded: by age, by count, and to its own directory.

A render is kept so the caller can hand it on, not so a later request can reuse it, so the
store has no reason to grow: each render stays for ``KEEP_S`` and at most ``KEEP_MAX`` stay.
"""

import os
import time

import pytest

from robovast.service import screenshot

CAMPAIGN = "demo-2026-08-09-000000"


@pytest.fixture(name="store")
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBOVAST_SCREENSHOTS", str(tmp_path / "screenshots"))
    return tmp_path


def _render(tmp_path, data=b"png"):
    """A frame laid out as render() lays one out: ``<request dir>/render/frame.png``."""
    import tempfile
    root = tempfile.mkdtemp(prefix="robovast-screenshot-", dir=tmp_path)
    frame = os.path.join(root, "render", screenshot.FRAME_NAME)
    os.makedirs(os.path.dirname(frame))
    with open(frame, "wb") as handle:
        handle.write(data)
    from pathlib import Path
    return Path(frame)


def test_keeping_moves_the_render_and_removes_its_request_directory(store):
    frame = _render(store, b"pixels")
    kept = screenshot.keep(CAMPAIGN, frame)
    assert kept.read_bytes() == b"pixels"
    assert not frame.parent.parent.exists()
    assert screenshot.kept(CAMPAIGN, kept.name) == kept


def test_discard_leaves_a_kept_render_alone(store):
    """The MCP discards whatever path it was given; in-process that path is the kept one."""
    kept = screenshot.keep(CAMPAIGN, _render(store))
    screenshot.discard(kept)
    assert kept.is_file()


def test_a_render_past_its_time_is_no_longer_kept(store):
    kept = screenshot.keep(CAMPAIGN, _render(store))
    later = time.time() + screenshot.KEEP_S + 1
    with pytest.raises(KeyError, match="kept for"):
        screenshot.kept(CAMPAIGN, kept.name, now=later)
    screenshot.prune(now=later)
    assert not kept.exists()
    # An emptied campaign directory goes with its last render.
    assert not kept.parent.exists()


def test_the_oldest_renders_go_first_beyond_the_count(store, monkeypatch):
    monkeypatch.setattr(screenshot, "KEEP_MAX", 2)
    kept = []
    for i in range(3):
        kept.append(screenshot.keep(CAMPAIGN, _render(store)))
        # Distinct ages, so "oldest" is decided by time rather than by directory order.
        made = time.time() - 100 + i
        os.utime(kept[-1], (made, made))
    screenshot.prune()
    assert [p.exists() for p in kept] == [False, True, True]


@pytest.mark.parametrize("campaign, name", [
    (CAMPAIGN, "../../outside.png"),
    (CAMPAIGN, "frame.png"),
    ("..", "0" * 32 + ".png"),
    ("a/b", "0" * 32 + ".png"),
])
def test_a_name_cannot_reach_outside_the_store(store, campaign, name):
    (store / "outside.png").write_bytes(b"secret")
    with pytest.raises(KeyError):
        screenshot.kept(campaign, name)


def test_a_campaign_id_that_is_not_one_directory_is_refused(store):
    with pytest.raises(screenshot.ScreenshotUnavailable):
        screenshot.keep("../escape", _render(store))
