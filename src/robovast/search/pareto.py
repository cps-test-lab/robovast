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

"""Non-dominated (Pareto) selection.

With one objective the deliverable is "the best". With two it is the set of points no other
point beats on *every* objective at once — fast-and-close against slow-and-safe, with the
trade-off between them being the actual result. Collapsing that to a single winner requires a
weighting nobody has, and inventing one hides the finding.

Kept separate from the strategies because it is the shape of the answer rather than a way of
searching: a multi-objective strategy fills :attr:`~robovast.search.types.SearchReport.front`
with it, and post-hoc analysis over a finished campaign computes it the same way.
"""

import math
from typing import Iterable, Sequence

from robovast.common.config import ObjectiveSpec
from robovast.search.types import Evaluation


def _values(ev: Evaluation, specs: Sequence[ObjectiveSpec]) -> list[float] | None:
    """This evaluation's objective values in ``specs`` order, or ``None`` if it lacks one.

    ``None`` rather than a substitute: a cell that did not report every objective cannot be
    compared on all of them, and filling the gap would fabricate the comparison that decides
    whether it survives.
    """
    out = []
    for spec in specs:
        value = ev.objectives.get(spec.name)
        if value is None:
            return None
        value = float(value)
        if not math.isfinite(value):
            raise ValueError(
                f"objective '{spec.name}' is not finite ({value}); a non-finite value wins or "
                f"loses every comparison depending on the operator, so the front would be "
                f"decided arbitrarily")
        out.append(value)
    return out


def _better(a: float, b: float, direction: str) -> bool:
    return a < b if direction == 'minimize' else a > b


def dominates(a: Evaluation, b: Evaluation, specs: Sequence[ObjectiveSpec]) -> bool:
    """Whether ``a`` beats ``b`` on at least one objective and is no worse on any.

    Direction is read per objective, so a campaign may maximize one and minimize another —
    getting that backwards silently inverts the whole front, which is why it is never inferred.
    """
    av, bv = _values(a, specs), _values(b, specs)
    if av is None or bv is None:
        return False
    strictly_better = False
    for x, y, spec in zip(av, bv, specs):
        if _better(y, x, spec.direction):     # b beats a here -> a cannot dominate
            return False
        if _better(x, y, spec.direction):
            strictly_better = True
    return strictly_better


def pareto_front(evaluations: Iterable[Evaluation],
                 specs: Sequence[ObjectiveSpec]) -> list[Evaluation]:
    """The non-dominated subset, in input order.

    Returned whole rather than ranked or trimmed: the front *is* the answer to "what trade-offs
    are available", and truncating it answers a narrower question while looking like the same one.
    Evaluations missing a declared objective are excluded (see :func:`_values`); duplicates are
    all kept, because two cells that measured the same are two real results and dropping one
    misreports how much of the space reaches that trade-off.

    O(n^2) on purpose: a front is computed once per report over a campaign's evaluations, where n
    is batches x per_batch, and a faster algorithm would trade clarity for time nobody is short of.
    """
    scored = [ev for ev in evaluations if _values(ev, specs) is not None]
    return [ev for ev in scored
            if not any(dominates(other, ev, specs) for other in scored)]
