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

"""Reading a search's own record back out of its store.

A search campaign writes one ``unit`` row per parameter set it proposed, carrying the
fields of an :class:`~robovast.search.types.Evaluation` and the status that says whether it
became one. That record is complete enough to re-drive a strategy through the exact
sequence of ``ask``/``tell`` calls it saw the first time, which is what
:meth:`~robovast.search.strategy.SearchStrategy.resume` does with it -- so nothing about a
strategy's internal state has to be serialized anywhere.

Both numbers a replay needs are here, and they are not the same number:

* how many parameter sets were **asked for** in each batch -- every unit row, including
  the draws that never became evaluations; and
* which of them were **told back** -- the ``evaluated`` rows.

A draw the variation pipeline could not realize (``composition_failed``) or whose every run
was lost (``no_sample``) costs a proposal but produces no evaluation, so a replay that
counted only the evaluations would under-advance the strategy's sequence and every
parameter set after it would differ.
"""

import json
from dataclasses import dataclass, field

from .types import Evaluation, ParamSet


@dataclass
class RecordedBatch:
    """One batch as its store recorded it.

    Attributes:
        asked: how many parameter sets the strategy proposed for this batch.
        evaluations: those that were scored, in the order they were recorded.
        reps: repetitions ALLOCATED to each recorded cell, in the same order -- every unit
            row, not only the scored ones, because a draw that composed to nothing still
            occupied the plan its allocation reserved. ``None`` for a row written before
            the allocation was recorded, where it was the campaign's ``execution.runs``.
    """

    asked: int = 0
    evaluations: list = field(default_factory=list)
    reps: list = field(default_factory=list)


def _evaluation(row) -> Evaluation:
    """One ``unit`` row as the :class:`Evaluation` the strategy was told.

    ``paramset_id`` is taken from the row rather than re-derived: it is what the results
    are addressed by on disk, so a replay must carry the identity the campaign used even
    if the derivation were ever to change.
    """
    values = json.loads(row["params_json"] or "{}")
    return Evaluation(
        params=ParamSet(values=values, id=row["paramset_id"] or "",
                        n_reps=_reps(row)),
        objectives=json.loads(row["objectives_json"] or "{}"),
        measures=json.loads(row["measures_json"] or "{}"),
        n_samples=row["n_samples"] or 0,
        raw={"result_dir": row["result_dir"] or ""},
    )


def recorded_batches(store, campaign_row_id: int) -> list:
    """Every batch of *campaign_row_id*, in execution order.

    Reads through the store's own ``batches``/``units`` accessors rather than SQL of its
    own: those already answer "in execution order", which is the property the replay
    depends on and the only one that would be silently wrong if it were re-derived here.
    """
    out = []
    for batch in store.batches(campaign_row_id):
        rows = store.units(batch["id"])
        out.append(RecordedBatch(
            asked=_asked(batch, rows),
            evaluations=[_evaluation(r) for r in rows if r["status"] == "evaluated"],
            reps=[_reps(r) for r in rows]))
    return out


def _reps(row):
    """Repetitions allocated to this cell, or ``None`` when the store recorded none.

    ``None`` is neither zero nor a default: it means this row predates the column, where
    every cell got the campaign's ``execution.runs`` because no per-cell allocation could
    be recorded. The caller substitutes that; substituting it here would hide which rows
    carry a measurement and which carry an assumption.
    """
    try:
        return row["n_reps"]
    except (IndexError, KeyError):      # a store predating the column
        return None


def _asked(batch, rows) -> int:
    """How many parameter sets this batch PROPOSED.

    The batch row's own count, which is the only place it is known: two draws with the
    same values are one cell -- ``ParamSet.id`` is derived from them and results are
    addressed by it -- so the batch composes and records that cell once, and the unit rows
    are then fewer than the ask.

    Falls back to the row count for a store written before ``batch.asked`` existed, where
    the two were the same number: no draw was ever collapsed there, because a repeated one
    aborted the campaign instead.
    """
    try:
        asked = batch["asked"]
    except (IndexError, KeyError):      # a store predating the column
        return len(rows)
    return len(rows) if asked is None else int(asked)
