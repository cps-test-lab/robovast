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

"""A build's progress as one line: ``label  0%...10%...20%...100%  took``.

Only ever appended to -- no carriage return, no cursor movement -- so the line reads the same
in a terminal, a log file and a notebook cell.
"""

from __future__ import annotations

import sys
import time
from typing import Dict, Optional, TextIO

#: The share of the line each phase of :meth:`~robovast_data.engine.Engine.build` fills:
#: building counts runs and takes most of the time, compacting counts tables.
PHASES: Dict[str, float] = {"build": 0.85, "compact": 0.15}


class ProgressLine:
    """Called with ``(phase, done, total)``; writes each tenth reached."""

    def __init__(self, label: str, stream: Optional[TextIO] = None, step: int = 10,
                 phases: Optional[Dict[str, float]] = None):
        self.stream = stream or sys.stdout
        self.step = step
        self.phases = dict(phases or PHASES)
        self.next = 0
        self.started = time.monotonic()
        self.stream.write(f"{label}  ")
        self.stream.flush()

    def _offset(self, phase: str) -> float:
        offset = 0.0
        for name, share in self.phases.items():
            if name == phase:
                return offset
            offset += share
        raise KeyError(phase)

    def _reach(self, percent: float) -> None:
        while self.next <= percent:
            self.stream.write(f"{'' if self.next == 0 else '...'}{self.next}%")
            self.next += self.step
        self.stream.flush()

    def __call__(self, phase: str, done: int, total: int) -> None:
        share = self.phases[phase]
        self._reach(100.0 * (self._offset(phase) + share * done / max(total, 1)))

    def finish(self, summary: str = "") -> float:
        """Complete the line; the seconds since it started."""
        self._reach(100.0)
        took = time.monotonic() - self.started
        self.stream.write(f"  {took:.0f} s{'  ' + summary if summary else ''}\n")
        self.stream.flush()
        return took


__all__ = ["PHASES", "ProgressLine"]
