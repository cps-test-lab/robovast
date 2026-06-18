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

"""Random search — the basic, dependency-free strategy.

Uniformly samples the declared ``search_space`` each batch. ``tell`` only
records results (random is memoryless), but every evaluation feeds the report so
failures are captured. A legitimate failure-finding baseline that exercises the
full ask/tell/report loop. Single-objective; no ``strategy_parameters``.
"""

import logging
import math
import random
from typing import Any

from robovast.common.config import (BoolDim, ChoiceDim, FloatDim, IntDim,
                                     SearchDim)

from ..strategy import SearchStrategy
from ..types import Evaluation, ParamSet, SearchReport

logger = logging.getLogger(__name__)


def _sample_dim(dim: SearchDim, rng: random.Random) -> Any:
    if isinstance(dim, BoolDim):
        return rng.choice([False, True])
    if isinstance(dim, ChoiceDim):
        return rng.choice(dim.values)
    if isinstance(dim, FloatDim):
        if dim.log:
            return math.exp(rng.uniform(math.log(dim.low), math.log(dim.high)))
        return rng.uniform(dim.low, dim.high)
    if isinstance(dim, IntDim):
        if dim.log:
            value = int(round(math.exp(rng.uniform(math.log(dim.low), math.log(dim.high)))))
        else:
            value = rng.randint(dim.low, dim.high)
        if dim.step:
            value = dim.low + round((value - dim.low) / dim.step) * dim.step
            value = min(max(value, dim.low), dim.high)
        return value
    raise TypeError(f"Unsupported search dimension type: {type(dim).__name__}")


class RandomSearch(SearchStrategy):
    """Memoryless uniform sampler over the declared search space."""

    PARAMS_MODEL = None

    def __init__(self, cfg, params):
        super().__init__(cfg, params)
        self._rng = random.Random(cfg.seed)
        self._batches_done = 0
        self._history: list[Evaluation] = []

    def ask(self, n: int) -> list[ParamSet]:
        proposals = [
            ParamSet(values={path: _sample_dim(dim, self._rng)
                             for path, dim in self.search_space.items()})
            for _ in range(n)
        ]
        logger.debug("RandomSearch proposed %d parameter set(s)", len(proposals))
        return proposals

    def tell(self, evaluations: list[Evaluation]) -> None:
        self._history.extend(evaluations)
        self._batches_done += 1

    def is_done(self) -> bool:
        return self._batches_done >= self.cfg.budget.batches

    def report(self) -> SearchReport:
        ranked = sorted(self._history, key=self.objective_value, reverse=True)
        return SearchReport(
            evaluations=list(self._history),
            best=ranked[0] if ranked else None,
            extra={"batches": self._batches_done},
        )
