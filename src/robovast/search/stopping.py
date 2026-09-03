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

"""Generic, strategy-agnostic stop evaluation for a search.

A search ends when **any** configured criterion fires. Criteria come from two
parallel ``.vast`` lists, both evaluated here so a single component knows them all
(needed for both the stop decision and the live progress line):

* ``budget`` — resource caps: ``batches`` / ``time`` / ``evaluations`` / ``runs``.
* ``stopping`` — convergence / quality: ``target_objective`` / ``no_improvement``
  / ``metric``.

Evaluating centrally (not in the strategy) means the same criteria work for
random, QD and Optuna with no per-strategy code.
"""

import logging
import operator
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

_OPS = {'>=': operator.ge, '<=': operator.le, '>': operator.gt, '<': operator.lt}


@dataclass
class StopSnapshot:
    """Search progress after a completed batch."""
    batch: int                              # batches completed so far (1-based count)
    elapsed: float                          # wall-clock seconds since search start
    best_objective: Optional[float] = None  # best objective SO FAR, in RAW units
    metrics: dict = field(default_factory=dict)  # strategy report().extra (e.g. coverage)
    #: Parameter sets SCORED so far, and individual RUNS executed so far. Two counts
    #: and not one: a cell evaluated once can cost any number of repetitions, so
    #: neither predicts the other once ``search.repetitions`` is adaptive.
    evaluations: int = 0
    runs: int = 0


@dataclass
class StopResult:
    """Which criterion ended the search."""
    kind: str       # criterion type: batches/time/target_objective/...
    reason: str     # human-readable explanation (also persisted)


@dataclass
class CriterionProgress:
    """One criterion's current value vs its limit, for the run-time progress line.

    ``label`` is what a reader *prints*; ``kind`` is what the criterion *is*. They
    coincide for ``batches`` and ``time`` and diverge for the rest, where the label is
    the user's own objective or metric name -- so anything deciding behaviour from a
    criterion (rather than just showing it) must key on ``kind``, or a metric somebody
    named ``batches`` gets treated as the batch counter.
    """
    label: str
    current: float
    limit: float
    done: bool
    kind: str = ""            # the criterion's `type`; see the class docstring
    #: The comparison that makes this criterion FIRE, as ``current <op> limit``. Every kind has
    #: one: the resource caps and ``no_improvement`` fire at ``>=`` their limit,
    #: ``target_objective`` at ``<=`` or ``>=`` depending on the objective's direction, and
    #: ``metric`` at whatever the user wrote.
    #:
    #: Published because ``label current / limit`` alone is ambiguous the moment the comparison
    #: is not ``>=``: a ``metric`` with ``op: '<='`` at ``0.1 / 0.8`` is already SATISFIED and
    #: reads to a human as "12% of the way there". ``done`` says whether it fired; this says
    #: which way it was heading, which is what makes the row a sentence rather than a pair.
    #:
    #: It does NOT make a fraction computable. A ``<=`` criterion has no lower bound to measure
    #: from and an objective has no origin, so only kinds with a real floor (the caps, and
    #: ``no_improvement`` counting up from zero) can be drawn as a share of anything.
    op: str = ">="


def _fmt(v: float) -> str:
    return f"{v:.4g}" if isinstance(v, float) else str(v)


