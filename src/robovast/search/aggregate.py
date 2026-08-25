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

"""Turning a cell's repetitions into one score.

An extractor has N runs and must return one number. Which number is a real modelling
choice, not a formality, and the obvious answer is usually the wrong one:

* **The mean hides the run you care about.** Four comfortable landings and one that
  nearly tipped over average to "comfortable". For a safety margin the interesting
  statistic is the bad tail, not the centre.
* **On a quality-diversity archive the mean collapses the spread the archive exists to
  map.** Measured on a quadrotor QD campaign: behaviour measures averaged over five
  runs filled 3 of 512 cells, because averaging pulled every cell toward the middle of
  the behaviour space before the archive ever saw it.

So ``worst`` is the default here and ``mean`` must be asked for by name. This module is
deliberately tiny and has no notion of what is being measured -- an extractor knows that
and this does not -- but every SUT re-derives the same three lines otherwise, and the
one that matters (which end is the bad end) is easy to get backwards.

Direction is expressed as ``higher_is_safer`` rather than reusing the objective's
``maximize``/``minimize``. They are different questions: an adversarial search
*minimizes* a safety margin on purpose, and the margin is still a quantity where higher
means safer. Conflating them silently aggregates from the wrong tail.
"""

import math
from typing import Iterable, Sequence

_METHODS = ('worst', 'mean', 'quantile')


def aggregate(values: Iterable[float], how: str = 'worst', *,
              higher_is_safer: bool = True, quantile: float = 0.1) -> float:
    """Reduce one cell's per-run values to a single score.

    Args:
        values: One value per completed run.
        how: ``worst`` (default), ``quantile`` or ``mean``.
        higher_is_safer: Which end is the bad end. ``True`` for a clearance or a margin,
            ``False`` for a cost such as time or control effort.
        quantile: Tail fraction for ``how='quantile'``, in ``[0, 1]``. Read from the
            unsafe end, so ``0.1`` is the 10th percentile of a margin and the 90th of a
            cost -- the same pessimism either way.

    Raises:
        ValueError: on an empty sample, an unknown method, a non-finite value, or a
            quantile outside ``[0, 1]``. Never returns a fallback: a cell that produced
            nothing measurable has no score, and inventing one is the failure mode
            ``NoSampleError`` exists to prevent.
    """
    vals = [float(v) for v in values]
    if not vals:
        raise ValueError(
            "cannot aggregate: no values (a cell that produced nothing has no score; "
            "raise NoSampleError rather than substituting one)")
    if not all(math.isfinite(v) for v in vals):
        raise ValueError(
            "cannot aggregate: values must be finite (a NaN wins or loses every "
            "comparison depending on the operator, so the cell would score arbitrarily)")
    if how not in _METHODS:
        raise ValueError(f"unknown aggregation '{how}'; expected one of {_METHODS}")

    if how == 'mean':
        return sum(vals) / len(vals)
    if how == 'worst':
        return min(vals) if higher_is_safer else max(vals)

    if not 0.0 <= quantile <= 1.0:
        raise ValueError(f"quantile must be in [0, 1], got {quantile}")
    # Read from the unsafe end, so the caller asks for "the pessimistic tenth" and gets
    # it whichever direction the quantity runs in.
    q = quantile if higher_is_safer else 1.0 - quantile
    return _percentile(sorted(vals), q)


def _percentile(ordered: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile of an already-sorted sample.

    Spelled out rather than pulled from numpy: this module is imported by extractors,
    which run inside the campaign's own container, and a three-line calculation is not
    worth making that container's dependency set larger.
    """
    if len(ordered) == 1:
        return ordered[0]
    pos = q * (len(ordered) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)
