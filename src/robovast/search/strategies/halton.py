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

"""Low-discrepancy coverage: a scrambled Halton sequence.

``random`` is the baseline every other strategy is compared against, and it is a weak
one. Uniform draws clump and leave holes, so a failure region can sit between samples
and an estimate carries more variance than its sample size suggests. A low-discrepancy
sequence fills the space evenly *by construction*: same budget, same question, tighter
answer. When two campaigns exist only to be compared -- "does this strategy find more
than blind sampling?" -- the blind one should at least be good at being blind.

**Halton rather than Sobol**, for one reason that matters here: Sobol needs
direction-number tables and in practice a scipy dependency, and scipy is an optional
extra. A baseline that only runs when an extra is installed is not a baseline. Halton
needs nothing beyond numpy, which is already a hard dependency. Its known weakness is
high dimensions, where the larger prime bases correlate; a search space of the size these
campaigns declare (a handful of factors) is nowhere near that, and the alternative was
not "Sobol" but "random".

**Scrambled**, because the raw sequence is deterministic: two campaigns with different
seeds would otherwise sample the identical points and their comparison would measure
nothing. Digit-permutation scrambling (Owen-style in spirit, per-base rather than
per-level) keeps the low-discrepancy property while making the series a function of the
seed.
"""

import logging
from typing import Optional

import numpy as np
from pydantic import BaseModel, ConfigDict

from robovast.search.space import SearchSpaceCodec
from robovast.search.strategy import SearchStrategy
from robovast.search.types import Evaluation, ParamSet, SearchReport

logger = logging.getLogger(__name__)

#: Primes used as the per-dimension bases, in order. Enough for any search space this is
#: a sensible sampler for; beyond it the sequence's dimensional correlation is the reason
#: to reach for a different method rather than a longer table.
_PRIMES = (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47, 53, 59, 61, 67, 71)


class HaltonParams(BaseModel):
    """``strategy_parameters`` schema for the Halton strategy."""
    model_config = ConfigDict(extra='forbid')
    #: Permute each base's digits, seeded from ``search.seed``. Off gives the textbook
    #: sequence: identical for every campaign, and therefore useless for comparing two.
    scramble: bool = True


def _scramble_tables(bases, seed: Optional[int], scramble: bool):
    """One digit permutation per base, or the identity when scrambling is off."""
    rng = np.random.default_rng(0 if seed is None else int(seed))
    tables = []
    for base in bases:
        digits = np.arange(base)
        if scramble:
            # 0 stays 0 so the sequence still starts at the origin corner rather than
            # jumping; permuting it as well shifts every point by a constant and buys
            # nothing that the per-base permutation has not already bought.
            tail = digits[1:].copy()
            rng.shuffle(tail)
            digits = np.concatenate([digits[:1], tail])
        tables.append(digits)
    return tables


def _halton_point(index: int, bases, tables) -> np.ndarray:
    """The ``index``-th point of the (scrambled) Halton sequence, in ``[0, 1)^d``.

    Radical inversion: write ``index`` in the given base and mirror its digits about the
    decimal point. Randomly accessible, so a batch can be drawn from an offset without
    replaying the sequence that preceded it.
    """
    out = np.empty(len(bases))
    for dim, (base, table) in enumerate(zip(bases, tables)):
        value, denom, n = 0.0, 1.0, index
        while n > 0:
            n, digit = divmod(n, base)
            denom *= base
            value += table[digit] / denom
        out[dim] = value
    return out


class HaltonSearch(SearchStrategy):
    """Deterministic even coverage of the declared space."""

    PARAMS_MODEL = HaltonParams

    def __init__(self, cfg, params: HaltonParams):
        super().__init__(cfg, params)
        self._codec = SearchSpaceCodec(cfg.search_space)
        self._bases = _PRIMES[:len(cfg.search_space)]
        if len(cfg.search_space) > len(_PRIMES):
            raise ValueError(
                f"halton supports up to {len(_PRIMES)} dimensions, got "
                f"{len(cfg.search_space)}; beyond that the sequence's dimensional "
                f"correlation makes it a worse baseline than random")
        self._tables = _scramble_tables(self._bases, cfg.seed, params.scramble)
        # Continues across batches. Restarting per batch would re-draw the same points and
        # cover *less* than random -- the evenness is a property of the whole series.
        # Index 1 rather than 0: the zeroth point is the origin corner in every dimension,
        # which is a boundary of the space rather than a sample from it.
        self._index = 1
        self._batches_done = 0
        self._history: list[Evaluation] = []

    def ask(self, n: int) -> list[ParamSet]:
        proposals = []
        for _ in range(n):
            vec = _halton_point(self._index, self._bases, self._tables)
            self._index += 1
            proposals.append(ParamSet(values=self._codec.decode(vec)))
        logger.debug("HaltonSearch proposed %d parameter set(s)", len(proposals))
        return proposals

    def tell(self, evaluations: list[Evaluation]) -> None:
        # A short generation is ordinary: an unrealizable draw composes to nothing and
        # never comes back. The sequence index has already advanced, which is correct --
        # the point was proposed, and re-proposing it would not make it realizable.
        self._history.extend(evaluations)
        self._batches_done += 1

    def report(self) -> SearchReport:
        ranked = sorted(self._history, key=self.objective_value, reverse=True)
        return SearchReport(
            evaluations=list(self._history),
            best=ranked[0] if ranked else None,
            extra={"batches": self._batches_done, "points_drawn": self._index - 1},
        )