class StopConditions:
    """Evaluates the combined budget + stopping criteria, OR-combined.

    Stateful: records the best-objective-so-far per batch (for ``no_improvement``),
    so :meth:`should_stop` / :meth:`progress` must be called **once per batch, in
    order**.
    """

    def __init__(self, budget, stopping, objective_name: str, direction: str = 'maximize'):
        self.budget = list(budget or [])
        self.stopping = list(stopping or [])
        self.criteria = self.budget + self.stopping
        self.objective_name = objective_name
        self.direction = direction
        self._best_history: list[float] = []   # best-so-far after each batch
        self._warned_metrics: set = set()

    @property
    def needs_metrics(self) -> bool:
        """Whether any criterion reads strategy ``report().extra`` (lazy fetch)."""
        return any(c.type == 'metric' for c in self.criteria)

    @property
    def has_budget(self) -> bool:
        return bool(self.budget)

    def _improved_by(self, recent: float, past: float, min_delta: float) -> bool:
        """Whether ``recent`` strictly beats ``past`` by more than min_delta
        (direction-aware); strict so min_delta=0 treats an equal value as no gain."""
        if self.direction == 'minimize':
            return recent < past - min_delta
        return recent > past + min_delta

    def _meets_target(self, best: float, target: float) -> bool:
        return best <= target if self.direction == 'minimize' else best >= target

    def _stale_batches(self, min_delta: float) -> int:
        """How many batches back the best objective still has not improved on.

        The largest ``k`` for which the best after the newest batch does not beat the best
        ``k`` batches earlier by *min_delta* -- which is the distance ``no_improvement``
        fires at, so :meth:`_fired` and :meth:`_progress` read it from here rather than each
        deciding what "stale" means.

        They did decide separately, and the two answers are not the same one: counting
        *consecutive* batches that failed to improve asks whether each single round cleared
        min_delta, while the criterion asks whether the whole window did. A search improving
        by less than min_delta every round but by more than it across the window is stale by
        the first reading and improving by the second -- so the progress row reported the
        criterion fired, permanently, while the search ran on. That row is what
        ``stopping_soon`` is derived from, so a reader was told the search was one round from
        ending for as long as it kept improving.
        """
        history = self._best_history
        stale = 0
        for k in range(1, len(history)):
            if self._improved_by(history[-1], history[-1 - k], min_delta):
                break
            stale = k
        return stale

    def _record(self, snap: StopSnapshot) -> None:
        """Append this batch's best-so-far (carry forward when absent) — keeps the
        no_improvement window aligned with batch indices. Idempotent per batch:
        call once via should_stop()."""
        if snap.best_objective is not None:
            self._best_history.append(snap.best_objective)
        elif self._best_history:
            self._best_history.append(self._best_history[-1])

    def seed_history(self, best_per_batch: list) -> None:
        """Seed the per-batch best-so-far a resumed search already has behind it.

        The resume counterpart of :meth:`_record`, and the reason ``no_improvement`` can be
        trusted across a restart: a stopping set is built from configuration alone, before
        any store is open, so it cannot read its own history and would otherwise start every
        re-entered search believing it had just improved.

        A named method rather than a public list because ``_stale_batches`` walks this by
        index and reads it as one entry per batch that had a best; handing it a
        differently-shaped list is the one way to make the criterion measure something else
        while still returning a number.
        """
        self._best_history.extend(best_per_batch)

    def should_stop(self, snap: StopSnapshot) -> Optional[StopResult]:
        """Return the first criterion that fires, else ``None``. Call once/batch."""
        self._record(snap)
        for crit in self.criteria:
            reason = self._fired(crit, snap)
            if reason:
                return StopResult(kind=crit.type, reason=reason)
        return None

    def progress(self, snap: StopSnapshot) -> list[CriterionProgress]:
        """Current value vs limit for each criterion (for the progress line)."""
        out = []
        for crit in self.criteria:
            cp = self._progress(crit, snap)
            if cp is not None:
                out.append(cp)
        return out

    # -- per-criterion helpers ----------------------------------------------

    def _fired(self, crit, snap: StopSnapshot) -> Optional[str]:
        t = crit.type
        if t == 'batches':
            if snap.batch >= crit.value:
                return f"batches budget reached ({snap.batch} >= {crit.value})"
        elif t == 'time':
            if snap.elapsed >= crit.seconds:
                return f"time budget reached ({snap.elapsed:.0f}s >= {crit.seconds:.0f}s)"
        elif t == 'evaluations':
            if snap.evaluations >= crit.value:
                return (f"evaluations budget reached "
                        f"({snap.evaluations} >= {crit.value})")
        elif t == 'runs':
            if snap.runs >= crit.value:
                return f"runs budget reached ({snap.runs} >= {crit.value})"
        elif t == 'target_objective':
            if snap.best_objective is not None and self._meets_target(snap.best_objective, crit.value):
                return (f"target_objective reached ({self.objective_name}="
                        f"{_fmt(snap.best_objective)}, target {_fmt(crit.value)})")
        elif t == 'no_improvement':
            if self._stale_batches(crit.min_delta) >= crit.patience:
                return (f"no_improvement over {crit.patience} batch(es) "
                        f"(min_delta={_fmt(crit.min_delta)})")
        elif t == 'metric':
            val = snap.metrics.get(crit.name)
            if val is None:
                if crit.name not in self._warned_metrics:
                    self._warned_metrics.add(crit.name)
                    logger.warning("stopping: metric '%s' not reported by the strategy; "
                                   "criterion will never fire", crit.name)
            elif _OPS[crit.op](val, crit.value):
                return f"metric {crit.name} {crit.op} {_fmt(crit.value)} (={_fmt(val)})"
        return None

    def _progress(self, crit, snap: StopSnapshot) -> Optional[CriterionProgress]:
        t = crit.type
        if t == 'batches':
            return CriterionProgress('batches', snap.batch, crit.value,
                                    snap.batch >= crit.value, kind=t)
        if t == 'time':
            return CriterionProgress('time', round(snap.elapsed, 1), crit.seconds,
                                     snap.elapsed >= crit.seconds, kind=t)
        if t == 'evaluations':
            return CriterionProgress('evaluations', snap.evaluations, crit.value,
                                     snap.evaluations >= crit.value, kind=t)
        if t == 'runs':
            return CriterionProgress('runs', snap.runs, crit.value,
                                     snap.runs >= crit.value, kind=t)
        if t == 'target_objective':
            cur = snap.best_objective if snap.best_objective is not None else float('nan')
            done = snap.best_objective is not None and self._meets_target(snap.best_objective, crit.value)
            # `<=` when lower is better -- the same comparison `_meets_target` applies, so the
            # row cannot describe a different test from the one that stops the search.
            op = '<=' if self.direction == 'minimize' else '>='
            return CriterionProgress(self.objective_name, cur, crit.value, done, kind=t, op=op)
        if t == 'no_improvement':
            stale = self._stale_batches(crit.min_delta)
            return CriterionProgress('stale_batches', stale, crit.patience,
                                    stale >= crit.patience, kind=t)
        if t == 'metric':
            val = snap.metrics.get(crit.name)
            if val is None:
                return None
            return CriterionProgress(crit.name, val, crit.value,
                                    _OPS[crit.op](val, crit.value), kind=t, op=crit.op)
        return None


def build_stop_conditions(search_cfg) -> StopConditions:
    """Build :class:`StopConditions` from a validated ``SearchConfig``.

    Always returns an instance (config validation guarantees at least one budget
    or stopping criterion).
    """
    spec = search_cfg.objectives[0]
    return StopConditions(search_cfg.budget, search_cfg.stopping, spec.name, spec.direction)
