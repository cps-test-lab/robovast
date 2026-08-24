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

"""Spend the budget on the boundary, not on the interior.

"Maximize failures" has a trivial answer -- crank the worst factor to its limit -- and a
search that finds it has told you something you could have guessed. The engineering
question is narrower: *where does it start failing?* No amount of budget spent deep inside
the failure region answers that, and a maximizing search spends all of it there.

This strategy traces a **level set**: the contour where the objective crosses a stated
value. For a safety margin the interesting level is zero (the failure boundary); for a
failure rate it is 0.5 (the coin-flip contour, where the outcome is genuinely uncertain).

**The level is a strategy parameter, not an objective direction.** ``direction`` means
"which way is better", and a target answers a different question -- there is no better,
only nearer. Overloading it would also have collided with the existing
``stopping: target_objective``, which is a stopping criterion rather than an optimisation
target. Two different ``target``s in one file format is a confusion nobody needs.

**No surrogate library.** The model here is inverse-distance-weighted k-nearest-neighbour
over what has already been evaluated, which needs nothing beyond numpy. That is a real
choice rather than a shortcut: a Gaussian process would model the landscape better, and it
would also make this strategy unavailable without an optional extra, on campaigns whose
budgets are tens of evaluations rather than thousands -- the regime where a GP's advantage
is smallest and its hyper-parameter fitting is least reliable. If a campaign ever wants
one, it is another strategy, not a rewrite of this one.

**A cold start covers rather than guesses.** With no history there is no level to seek, so
the first batch is drawn from the same low-discrepancy sequence the coverage baseline uses.
Nothing is wasted: those points are what the model is built from.
"""

import logging

import numpy as np
from pydantic import BaseModel, ConfigDict

from robovast.search.space import SearchSpaceCodec
from robovast.search.strategies.halton import _halton_point, _PRIMES, _scramble_tables
from robovast.search.strategy import SearchStrategy
from robovast.search.types import Evaluation, ParamSet, SearchReport

logger = logging.getLogger(__name__)


class BoundaryParams(BaseModel):
    """``strategy_parameters`` schema for the boundary strategy."""
    model_config = ConfigDict(extra='forbid')
    #: The objective value whose contour is being traced. Zero for a signed margin, 0.5
    #: for a rate. No default: which level matters is a property of the experiment, and a
    #: guess here would silently trace the wrong contour.
    level: float
    #: Neighbours the k-NN surrogate consults.
    neighbours: int = 5
    #: Candidates scored per proposal. Larger searches the level more finely at no
    #: simulation cost -- this is arithmetic, not runs.
    candidates: int = 512
    #: How strongly to prefer candidates far from what has already been evaluated.
    #: 0 traces the model's current guess exactly and re-proposes the same cell forever;
    #: the default keeps a batch spread along the contour rather than piled on one point.
    exploration: float = 0.35


class BoundarySearch(SearchStrategy):
    """Active sampling of one objective's level set."""

    PARAMS_MODEL = BoundaryParams

    def __init__(self, cfg, params: BoundaryParams):
        super().__init__(cfg, params)
        self._codec = SearchSpaceCodec(cfg.search_space)
        self._dims = len(cfg.search_space)
        self._bases = _PRIMES[:self._dims]
        self._tables = _scramble_tables(self._bases, cfg.seed, True)
        self._halton_index = 1
        self._rng = np.random.default_rng(0 if cfg.seed is None else int(cfg.seed))
        self._batches_done = 0
        self._history: list[Evaluation] = []
        self._points: list[np.ndarray] = []      # unit-cube coordinates of evaluations
        self._values: list[float] = []

    # -- proposing ----------------------------------------------------------

    def ask(self, n: int) -> list[ParamSet]:
        name = self.single_objective.name       # refuses >1 objective, loudly
        del name
        if len(self._points) < 2:
            return [self._from_vec(self._next_halton()) for _ in range(n)]
        return [self._from_vec(v) for v in self._pick(n)]

    def _next_halton(self) -> np.ndarray:
        vec = _halton_point(self._halton_index, self._bases, self._tables)
        self._halton_index += 1
        return vec

    def _from_vec(self, vec: np.ndarray) -> ParamSet:
        return ParamSet(values=self._codec.decode(vec))

    def _pick(self, n: int) -> list[np.ndarray]:
        """Choose ``n`` candidates nearest the level, kept apart from each other.

        Chosen one at a time with each pick added to the "already taken" set, because a
        batch scored in one pass would put every proposal on the single best point: they
        are scored against the same model and would all agree. A cell evaluated eight
        times locates the boundary no better than once.
        """
        pool = np.array([self._rng.random(self._dims) for _ in range(self.params.candidates)])
        known = np.array(self._points)
        taken: list[np.ndarray] = []
        picked = []
        for _ in range(n):
            scores = [self._score(c, known, taken) for c in pool]
            best = int(np.argmax(scores))
            picked.append(pool[best])
            taken.append(pool[best])
        return picked

    def _score(self, candidate: np.ndarray, known: np.ndarray,
               taken: list[np.ndarray]) -> float:
        """Higher is better: close to the level, away from what is already sampled."""
        predicted, distance = self._predict(candidate, known)
        # Nearness to the level, normalised by the spread actually observed so that the
        # two halves of the score stay comparable whatever the objective's units are.
        spread = (max(self._values) - min(self._values)) or 1.0
        nearness = 1.0 - min(abs(predicted - self.params.level) / spread, 1.0)
        apart = distance
        if taken:
            apart = min(apart, min(float(np.linalg.norm(candidate - t)) for t in taken))
        return nearness + self.params.exploration * apart

    def _predict(self, candidate: np.ndarray, known: np.ndarray) -> tuple[float, float]:
        """Inverse-distance-weighted k-NN estimate, and the distance to the nearest point.

        The distance is returned alongside because it is the cheap uncertainty proxy: far
        from every evaluation, the estimate is a guess, and that is exactly where another
        evaluation is worth most.
        """
        deltas = np.linalg.norm(known - candidate, axis=1)
        k = min(self.params.neighbours, len(known))
        nearest = np.argsort(deltas)[:k]
        d = deltas[nearest]
        nearest_d = float(d[0])
        if nearest_d == 0.0:
            return float(self._values[int(nearest[0])]), 0.0
        weights = 1.0 / d
        values = np.array([self._values[i] for i in nearest])
        return float((weights * values).sum() / weights.sum()), nearest_d

    # -- learning -----------------------------------------------------------

    def tell(self, evaluations: list[Evaluation]) -> None:
        spec = self.single_objective
        for ev in evaluations:
            value = ev.objectives.get(spec.name)
            if value is None:
                continue
            self._history.append(ev)
            self._points.append(self._codec.encode(ev.params.values))
            self._values.append(float(value))
        self._batches_done += 1

    def report(self) -> SearchReport:
        level = self.params.level
        closest = None
        if self._history:
            closest = min(self._history,
                          key=lambda ev: abs(float(ev.objectives[self.single_objective.name])
                                             - level))
        extra = {
            "batches": self._batches_done,
            "level": level,
            # How near the contour the search actually got. A boundary search that never
            # approached its level found no boundary, and that has to be visible rather
            # than inferred from a best-objective number that means nothing here.
            "closest_to_level": (
                abs(float(closest.objectives[self.single_objective.name]) - level)
                if closest is not None else None),
        }
        return SearchReport(evaluations=list(self._history), best=closest, extra=extra)
