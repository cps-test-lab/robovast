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

"""Which tuning survives the *worst* environment? -- a nested search.

Every other campaign here asks one of two questions. The adversarial ones fix the stack and
search the world: *given this configuration, what breaks it?* The design one fixes the world
and searches the stack: *across tunings, what does safety cost in time?* Neither answers the
question an engineer actually ships on, which is **which tuning is least bad when the world
is against it**.

That is a minimax, and it is nested: an outer search over the tunings we choose, and for each
one an inner search over the environments we do not. A tuning's score is the worst its
adversary could find.

**It needs no framework change**, which is the point worth noting. ``ask``/``tell`` is flat,
but a strategy holds its own state, so the nesting lives here: this proposes parameter sets
that pair the current tuning with an adversarially-chosen environment, accumulates what it is
told, and advances the outer level once a tuning's inner budget is spent. The controller only
ever sees a flat sequence of parameter sets.

**Two things it cannot hide.** The controller folds a scalar best from the *flat* objective,
which for a nested search is inner robustness rather than the outer worst-case -- so the live
progress line shows a number that is not what is being optimised, and ``report().extra`` is
where the real answer lives. And the cost multiplies: outer x inner x repetitions. Size it
deliberately.
"""

import logging

import numpy as np
from pydantic import BaseModel, ConfigDict

from robovast.search.space import SearchSpaceCodec
from robovast.search.strategy import SearchStrategy
from robovast.search.types import Evaluation, ParamSet, SearchReport

logger = logging.getLogger(__name__)


class MinimaxParams(BaseModel):
    """``strategy_parameters`` for the nested minimax strategy."""
    model_config = ConfigDict(extra='forbid')
    #: The dimensions *we* choose -- the design variables the outer search ranges over.
    outer: list[str]
    #: The dimensions the world chooses -- what the adversary ranges over for each tuning.
    inner: list[str]
    #: Evaluations the adversary gets per candidate tuning. The whole cost is
    #: ``outer_candidates x inner_budget x execution.runs``; this is the multiplier.
    inner_budget: int = 8
    #: Candidate tunings considered in total.
    outer_candidates: int = 6


class Minimax(SearchStrategy):
    """Outer search over tunings; inner adversary over environments."""

    PARAMS_MODEL = MinimaxParams

    def __init__(self, cfg, params: MinimaxParams):
        super().__init__(cfg, params)
        missing = [d for d in (*params.outer, *params.inner) if d not in cfg.search_space]
        if missing:
            raise ValueError(
                f"minimax: outer/inner name dimensions that search_space does not declare: "
                f"{missing}")
        overlap = set(params.outer) & set(params.inner)
        if overlap:
            # A dimension cannot be both chosen and imposed. Silently letting one be both
            # would make the outer search optimise against itself.
            raise ValueError(
                f"minimax: {sorted(overlap)} appear in both outer and inner; a dimension is "
                f"either a design variable or the adversary's, not both")
        self._codec = SearchSpaceCodec(cfg.search_space)
        self._rng = np.random.default_rng(0 if cfg.seed is None else int(cfg.seed))
        self._tunings = [self._draw(params.outer) for _ in range(params.outer_candidates)]
        self._index = 0                     # which tuning is under attack
        self._spent = 0                     # its inner evaluations so far
        self._worst: dict[int, float] = {}  # tuning index -> worst robustness seen
        self._history: list[Evaluation] = []
        self._batches_done = 0
        self._pending: dict[str, int] = {}  # ParamSet.id -> tuning index

    def _draw(self, names) -> dict:
        """One uniform draw over the named dimensions, decoded to real units."""
        vec = self._rng.random(len(self.search_space))
        values = self._codec.decode(vec)
        return {name: values[name] for name in names}

    def ask(self, n: int) -> list[ParamSet]:
        spec = self.single_objective     # refuses >1 objective, loudly
        del spec
        out = []
        for _ in range(n):
            if self._index >= len(self._tunings):
                # Budget exhausted: keep attacking the last tuning rather than proposing
                # nothing. A short batch is legal, but an empty one wastes a round.
                self._index = len(self._tunings) - 1
            tuning = self._tunings[self._index]
            values = dict(tuning)
            values.update(self._draw(self.params.inner))
            ps = ParamSet(values=values)
            self._pending[ps.id] = self._index
            out.append(ps)
            self._spent += 1
            if self._spent >= self.params.inner_budget:
                self._index += 1
                self._spent = 0
        return out

    def tell(self, evaluations: list[Evaluation]) -> None:
        name = self.single_objective.name
        for ev in evaluations:
            index = self._pending.pop(ev.params.id, None)
            value = ev.objectives.get(name)
            if index is None or value is None:
                continue
            # The adversary's job is to find the WORST environment for this tuning, so a
            # tuning's score is the most extreme value its inner search reached -- not the
            # mean, which would let a tuning that fails catastrophically once look
            # acceptable because it usually copes.
            #
            # "Most extreme" is whichever end the campaign DECLARED, because the inner
            # search is the adversary: a campaign minimizing robustness is already hunting
            # the low end, and one maximizing a failure rate is hunting the high end.
            # `objective_value` orients that once -- higher is always the direction the
            # campaign is pushing -- so the adversary keeps the MAXIMUM of it and needs no
            # branch on direction. Comparing the raw value instead was correct only for
            # `minimize`, the direction this example happens to declare; under `maximize`
            # the adversary kept the mildest environment it had found, and did so silently.
            self._worst[index] = max(self._worst.get(index, float('-inf')),
                                     self.objective_value(ev))
            self._history.append(ev)
        self._batches_done += 1

    def _raw(self, oriented: float) -> float:
        """An oriented worst case back in the objective's own units, for reporting.

        The two orderings are done on oriented values so neither needs to know the
        direction; the report has to undo that, or a minimized objective would be published
        with its sign flipped and every number in it would disagree with the campaign's own
        data.
        """
        return -oriented if self.single_objective.direction == 'minimize' else oriented

    def report(self) -> SearchReport:
        # Only tunings whose adversary actually ran. A tuning with no evaluations has no
        # worst case, and reporting one as unbeaten would make an untested candidate the
        # winner -- the most dangerous possible failure of this strategy.
        scored = [(i, w) for i, w in self._worst.items() if w != float('-inf')]
        # Best-defended tuning first: the one whose adversary got LEAST far, which on the
        # oriented scale is the smallest worst case. Ascending, and with no branch on
        # direction for the same reason `tell` needs none. Sorting the raw value descending
        # was the matching half of the same bug -- right for `minimize`, and under
        # `maximize` it crowned the least robust tuning and reported it as the answer.
        ranked = sorted(scored, key=lambda item: item[1])
        extra = {
            "batches": self._batches_done,
            "tunings_scored": len(scored),
            "tunings_total": len(self._tunings),
            # THE ANSWER, and it is here rather than in `best` because the controller's
            # scalar best folds the flat inner objective, which is a different quantity.
            "outer_best": self._raw(ranked[0][1]) if ranked else None,
            "robust_tuning": self._tunings[ranked[0][0]] if ranked else None,
            "worst_case_by_tuning": [
                {"tuning": self._tunings[i], "worst_robustness": self._raw(w)}
                for i, w in ranked
            ],
        }
        best = None
        if ranked:
            winner = self._tunings[ranked[0][0]]
            best = next((ev for ev in self._history
                         if all(ev.params.values.get(k) == v for k, v in winner.items())), None)
        return SearchReport(evaluations=list(self._history), best=best, extra=extra)
