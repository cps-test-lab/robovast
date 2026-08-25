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
    # Ordered by when they occur: acceptance → lane pre-flight → image build (if any)
    # → plugin install (if any) → config-variation expansion (batch) → the run loop
    # → finish → postprocess → share. (``importing`` is the exception: it is where a
    # campaign that was taken in rather than run *starts*.) ``initializing``, ``building``, ``plugin
    # install`` and ``variation`` precede ``running`` and exist so the pre-run steps
    # are observable rather than a blank "starting".
    #
    # ``initializing`` is the phase a campaign has from the instant the service
    # accepts it: registered, listed, and addressable by id, with none of the slow
    # lane pre-flight done yet (project push, registry/base-image resolution, the
    # object-store tunnel). It exists because that work used to happen *before* the
    # campaign was registered, so a caller whose start call timed out could poll
    # every read path and be told, truthfully and misleadingly, that no such
    # campaign existed -- which is exactly the state that invites a retry and a
    # duplicate campaign. Nothing may be slow ahead of this phase.
    INITIALIZING = "initializing"
    BUILDING = "building"
    STARTING = "starting"
    PLUGIN_INSTALL = "plugin install"
    VARIATION = "variation"
    RUNNING = "running"
    FINISHING = "finishing"
    # ``importing`` is the one live phase a campaign can have without ever having run
    # here: a campaign taken in from an archive or from the share enters at this phase
    # and, when what arrived was raw, rolls straight on into ``postprocessing``. It is
    # in the enum rather than off to one side so that everything that already knows
    # "live" -- ``vast wait``, the busy guard, the campaign view -- treats an import
    # like any other work in progress without being told about imports.
    IMPORTING = "importing"
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

    Two different things can go wrong with a run, and conflating them let a sweep
    report itself as clean while a trial had failed:

    * ``no_result`` — the run delivered **nothing**: no result artifact reached storage
      (``total - completed``). Only meaningful once the batch's jobs have all reached a
      terminal state; it stays 0 while the batch runs, since an unfinished run is not
      yet a lost one.
    * ``failed`` — the run delivered a result whose **own verdict is a failure** (the
      scenario reported a failure or error). Its job may have completed normally: a
      failing trial is still a successful execution. Set when the batch's outcomes are
      recorded, so 0 before then.

    * ``killed`` — an operator stopped the run's job by hand (see
      :meth:`~robovast.service.interface.RobovastInterface.stop_job`). Its own counter and
      *not* part of ``failed``: a run somebody ended says nothing about the system under
      test, so counting it as a trial failure would put a human intervention into the
      campaign's measured outcome. It is a subset of ``no_result`` — a killed run delivered
      nothing, which is exactly why it was recorded — so it explains part of that number
      rather than adding to it.

    * ``invalid`` — the runner threw the trial away, because a container it ran against
      crashed and was restarted under it (see
      :func:`~robovast.execution.cluster_execution.cluster_execution.pod_invalidating_restart`).
      Counted apart from ``failed`` for the same reason ``killed`` is, and for a sharper
      one: the trial may well have written a *passing* verdict, against a simulator that
      had lost its state. Unlike ``killed`` it is not a subset of ``no_result`` — an
      invalidated run can have delivered a result, which is precisely the danger.

    ``completed`` counts runs that produced results — including failing ones — and
    ``total`` is the number expected. So ``total=25, completed=25, no_result=0,
    failed=1`` means every run delivered data and one trial did not pass: 24 usable.
    A reader showing ``completed`` as a success count is therefore wrong; successes are
    ``completed - failed``.

    The per-batch scope holds for a **live** status. One recovered from disk
    (:func:`~robovast.execution.status_recovery.reconstruct_status_from_disk`) reports
    the whole campaign instead — there is no current batch for it to be relative to."""
    completed: int = 0
    total: int = 0
    no_result: int = 0
    failed: int = 0
    killed: int = 0
    invalid: int = 0


class HealthFinding(BaseModel):
    """One thing a run's **simulator** reported wrong about itself, while it was running.

    The cross-repo contract, and the whole of it. RoboVAST learns exactly one word --
    ``level`` -- and passes ``check`` and ``detail`` through untouched, so there is no
    per-check knowledge anywhere in RoboVAST and any simulator shipping a command with this
    contract is understood. See
    :meth:`~robovast.common.simulators.SimulatorBackend.health_command` for who is asked and
    :ref:`mcp-liveness` for the document it prints.

    ``level`` decides what happens, and only these two mean anything here:

    * ``error`` — the run is not doing what it was started to do. Ends a ``vast wait``
      (exit 5), because nobody would otherwise be told: a run whose simulator is wedged
      still holds ``running`` for its whole life.
    * ``warn`` — worth reporting, never worth ending a wait for. Surfaces on
      ``get_job_state`` and on the campaign's own exit.

    ``check`` is a stable slug the simulator owns, and it is what makes "at most once" mean
    something: the waiter fires on a *new* slug, so a check that keeps firing is one fault
    rather than a stream of exits.
    """

    #: Which job reported it. Present because a campaign's findings arrive from several
    #: running jobs at once on the cluster lane, and "something is wedged" is not actionable
    #: without saying which.
    job_name: str
    level: str          # error | warn -- the only word RoboVAST interprets
    check: str          # the simulator's own slug, passed through
    detail: str         # the simulator's own observation, passed through


class BudgetItem(BaseModel):
    """One budget/stopping criterion's current value vs its limit."""
    label: str
    current: Optional[float] = None      # None when not-yet-defined (e.g. NaN)
    limit: float
    done: bool = False
    # Which *kind* of criterion this is, from the stopping vocabulary (``batches``,
    # ``time``, ``target_objective``, ``no_improvement``, ``metric``). ``label`` cannot
    # answer that: it is the criterion type only for ``batches``/``time`` and the user's
    # own objective or metric name otherwise, so a reader keying on it would treat a
    # metric somebody named ``batches`` as the batch counter. ``None`` on a status
    # written before this field existed.
    kind: Optional[str] = None


