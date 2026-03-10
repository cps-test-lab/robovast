#!/usr/bin/env python3
# Copyright (C) 2025 Frederik Pasch
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

"""Common terminal progress bar utilities shared across robovast tools."""

import sys
import time

_BAR_WIDTH = 20
_CLEAR_EOL = "\033[K"


def fmt_size(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MiB"


# Keep internal alias for use within this module
_fmt_size = fmt_size


def _fmt_rate(bps: float) -> str:
    if bps >= 1024 * 1024:
        return f"{bps / 1024 / 1024:.1f} MiB/s"
    if bps >= 1024:
        return f"{bps / 1024:.1f} KiB/s"
    return f"{bps:.0f} B/s"


def make_download_progress_callback(label: str, start: float):
    """Return a ``(received, total)`` callback that prints a download progress bar.

    Args:
        label: Label shown to the left of the bar (e.g. a campaign ID).
        start: ``time.monotonic()`` timestamp of when the download started.

    Returns:
        A callable ``(received: int, total: int) -> None``.
    """
    last_pct = [-1.0]

    def _cb(received: int, total: int) -> None:
        if total <= 0:
            sys.stdout.write(f"\r{label}  {_fmt_size(received)}" + _CLEAR_EOL)
            sys.stdout.flush()
            return
        pct = received / total * 100
        if pct - last_pct[0] < 0.5 and received < total:
            return
        last_pct[0] = pct
        elapsed = max(time.monotonic() - start, 1e-6)
        rate = received / elapsed
        filled = int(_BAR_WIDTH * received / total)
        progressbar = "█" * filled + "░" * (_BAR_WIDTH - filled)
        line = (
            f"{label}  [{progressbar}]  {pct:5.1f}%  "
            f"{_fmt_size(received)}/{_fmt_size(total)}  {_fmt_rate(rate)}"
        )
        sys.stdout.write("\r" + line + _CLEAR_EOL)
        sys.stdout.flush()

    return _cb


class ProgressBar:
    """Context manager for iteration-based terminal progress bars.

    Displays a ``█░`` bar in the same style as the robovast download progress
    bar.  Designed as a lightweight, dependency-free alternative to *tqdm* for
    use in robovast CLI tools.

    Example::

        with ProgressBar(total=42, desc="Creating jobs", unit="job") as pbar:
            for item in items:
                process(item)
                pbar.update()

    Args:
        total: Total number of iterations.
        desc: Description shown to the left of the bar.
        unit: Unit label appended after the count (e.g. ``"job"``).
    """

    def __init__(self, total: int, desc: str = "", unit: str = "it") -> None:
        self.total = total
        self.desc = desc
        self.unit = unit
        self._current = 0
        self._start: float = 0.0

    # ------------------------------------------------------------------
    # Context manager protocol
    # ------------------------------------------------------------------

    def __enter__(self) -> "ProgressBar":
        self._start = time.monotonic()
        self._render()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if exc_type is None:
            # Ensure we show 100 % on clean exit.
            self._current = self.total
            self._render()
        sys.stdout.write("\n")
        sys.stdout.flush()
        return False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(self, n: int = 1) -> None:
        """Advance the counter by *n* and redraw the bar."""
        self._current = min(self._current + n, self.total)
        self._render()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _render(self) -> None:
        pct = (self._current / self.total * 100) if self.total > 0 else 0.0
        filled = int(_BAR_WIDTH * self._current / self.total) if self.total > 0 else 0
        bar = "█" * filled + "░" * (_BAR_WIDTH - filled)
        elapsed = max(time.monotonic() - self._start, 1e-6)
        rate = self._current / elapsed if self._current > 0 else 0.0
        rate_str = f"  {rate:.1f} {self.unit}/s" if rate > 0 else ""
        line = (
            f"{self.desc}  [{bar}]  {pct:5.1f}%"
            f"  {self._current}/{self.total} {self.unit}{rate_str}"
        )
        sys.stdout.write("\r" + line + _CLEAR_EOL)
        sys.stdout.flush()
