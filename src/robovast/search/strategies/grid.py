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

"""Grid -- every cell of a finite search space, once.

The cells are the Cartesian product of the dimensions, in the order ``search_space`` declares
them, each dimension's values in its own order: a choice as listed, a bool as ``false, true``,
an int from ``low`` to ``high`` on its ``step``. ``ask(n)`` hands out the next ``n`` cells, and
once every cell has been asked the strategy is :attr:`exhausted`, which ends the search. A
float has no finite set of values and is refused.
"""

import itertools
import logging

from pydantic import BaseModel, ConfigDict

from robovast.common.config import BoolDim, ChoiceDim, IntDim

from ..strategy import SearchStrategy
from ..types import Evaluation, ParamSet, SearchReport

logger = logging.getLogger(__name__)


class GridParams(BaseModel):
    """A grid takes no parameters: its cells are the search space's."""
    model_config = ConfigDict(extra='forbid')


def dimension_values(dim) -> list:
    """Every value *dim* takes in a grid, in the dimension's own order."""
    if isinstance(dim, BoolDim):
        return [False, True]
    if isinstance(dim, ChoiceDim):
        return list(dim.values)
    if isinstance(dim, IntDim):
        return list(range(dim.low, dim.high + 1, dim.step or 1))
    raise TypeError(f"grid cannot enumerate a {type(dim).__name__}")


class GridSearch(SearchStrategy):
    """Proposes every cell of an int, choice and bool search space, then ends."""

    PARAMS_MODEL = GridParams
    DIMENSION_TYPES = frozenset({"int", "choice", "bool"})
    ENDS_ITSELF = True

    def __init__(self, cfg, params):
        super().__init__(cfg, params)
        names = list(self.search_space)
        self._cells = [dict(zip(names, values)) for values in itertools.product(
            *(dimension_values(self.search_space[n]) for n in names))]
        self._next = 0
        self._history: list[Evaluation] = []

    @property
    def cells(self) -> int:
        """How many cells the grid has."""
        return len(self._cells)

    @property
    def exhausted(self) -> bool:
        return self._next >= len(self._cells)

    def ask(self, n: int) -> list[ParamSet]:
        chunk = self._cells[self._next:self._next + n]
        self._next += len(chunk)
        logger.debug("GridSearch proposed cells %d..%d of %d", self._next - len(chunk),
                     self._next, len(self._cells))
        return [ParamSet(values=dict(cell)) for cell in chunk]

    def tell(self, evaluations: list[Evaluation]) -> None:
        self._history.extend(evaluations)

    def report(self) -> SearchReport:
        best = None
        if len(self.objectives) == 1 and self._history:
            best = max(self._history, key=self.objective_value)
        return SearchReport(evaluations=list(self._history), best=best,
                            extra={"cells": len(self._cells), "asked": self._next})