class Status(BaseModel):
    """The controller's live state, served by ``GET /campaigns/{id}/status``.

    ``phase`` is an **open** string the controller advances through a documented
    vocabulary (``initializing`` → ``building`` → ``starting`` → ``variation`` →
    ``running`` → ``finishing`` → ``importing`` → ``postprocessing`` → ``sharing`` →
    ``finished`` / ``failed``); ``stage`` and ``extra`` exist so future markers (e.g.
    ``"upload-to-share-done"``) slot in without a schema change.

    What is on the share is deliberately *not* here. It was, as ``share_provider``,
    and nothing ever wrote it -- which is the shape of the mistake: the share is
    another system's state, so a copy of it on a campaign goes stale the first time
    somebody deletes an archive out of band. ``list_share_archives`` asks the share.

    **Before adding a field: this is a hot fan-out payload.** The web UI renders every
    campaign in the list as a card and each card polls ``GET /campaigns/{id}/status``
    every 1.5 seconds, so anything here is paid for once per campaign on screen, per
    poll, per open tab -- and some of it is computed per read (``health``). What belongs
    here is therefore BOUNDED state and cheap cursors: a phase, a counter, a best-so-far,
    a batch index. A SERIES does not, however small it looks: ``batch_history`` was one
    (a row per batch, growing all run long), nothing ever read it, and it was removed in
    favour of ``GET /campaigns/{id}/search/history`` -- a route fetched only while
    something is displaying it, keyed on the ``batches_done`` cursor that lives here.
    The rule, and where a live run view lands, is in ``docs/http_api.rst``.
    """
    # validate_assignment so the controller can assign plain dicts to the typed
    # sub-fields (``runs``, ``budget``) and they coerce to the models.
    model_config = ConfigDict(validate_assignment=True)

    # Default ``initializing``, not ``starting``: a Status that exists but has not
    # been advanced yet must not claim more than it knows. The first phase is set by
    # construction rather than by a caller remembering to set it.
    phase: str = Phase.INITIALIZING      # open vocabulary; see the Phase enum
    # When ``phase`` was last set. A phase alone cannot distinguish "slow" from
    # "wedged": without this, a campaign stuck in ``initializing`` or ``building``
    # looks identical to one making progress, forever. Readers render it as an age
    # ("initializing for 14 min"), which is what makes a hung pre-run step visible.
    phase_since: float = Field(default_factory=time.time)
    # When a progress signal last *advanced*. ``phase_since`` cannot answer this: a
    # campaign spends its whole run in one ``running`` phase, so its phase age grows
    # whether or not anything is happening. A hung run reported ``running`` with
    # ``progress: 0`` indefinitely and looked exactly like a slow one; the age of the
    # last actual advance is what separates them. See
    # ``ControllerState._stamp_progress`` for what counts as an advance.
    progress_since: float = Field(default_factory=time.time)
    # How long ``progress_since`` may legitimately stand still: the declared job budget
    # (``execution.timeout`` — see ``common.config.declared_job_seconds``), used as
    # declared, because packed runs can publish their results in one burst per job.
    # Carried on the status so a reader calls a run stalled against a *declared* limit
    # instead of a threshold it invented, and left on the conservative side: a missed
    # stall is recoverable, a false accusation against a healthy long run is not.
    # ``None`` when the controller never recorded one — then no reader may claim a stall.
    progress_deadline_s: Optional[int] = None
    # Wall-clock start of the **current batch's** runs. ``RunProgress`` is per-batch and
    # every counter in it resets when a batch begins, while the campaign's ``started_at``
    # does not -- so without this a reader can only date run progress from the campaign,
    # which is right for batch 0 and wrong for every batch after it. That is exactly how
    # a search past its first round lost the only clock its run counters could be read
    # against. ``None`` when no batch has begun, and on a status reconstructed from disk
    # (``status_recovery``), which has no current batch to be relative to -- the same
    # caveat ``RunProgress`` carries.
    batch_since: Optional[float] = None
    stage: Optional[str] = None
    mode: Optional[str] = None
    campaign_id: Optional[str] = None
    batch: int = 0                       # current batch index (0-based)
    batches_done: int = 0
    budget: list[BudgetItem] = Field(default_factory=list)
    runs: RunProgress = Field(default_factory=RunProgress)
    best_objective: Optional[float] = None
    stop: Optional[dict] = None          # {kind, reason} once the loop ends
    # Human-readable failure reason (message + short traceback tail) when
    # ``phase == "failed"``. Surfaced in the CLI/UI/MCP so a controller crash no
    # longer has to be dug out of the pod log; ``None`` on a healthy campaign.
    error: Optional[str] = None
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
    # What the running jobs' own simulators currently report about themselves. **Attached on
    # read and never persisted**: the controller does not write this, nothing in the results
    # records it, and a campaign nobody polls produces none of it -- diagnostics are not
    # results. Empty is therefore "nothing was reported", which is not the same as "nothing
    # is wrong"; :meth:`get_job_state` is where a reader finds out which of the two it is.
    health: list[HealthFinding] = Field(default_factory=list)
    # Checks a running job's simulator says it did NOT run, each already carrying its own reason
    # (``"<job>: <the simulator's sentence>"``). Beside ``health`` rather than inside it, because a
    # check that did not run is not a finding: it has no ``level``, and inventing one would be
    # RoboVAST asserting something the simulator did not.
    #
    # Reported at all because "no findings" and "the check never ran" are the same shape otherwise,
    # and the first reads as a clean bill of health. A robot-motion check with no roster is exactly
    # that case: nothing is wrong, and nothing looked.
    health_skipped: list[str] = Field(default_factory=list)
    updated_at: float = Field(default_factory=time.time)


