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

"""How many repetitions each parameter set gets — decided per cell, not per campaign.

``execution.runs`` spends the same number of runs on every cell. On a search that is
mostly waste: a cell whose runs all agree was decided by its first one, and the runs
that follow buy nothing. Measured on a quadrotor search campaign — 3 of 32
configurations produced a mixed outcome across 5 repetitions, so 145 of 160 runs each
bought a single bit that one run had already established.

**A policy layer, not a strategy.** It runs between :meth:`SearchStrategy.ask` and
composition, so every strategy gets it without knowing it exists — the alternative,
one noise-aware strategy, would have to be re-implemented for random, QD, Optuna and
anything a user writes. :attr:`ParamSet.n_reps` remains the strategy's own channel:
a set that already carries one is left alone, so a strategy that *does* reason about
noise still wins.

**Why local disagreement and not a confidence interval.** A confidence rule has to
know what the objective means — a proportion near 0.5 is uncertain, a margin near
zero is uncertain, a duration is never "uncertain" in that sense — so it would have to
be told the objective's type and threshold, and would be wrong whenever it was told
wrong. Spread among nearby evaluations needs none of that: it reads how much the
*measurements* disagree, which is the thing extra samples actually resolve. It works
unchanged for a rate, a margin or a time.

The allocation is deliberately made **before** the batch runs rather than by re-running
undecided cells afterwards. Appending runs to a cell that already has some would need
run indices to continue past what the first wave wrote, which the execution backends do
not offer; and a re-run wave costs a second composition and a second scheduling round
per batch. Allocating up front buys most of the saving with none of that.
"""

import logging
from typing import Optional

import numpy as np

from robovast.search.space import SearchSpaceCodec
from robovast.search.types import Evaluation, ParamSet

logger = logging.getLogger(__name__)


class RepetitionPolicy:
    """Assigns ``n_reps`` to the parameter sets of one batch."""

    def __init__(self, cfg, search_space: dict, default_runs: int):
        self.cfg = cfg
        self.default_runs = default_runs
        self._codec = SearchSpaceCodec(search_space) if cfg.policy == 'adaptive' else None

    def assign(self, param_sets: list[ParamSet],
               history: list[Evaluation]) -> list[ParamSet]:
        """Return the batch with ``n_reps`` filled in.

        ``history`` is every evaluation the search has scored so far. Sets that already
        carry ``n_reps`` are returned untouched.
        """
        if self.cfg.policy == 'fixed':
            reps = [self.default_runs] * len(param_sets)
        else:
            reps = self._adaptive(param_sets, history)
        out = []
        for ps, n in zip(param_sets, reps):
            # dataclasses.replace would recompute nothing (id is already set), but being
            # explicit keeps the identity guarantee visible: the cell is the same cell.
            out.append(ps if ps.n_reps is not None
                       else ParamSet(values=ps.values, id=ps.id, n_reps=int(n)))
        return out

    # -- adaptive -----------------------------------------------------------

    def _adaptive(self, param_sets, history) -> list[int]:
        lo, hi = self.cfg.min, self.cfg.max
        if lo == hi:
            return [lo] * len(param_sets)

        scored = [ev for ev in history if ev.objectives]
        if len(scored) < 2:
            # Nothing is known about the landscape yet, so nothing justifies spending
            # above the floor. Guessing high here would reproduce the uniform waste this
            # policy exists to remove, just with a different constant.
            return [lo] * len(param_sets)

        points = np.array([self._encode(ev.params.values) for ev in scored])
        values = np.array([self._objective(ev) for ev in scored], dtype=float)
        # The scale disagreement is measured against. Zero means every observation so
        # far agrees, so no neighbourhood can be "contested" and everything gets the
        # floor -- the 29-of-32 case.
        global_spread = float(values.max() - values.min())
        if global_spread <= 0.0:
            return [lo] * len(param_sets)

        k = min(self.cfg.neighbours, len(scored))
        out = []
        for ps in param_sets:
            vec = self._encode(ps.values)
            nearest = np.argsort(np.linalg.norm(points - vec, axis=1))[:k]
            local = values[nearest]
            rel = float(local.max() - local.min()) / global_spread
            out.append(int(round(lo + rel * (hi - lo))))
        return out

    def _encode(self, values: dict) -> np.ndarray:
        """Position in the normalized unit cube, so a metre and a percent contribute
        comparably to 'nearby' instead of whichever has the larger raw range."""
        return self._codec.encode(values)

    @staticmethod
    def _objective(ev: Evaluation) -> float:
        # The sole objective when there is one; with several, the first declared. Sign
        # is irrelevant -- only the SPREAD is read, and negating every value leaves it
        # unchanged -- so no direction handling is needed here.
        return float(next(iter(ev.objectives.values())))


def build_repetition_policy(cfg, search_space: dict,
                            default_runs: int) -> Optional[RepetitionPolicy]:
    """Build the policy for a ``search.repetitions`` block, or ``None`` when absent.

    ``None`` is not a degenerate policy but the absence of one: the controller then
    leaves ``n_reps`` alone and every cell runs ``execution.runs`` times, exactly as
    before this existed.
    """
    if cfg is None:
        return None
    return RepetitionPolicy(cfg, search_space, default_runs)
