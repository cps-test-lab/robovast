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

"""The campaign status *contract* — phase vocabulary + the ``Status`` model.

These are pure data types (stdlib + pydantic only) shared by every layer: the
controller that advances them, the service that serves them, the MCP/CLI clients
that render them, and ``common`` readers that persist/recover them. They live in
``common`` so that a foundational module (e.g. ``campaign_data`` reading a durable
``outcome.json`` back into a ``Status``) can depend on the contract *downward*,
instead of ``common`` reaching up into ``execution`` for it.

The live, thread-owning holder that the controller writes —
:class:`~robovast.execution.control_server.ControllerState` — stays in
``execution`` (it needs no wider audience and carries threading state); it imports
these models from here. ``execution.control_server`` also re-exports every name in
this module, so the historical ``from robovast.execution.control_server import
Status`` keeps working.
"""

import time
from enum import StrEnum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


# -- phase vocabulary -------------------------------------------------------

class Phase(StrEnum):
    """The campaign lifecycle vocabulary carried by ``Status.phase``.

    A ``StrEnum`` so members *are* their plain string value: the wire format is
    unchanged (JSON still sees ``"finished"``), existing string comparisons keep
    working, and set membership against raw strings does too. Prefer the group
    predicates (:func:`is_terminal` / :func:`is_running`) over re-listing phase
    names at a call site — that re-listing had drifted into several divergent
    "terminal" sets across the CLI, service, and MCP plugins.

    ``Status.phase`` stays typed ``str`` on purpose (the field is deliberately
    open, so a future ``stage``-like marker slots in without a schema change);
    this enum is the *known* vocabulary, not a lock on the field.
    """
    # -- live: the campaign is still working ------------------------------
    # Ordered by when they occur: image build (if any) → registration → plugin
    # install (if any) → config-variation expansion (batch) → the run loop → finish
    # → postprocess → share. ``building``, ``plugin install`` and ``variation`` precede
    # ``running`` and exist so the pre-run steps are observable rather than a blank
    # "starting".
    BUILDING = "building"
    STARTING = "starting"
    PLUGIN_INSTALL = "plugin install"
    VARIATION = "variation"
    RUNNING = "running"
    FINISHING = "finishing"
    POSTPROCESSING = "postprocessing"
    SHARING = "sharing"
    # -- terminal: the campaign is over, one way or another ---------------
    FINISHED = "finished"
    FAILED = "failed"
    STOPPED = "stopped"
    CRASHED = "crashed"
    UNKNOWN = "unknown"


#: Phases meaning the campaign is over (no more work will happen). Single source
#: of truth for the terminal test that was previously re-inlined — with divergent
#: membership — across the CLI, the service, and the MCP plugins.
TERMINAL_PHASES: frozenset[str] = frozenset({
    Phase.FINISHED, Phase.FAILED, Phase.STOPPED, Phase.CRASHED, Phase.UNKNOWN,
})

#: Phases meaning the campaign is still live/working (the complement).
RUNNING_PHASES: frozenset[str] = frozenset(set(Phase) - TERMINAL_PHASES)


def is_terminal(phase: str) -> bool:
    """True when ``phase`` means the campaign is over (see :data:`TERMINAL_PHASES`)."""
    return phase in TERMINAL_PHASES


def is_running(phase: str) -> bool:
    """True when the campaign is still live/working (the complement of terminal)."""
    return not is_terminal(phase)


# -- wire models ------------------------------------------------------------

class RunProgress(BaseModel):
    """Per-run progress for the current batch.

    ``completed`` counts runs that produced results (their result artifact reached
    storage); ``total`` is the number expected. ``failed`` is only meaningful once
    the batch's jobs have all reached a terminal state — then it is the count of
    expected runs that produced no result (``total - completed``); it stays 0 while
    the batch is still running (an unfinished run is not yet a failed one)."""
    completed: int = 0
    total: int = 0
    failed: int = 0


class BudgetItem(BaseModel):
    """One budget/stopping criterion's current value vs its limit."""
    label: str
    current: Optional[float] = None      # None when not-yet-defined (e.g. NaN)
    limit: float
    done: bool = False