#: What a caller should do once a run is known to be stalled. Part of the verdict
#: because the whole defect this fixes was that the next step had to be *remembered*:
#: the interim procedure lived in a skill, and a check that must be remembered is a
#: check that will be skipped.
STALL_NEXT_STEP = ("ask what the job is doing with get_job_state, then what it is repeating "
                   "with summarize=True on its log (get_job_log / get_campaign_log, or "
                   "`vast exec log`), then reproduce the configuration with exec_in_container "
                   "-- a fault that does not reproduce there is environmental rather than in "
                   "the config")


#: What a caller should do once a run's own simulator has reported something wrong. A DIFFERENT
#: step from :data:`STALL_NEXT_STEP`, and this used to reuse it, which sent a reader to go and ask
#: what the job was doing -- the question the finding had already answered. A finding names the job
#: and names the check, so the useful next move starts from those two facts.
HEALTH_NEXT_STEP = ("read that job with get_job_state -- the finding says a check failed, the tree "
                    "says which action was running while it did and the simulator's own state "
                    "carries the clock and the poses; look the check's slug up in the simulator's "
                    "documentation, not RoboVAST's, since the slug is the simulator's; then "
                    "reproduce the configuration with exec_in_container if the cause is not yet "
                    "in hand")


#: The only two finding levels RoboVAST gives meaning to. Named here so the waiter, the MCP
#: and the turn guard match on one spelling -- and deliberately just two: everything else a
#: simulator might say is passed through, and a level RoboVAST does not know is not an error.
HEALTH_ERROR = "error"
HEALTH_WARN = "warn"


