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

* ``budget`` — resource caps: ``batches`` / ``time``.
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


@dataclass
class StopResult:
    """Which criterion ended the search."""
    kind: str       # criterion type: batches/time/target_objective/...
    reason: str     # human-readable explanation (also persisted)


@dataclass
class CriterionProgress:
    """One criterion's current value vs its limit, for the run-time progress line."""
    label: str
    current: float
    limit: float
    done: bool


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

    def _record(self, snap: StopSnapshot) -> None:
        """Append this batch's best-so-far (carry forward when absent) — keeps the
        no_improvement window aligned with batch indices. Idempotent per batch:
        call once via should_stop()."""
        if snap.best_objective is not None:
            self._best_history.append(snap.best_objective)
        elif self._best_history:
            self._best_history.append(self._best_history[-1])

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
        elif t == 'target_objective':
            if snap.best_objective is not None and self._meets_target(snap.best_objective, crit.value):
                return (f"target_objective reached ({self.objective_name}="
                        f"{_fmt(snap.best_objective)}, target {_fmt(crit.value)})")
        elif t == 'no_improvement':
            h = self._best_history
            if len(h) > crit.patience and not self._improved_by(h[-1], h[-1 - crit.patience], crit.min_delta):
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
            return CriterionProgress('batches', snap.batch, crit.value, snap.batch >= crit.value)
        if t == 'time':
            return CriterionProgress('time', round(snap.elapsed, 1), crit.seconds,
                                     snap.elapsed >= crit.seconds)
        if t == 'target_objective':
            cur = snap.best_objective if snap.best_objective is not None else float('nan')
            done = snap.best_objective is not None and self._meets_target(snap.best_objective, crit.value)
            return CriterionProgress(self.objective_name, cur, crit.value, done)
        if t == 'no_improvement':
            h = self._best_history
            stale = 0
            # consecutive trailing batches with no improvement (vs min_delta)
            for k in range(1, len(h)):
                if self._improved_by(h[-k], h[-k - 1], crit.min_delta):
                    break
                stale += 1
            return CriterionProgress('stale_batches', stale, crit.patience, stale >= crit.patience)
        if t == 'metric':
            val = snap.metrics.get(crit.name)
            if val is None:
                return None
            return CriterionProgress(crit.name, val, crit.value, _OPS[crit.op](val, crit.value))
        return None


def build_stop_conditions(search_cfg) -> StopConditions:
    """Build :class:`StopConditions` from a validated ``SearchConfig``.

    Always returns an instance (config validation guarantees at least one budget
    or stopping criterion).
    """
    spec = search_cfg.objectives[0]
    return StopConditions(search_cfg.budget, search_cfg.stopping, spec.name, spec.direction)