class Status(BaseModel):
    """The controller's live state, served by ``GET /campaigns/{id}/status``.

    ``phase`` is an **open** string the controller advances through a documented
    vocabulary (``building`` → ``starting`` → ``variation`` → ``running`` →
    ``finishing`` → ``postprocessing`` → ``sharing`` → ``finished`` / ``failed``);
    ``stage`` and ``extra`` exist so future markers (e.g.
    ``"upload-to-share-done"``) slot in without a schema change. ``share_provider``
    names the share type of the current upload attempt; it can change across
    retriggers (a failed upload may be retried to a different provider).
    """
    # validate_assignment so the controller can assign plain dicts to the typed
    # sub-fields (``runs``, ``budget``) and they coerce to the models.
    model_config = ConfigDict(validate_assignment=True)

    phase: str = Phase.STARTING          # open vocabulary; see the Phase enum
    stage: Optional[str] = None
    mode: Optional[str] = None
    campaign_id: Optional[str] = None
    batch: int = 0                       # current batch index (0-based)
    batches_done: int = 0
    budget: list[BudgetItem] = Field(default_factory=list)
    runs: RunProgress = Field(default_factory=RunProgress)
    best_objective: Optional[float] = None
    batch_history: list[dict] = Field(default_factory=list)
    stop: Optional[dict] = None          # {kind, reason} once the loop ends
    # Human-readable failure reason (message + short traceback tail) when
    # ``phase == "failed"``. Surfaced in the CLI/UI/MCP so a controller crash no
    # longer has to be dug out of the pod log; ``None`` on a healthy campaign.
    error: Optional[str] = None
    # Share type of the current upload attempt; may change across retriggers
    # (the upload can be retried to a different provider).
    share_provider: Optional[str] = None
    # True once the campaign's configured analysis-postprocessing pipelines have run
    # to completion. A dedicated fact, not derivable from ``phase``: the run reaches
    # "finished" *before* postprocessing is chained (see controller._chain_postprocessing),
    # and a campaign whose ``.vast`` defines no postprocessing also ends "finished"
    # with only the minimal (pre-postprocess) data. Drives whether a download is the
    # postprocessed archive or the minimal campaign data.
    postprocessed: bool = False
    # Reason the campaign's most recent postprocessing attempt failed, or ``None`` when
    # it succeeded or never ran. A separate fact from ``phase``: a campaign whose runs
    # all finished but whose postprocessing failed stays ``phase == "finished"`` (the
    # runs are the deliverable) with ``postprocessed == False`` and this set — distinct
    # from a run failure (``phase == "failed"``). Cleared on a successful (re-)run.
    postprocessing_error: Optional[str] = None
    # Reason the campaign's most recent upload-to-share attempt failed, or ``None`` when
    # it succeeded or was never requested. Like ``postprocessing_error`` this is a
    # separate fact from ``phase``: a share failure keeps ``phase == "finished"`` (the
    # runs are the deliverable) and is re-triggerable from disk (service ``run_share``).
    # Cleared on a successful (re-)triggered upload.
    share_error: Optional[str] = None
    extra: dict = Field(default_factory=dict)
    updated_at: float = Field(default_factory=time.time)


def failure_detail(exc: BaseException, tail_lines: int = 20) -> str:
    """A concise, human-readable failure string for ``Status.error``.

    The exception message first (it carries the actionable part — e.g. the
    "Available configs:" list), then the tail of the traceback for genuine bugs.
    Shared by the local worker and the in-process cluster worker so both record
    failures the same way.
    """
    import traceback
    message = str(exc) or exc.__class__.__name__
    # A clean user error (e.g. CampaignConfigError) opts out of the traceback tail
    # via ``include_traceback = False``; its message is self-contained, so a stack
    # trace in the durable record would only be noise.
    if not getattr(exc, "include_traceback", True):
        return message
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    tb_tail = "".join(tb.splitlines(keepends=True)[-tail_lines:])
    return f"{message}\n\n{tb_tail}".strip()