#: Told to a caller whose campaign declared no budget, so "I cannot judge" is never
#: mistaken for "it is fine" — and so the fix is stated rather than left to be guessed.
NO_STALL_VERDICT = ("cannot judge: the .vast declares no execution.timeout, so there "
                    "is no budget to compare against. Read progress_age_s yourself "
                    "(is it far past how long one run should take?), or declare "
                    "execution.timeout to get a verdict here")


#: Told to a caller whose campaign is live but is not executing runs. The budget on the status
#: is the *per-run* one and the signal it is measured against is run counters (see
#: ``ControllerState._progress_signal``) — neither of which a phase that runs no runs can move.
#: So the clock could only ever run out: converting a large campaign's rosbags legitimately
#: outlasts any single run, and judging that against the per-run budget reported every such
#: campaign as stalled while it was healthily postprocessing — and ended ``vast wait`` at exit 4
#: with the advice to go inspect a job that had already succeeded. ``{phase}`` is named because
#: the useful next read differs per phase, and the age is re-described because ``set_phase``
#: restarts ``progress_since`` on every phase change: outside ``running`` it *is* the phase's age.
NO_STALL_VERDICT_OFF_RUN = (
    "cannot judge: the campaign is in '{phase}', where no run executes, and the only budget "
    "declared is the per-run one — there is nothing here for it to measure. progress_age_s is "
    "the age of this phase; read what the phase is doing with get_campaign_log")


def stall_report(status: "Status") -> dict:
    """Has this campaign's progress stopped advancing, and may we say so?

    The single derivation of the stall verdict, shared by every renderer (MCP, CLI,
    and — via the same fields — the web UI). Two kinds of answer, kept apart:

    * ``progress_age_s`` is a **fact**: seconds since progress last advanced (see
      ``ControllerState._stamp_progress``). Present whenever the campaign is live.
    * ``stalled`` is a **verdict**, made only against the budget the ``.vast``
      actually declared. It is deliberately **tri-state**, because a two-valued one
      cannot tell "within budget" apart from "no budget to check against" — and
      collapsing those two returns ``false`` for a run that is already dead, which is
      a health certificate for a corpse:

      - ``True``  — past the declared budget; not merely slow. ``stall_reason`` says
        what to do next.
      - ``False`` — inside the declared budget.
      - ``None``  — no verdict is possible: either no ``execution.timeout`` was
        declared, or the campaign is live in a phase that executes no runs (below).
        ``stall_verdict`` says which, and how to get one. Never a substituted
        backstop: the cluster's force-kill default exists so a run cannot hang
        forever, which is a fine reason to kill at one hour and a terrible reason to
        call a two-minute pilot healthy for the first fifty-nine.

    A terminal campaign gets none of it: its progress stopped advancing because it is
    over, which is not a stall.

    **A live campaign outside** ``running`` **gets no verdict either**, for the same reason
    one with no declared budget does not: the budget is per-*run*, and so is the signal it is
    measured against. In ``postprocessing``, ``sharing``, ``finishing`` or any pre-run phase
    nothing can advance that signal, so the clock runs from the moment the phase began and
    passing the budget says only that the phase outlasted one run. That is arithmetic, not a
    stall — and asserting it sent readers to diagnose a run that had already finished.

    Returns:
        ``{}`` for a terminal campaign; otherwise ``{progress_age_s, stalled}`` plus
        ``progress_deadline_s`` and possibly ``stall_reason`` when the campaign is running
        against a declared budget, or ``stall_verdict`` when no verdict is possible.
    """
    if is_terminal(status.phase) or not status.progress_since:
        return {}
    age = round(max(0.0, time.time() - status.progress_since), 1)
    if status.phase != Phase.RUNNING:
        # Before the budget check, not after: this is the more specific reason no verdict is
        # possible, and a campaign postprocessing without a declared timeout should be told
        # about the phase it is in rather than sent to add a timeout that would not help.
        # ``progress_deadline_s`` is deliberately withheld — reporting a per-run budget beside
        # a phase it cannot judge is what invited the comparison in the first place.
        return {"progress_age_s": age, "stalled": None,
                "stall_verdict": NO_STALL_VERDICT_OFF_RUN.format(phase=status.phase)}
    deadline = status.progress_deadline_s
    if not deadline:
        return {"progress_age_s": age, "stalled": None,
                "stall_verdict": NO_STALL_VERDICT}
    report = {"progress_age_s": age, "progress_deadline_s": deadline,
              "stalled": age > deadline}
    if report["stalled"]:
        report["stall_reason"] = (
            f"no progress for {age:.0f}s, past the {deadline}s expected per run — "
            f"the run is not merely slow. Next: {STALL_NEXT_STEP}")
    return report


