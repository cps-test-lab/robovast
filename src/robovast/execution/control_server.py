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
from robovast.client.status import (  # noqa: F401  # pylint: disable=unused-import; Re-exported on purpose: see this module's docstring. flake8 needs the noqa,; pylint needs the disable, and neither implies the other.
    RUNNING_PHASES, TERMINAL_PHASES, BudgetItem, Phase, RunProgress, Status, failure_detail,
    is_running, is_terminal)

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

        For the same reason the ``time`` budget row is **excluded**. Its value is a pure
        function of wall-clock, so it advances whether or not the campaign does -- and a
        signal that always advances is not a signal. Today it is only rewritten at a batch
        boundary, where ``batches_done`` moves anyway, so this changes no verdict; it is here
        so that publishing elapsed more often can never silently make every time-budgeted
        search un-stallable. Only facts whose change IS evidence of progress belong in this
        tuple.
        """
        st = self._status
        return (st.runs.completed if st.runs else 0, st.batches_done,
                tuple(b.current for b in st.budget if b.kind != "time"))

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
            was_queued = self._status.waiting_for_capacity
            for key, value in fields.items():
                setattr(self._status, key, value)
            # Leaving a capacity queue is forward movement, and by exactly the rule
            # ``set_phase`` states for a phase change: a campaign that queued for longer than
            # one run's budget would otherwise enter its first run already carrying a
            # progress clock older than the deadline, and be called stalled before that run
            # could possibly finish. ``waiting_for_capacity`` suppresses the verdict only
            # WHILE the wait lasts, so without this the accusation simply lands the moment
            # the wait ends -- which is the worst moment, because the campaign has just
            # started doing the thing it was waiting to do.
            #
            # A campaign held behind its own calibration probe hits this first, but it is
            # not calibration-specific: any campaign queued behind another for longer than
            # the per-run budget is accused the same way, which is the multi-campaign case
            # admission exists to serve.
            if was_queued and not self._status.waiting_for_capacity:
                self._status.progress_since = time.time()
                self._progress_mark = self._progress_signal()
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

        **A stage belongs to the phase that set it**, so a phase change clears it unless the new
        call names one. Without that, ``stage`` outlived its phase for the rest of the campaign's
        life: a campaign that had waited for an image reported "waiting for image(s) simulation,
        sut" while it ran, while it postprocessed, and after it finished -- a sentence that was
        true once and then read as a live statement about a campaign that was already over. The
        one call site that cleared it by hand (``stage=""`` on entering ``starting``) is the
        workaround this replaces.

        Re-setting the *same* phase keeps the stage: some paths do that defensively, and clearing
        there would wipe a stage the same phase had just set.
        """
        with self._lock:
            if phase != self._status.phase:
                self._status.phase_since = time.time()
                self._status.progress_since = self._status.phase_since
                self._status.stage = None
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


def stage_output_callback(state, log):
    """An ``output_callback`` that logs a step's line *and* publishes it as ``Status.stage``.

    Postprocessing narrates itself through that callback already — one line per step, plus the
    ``data.db`` builder's run counter — and that narration is the only account a reader gets of
    a phase with **no run counter of its own**. A campaign holds ``postprocessing`` with
    ``progress`` pinned and every run tally frozen, so on the evidence the campaign view had,
    converting a large campaign's rosbags and a wedged step looked identical for as long as it
    took. ``stage`` is the field for exactly that ("a live marker string"), and ``set_phase``
    clears it on the next phase change, so a marker cannot outlive what it described.

    The line is published **verbatim**. Which of them matters is the reader's question, each
    step's summary is replaced by the next step's ``Executing`` line within seconds, and a
    producer-side filter would be a second place obliged to know what postprocessing's steps
    are called. Renderers are expected to truncate; the field is one line, not a log tail.

    *state* may be ``None`` — the re-run entry points postprocess a campaign nothing is
    driving, and there is no live status to publish into. Then this is just *log*, so a caller
    never has to ask which case it is in.
    """
    if state is None:
        return log

    def publish(msg):
        log(msg)
        # Not set_phase: the phase is already ``postprocessing`` and re-setting it would
        # reset ``phase_since``, turning the age of the phase into the age of its last step.
        state.update(stage=str(msg))

    return publish
