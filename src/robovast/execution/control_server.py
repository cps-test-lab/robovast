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

"""Live campaign state holder (``ControllerState``).

Every campaign is driven by a :class:`~robovast.execution.controller.CampaignController`
that runs **in the driving process** — the ``vast`` CLI locally, or the
``robovast-service`` for cluster runs. The controller advances a shared
:class:`ControllerState`; the service reads its :meth:`~ControllerState.snapshot`
directly to answer ``GET /campaigns/{id}/status`` (no separate control server, no
pod-IP hop — those existed only when the controller lived in its own pod).

The status *contract* it carries (``Phase``, ``Status`` and the phase-group
predicates) now lives in :mod:`robovast.client.status`, so foundational ``common``
modules can depend on it downward instead of ``common`` reaching up into
``execution``. It is **re-exported here verbatim**, so ``from
robovast.execution.control_server import Status`` (and ``Phase`` etc.) keeps
working everywhere. ``fastapi``/``uvicorn`` are not needed here.
"""

import logging
import threading
import time
from typing import Optional

# The status contract lives in robovast.client -- it is what a client reads a campaign's
# state through, and it must survive an install with no simulator. Re-exported here so
# existing ``control_server`` importers are unaffected (see module docstring).
from robovast.client.status import (  # noqa: F401  # pylint: disable=unused-import
    # Re-exported on purpose: see this module's docstring. flake8 needs the noqa,
    # pylint needs the disable, and neither implies the other.
    RUNNING_PHASES, TERMINAL_PHASES, BudgetItem, Phase, RunProgress, Status,
    failure_detail, is_running, is_terminal)

logger = logging.getLogger(__name__)


# -- shared state -----------------------------------------------------------

class ControllerState:
    """Thread-safe holder the controller writes and the service reads.

    The controller calls :meth:`update` / :meth:`set_phase` at each batch
    boundary (and :meth:`update` for run-level progress within a batch); the
    reader takes a consistent :meth:`snapshot`. :meth:`request_stop` /
    :attr:`stop_requested` back the cooperative ``stop`` (now an in-process call
    from ``client.stop`` rather than an HTTP command).
    """

    def __init__(self, **initial):
        self._lock = threading.Lock()
        self._status = Status(**initial)
        self._stop_event = threading.Event()
        self._progress_suspend = threading.Event()
        self._progress_mark = self._progress_signal()

    def snapshot(self) -> Status:
        with self._lock:
            return self._status.model_copy(deep=True)

    def _progress_signal(self) -> tuple:
        """The values whose change means the campaign *advanced*.

        Exactly the inputs a reader derives ``progress`` from, so "progress moved"
        and "the progress number changed" cannot disagree: completed runs for a batch
        campaign, and batch count plus budget positions for a search (whose overall
        progress is its stopping criteria, not its per-batch run ratio).

        Deliberately **not** ``updated_at``: the progress poller rewrites the same
        counters every few seconds, so any write-based clock ticks forever on a wedged
        run and reports it as healthy.
        """
        st = self._status
        return (st.runs.completed if st.runs else 0, st.batches_done,
                tuple(b.current for b in st.budget))

    def _stamp_progress(self) -> None:
        """Move ``progress_since`` iff the progress signal actually advanced.

        Caller must hold the lock. Mirrors :meth:`set_phase`'s rule — a value
        re-written unchanged must not reset the clock a reader uses to tell "slow"
        from "wedged".
        """
        signal = self._progress_signal()
        if signal != self._progress_mark:
            self._progress_mark = signal
            self._status.progress_since = time.time()

    def update(self, **fields) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(self._status, key, value)
            self._stamp_progress()
            self._status.updated_at = time.time()

    def update_runs(self, **fields) -> None:
        """Merge *fields* into the ``runs`` sub-model, atomically.

        :meth:`update` assigns whole attributes, so ``update(runs={...})`` would drop
        every counter the dict omits. The counters are written from different places
        (the progress poller, the batch-completion tally, the outcome tally) and must
        not clobber each other, and merging under the lock keeps that safe without
        each caller having to read-modify-write.
        """
        with self._lock:
            self._status.runs = self._status.runs.model_copy(update=fields)
            self._stamp_progress()
            self._status.updated_at = time.time()

    def set_phase(self, phase: str, stage: Optional[str] = None) -> None:
        """Advance to *phase*, stamping when it started.

        ``phase_since`` moves only on an actual change, so re-setting the current
        phase (some paths do, defensively) does not keep resetting the clock a
        reader uses to tell "slow" from "wedged".

        Reaching a new phase is itself forward movement, so it also restarts
        ``progress_since``. Without that, a campaign that spent ten minutes in
        ``variation`` would enter ``running`` already carrying a ten-minute-old
        progress clock, and be reported as stalled before its first run had a chance
        to finish.
        """
        with self._lock:
            if phase != self._status.phase:
                self._status.phase_since = time.time()
                self._status.progress_since = self._status.phase_since
            self._status.phase = phase
            if stage is not None:
                self._status.stage = stage
            self._status.updated_at = time.time()

    def request_stop(self) -> None:
        self._stop_event.set()

    @property
    def stop_requested(self) -> bool:
        return self._stop_event.is_set()

    def suspend_progress(self) -> None:
        """Pause the run-progress poller (see CampaignController._poll).

        The poller lists the whole campaign prefix every few seconds. During a batch's
        result download over the same (off-cluster) storage tunnel, those extra list
        calls compound tunnel contention; suspending the poller for the download keeps
        the single transfer uncontended.
        """
        self._progress_suspend.set()

    def resume_progress(self) -> None:
        self._progress_suspend.clear()

    @property
    def progress_suspended(self) -> bool:
        return self._progress_suspend.is_set()