def error_findings(status: "Status") -> list["HealthFinding"]:
    """The ``error``-level findings on a live campaign, in report order.

    One derivation, shared by the waiter and the MCP for the reason :func:`stall_report`
    is shared: two surfaces deciding separately what counts as bad enough to act on is how
    they come to disagree about the same campaign.

    A terminal campaign gets none, as with a stall: what a run reported while it was wedged
    is history once it is over, and the results are the record then.
    """
    if is_terminal(status.phase):
        return []
    return [f for f in (status.health or []) if f.level == HEALTH_ERROR]


def finding_summary(finding: "HealthFinding") -> str:
    """One finding as a line, phrased identically wherever it is shown.

    Observation first and diagnosis never: the simulator's ``detail`` states what it saw,
    and the slug is carried so a reader can look the check up in the simulator's own docs
    rather than in RoboVAST's.
    """
    return (f"{finding.job_name}: health finding ({finding.level}) "
            f"{finding.check}: {finding.detail}")


def failure_detail(exc: BaseException, tail_lines: int = 20) -> str:
    """A concise, human-readable failure string for ``Status.error``.

    The exception message first (it carries the actionable part — e.g. the
    "Available configs:" list), then the tail of the traceback for genuine bugs.
    Shared by the local worker and the in-process cluster worker so both record
    failures the same way.

    The frames are formatted *without* the trailing ``Type: message`` line, because
    that line is the message already printed above it: appending the raw
    ``format_exception`` output made every recorded failure state its reason twice,
    and a long message (the version-1 migration text runs some twenty lines) then
    consumed the whole ``tail_lines`` budget, so the duplicate crowded out the very
    frames the tail exists to show.
    """
    import traceback
    message = str(exc) or exc.__class__.__name__
    # A clean user error (e.g. CampaignConfigError) opts out of the traceback tail
    # via ``include_traceback = False``; its message is self-contained, so a stack
    # trace in the durable record would only be noise.
    if not getattr(exc, "include_traceback", True):
        return message
    parts = traceback.format_exception(type(exc), exc, exc.__traceback__)
    # The final element(s) are exactly the "Type: message" block; a chained cause
    # keeps its own, which names a *different* failure and is worth reading.
    final = traceback.format_exception_only(type(exc), exc)
    if len(parts) > len(final) and parts[-len(final):] == final:
        parts = parts[:-len(final)]
    tb_tail = "".join("".join(parts).splitlines(keepends=True)[-tail_lines:])
    return f"{message}\n\n{tb_tail}".strip()
