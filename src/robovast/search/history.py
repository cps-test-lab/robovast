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

What is told back is not only what the batch scored. A cell an earlier batch measured is
not run again: the loop tells the strategy what it scored then, and records the cell as a
``recalled`` row naming the unit that measured it. The replay reads the told evaluation
from that unit, so it tells the strategy the same answer the live run did, read from the
same row. A batch recorded before a recall had a row has its recalled cells read off the
proposals the replay re-asks (:meth:`RecordedBatch.with_recalls`).

Only a ``complete`` batch is replayed. A batch interrupted before it recorded all of its
units is the campaign's last, and :func:`unfinished_batch` reads what it did record, so the
resumed loop can re-ask it, run only the cells it lacks and tell the strategy the whole of it.
"""

import json
from dataclasses import dataclass, field, replace
from typing import Callable, Optional

from .types import Evaluation, ParamSet


@dataclass
class RecordedBatch:
    """One batch as its store recorded it.

    Attributes:
        asked: how many parameter sets the strategy proposed for this batch.
        evaluations: those this batch scored, in the order they were recorded.
        recalled: the evaluations of cells an earlier batch measured and this one
            re-proposed, in the order it recalled them. Each is the object the earlier
            batch's ``evaluations`` holds, not a copy.
        reps: repetitions ALLOCATED to each cell this batch ran or tried to, in record
            order -- not only the scored ones, because a draw that composed to nothing still
            occupied the plan its allocation reserved, and not the recalled ones, which were
            allocated nothing. ``None`` for a row written before the allocation was
            recorded, where it was the campaign's ``execution.runs``.
        recalls_recorded: whether ``recalled`` is the batch's record. ``False`` for a
            batch written before a recalled cell had a row; its replay fills ``recalled``
            with :meth:`with_recalls`.
    """

    asked: int = 0
    evaluations: list = field(default_factory=list)
    recalled: list = field(default_factory=list)
    reps: list = field(default_factory=list)
    recalls_recorded: bool = True

    @property
    def told(self) -> list:
        """What the strategy was told for this batch: the scored cells, then the recalled.

        The live loop tells exactly this, built by this property, so the order a replay
        tells in is the order the live run told in by construction.
        """
        return self.evaluations + self.recalled

    def with_recalls(self, proposed: list, measured: dict) -> "RecordedBatch":
        """This batch with ``recalled`` read off the cells it *proposed*.

        *measured* maps ``ParamSet.id`` to the evaluation an earlier batch scored for it.
        Each distinct proposal found there, in proposal order, is a cell the live loop
        recalled rather than ran -- the split it made -- so a replay that re-asks the
        seeded strategy recovers them exactly.
        """
        ids = dict.fromkeys(ps.id for ps in proposed)
        return replace(self, recalled=[measured[i] for i in ids if i in measured],
                       recalls_recorded=True)


@dataclass(frozen=True)
class SearchPosition:
    """Where a search stands, folded from the batches its store recorded.

    Every number the loop needs to carry on from is here, so a campaign being re-entered
    and one starting now are read the same way: an empty record folds to the zero
    position, and there is no resume branch for the caller to forget a field in.

    One value rather than a set of counters assigned separately, because a counter missed
    at the call site is silent: it reads as zero, which is a legal value. Two of these
    decide when a search STOPS -- without ``best_objective`` a resumed ``target_objective``
    cannot know it is already met, and without ``best_per_batch`` ``no_improvement`` cannot
    know how long the search has been flat -- so a field added here must be filled in the
    one place that builds it.

    Attributes:
        batches: batches already recorded, and so the index of the next one.
        evaluations: parameter sets scored across them.
        runs: executions those batches ALLOCATED -- see :meth:`position_from`.
        history: every recorded :class:`~robovast.search.types.Evaluation`, in order.
        best_objective: best-so-far in raw objective units, ``None`` when nothing has
            scored or when the search has several objectives and no scalar best exists.
        best_per_batch: best-so-far AFTER each batch, index-aligned with batch numbers --
            what ``no_improvement`` measures staleness over.
        age_s: how long the campaign has been alive, in wall-clock seconds. What a ``time``
            budget caps, so a resumed search does not get a fresh clock.
    """

    batches: int = 0
    evaluations: int = 0
    runs: int = 0
    history: list = field(default_factory=list)
    best_objective: Optional[float] = None
    best_per_batch: list = field(default_factory=list)
    age_s: float = 0.0


def position_from(batches: list, *, default_runs: int,
                  fold_best: Callable[[Optional[float], list], Optional[float]],
                  age_s: float = 0.0) -> SearchPosition:
    """Fold *batches* into the :class:`SearchPosition` they add up to.

    A pure fold over the record, with no store of its own: the caller has already read the
    batches to re-drive its strategy through them, and reading them twice is one more way
    for the replay and the counters to disagree about the same campaign.

    *fold_best* is handed in rather than reimplemented. Which of two objective values is
    better is direction-aware and belongs to the strategy's objective spec, so the campaign
    has exactly one answer to it and this asks that one.

    ``best_per_batch`` gains an entry only once something has scored, which is what
    :meth:`~robovast.search.stopping.StopConditions._record` does for the live loop -- and it
    has to be the same rule, because the two lists are the same list either side of a
    restart. Carrying a value forward is unnecessary rather than skipped: *fold_best* keeps
    a best it already has, so once set it stays set and no batch can drop back to nothing.
    """
    evaluations = runs = 0
    history: list = []
    best = None
    best_per_batch: list = []
    for batch in batches:
        evaluations += len(batch.evaluations)
        history.extend(batch.evaluations)
        # What the batch COST, by the same measure the live loop uses: executions
        # attempted -- every cell's ALLOCATION, not what produced a sample. A draw that
        # composed to nothing still occupied the plan its allocation reserved, so this
        # sums over every recorded cell rather than over the scored ones.
        #
        # Read from the record rather than re-derived. `search.repetitions` sizes each
        # cell separately, so re-deriving meant recounting an unevenly-spent campaign as
        # an evenly spent one -- under where the policy had spent above `execution.runs`,
        # over where it had spent below -- and a `runs` budget then stopped the resumed
        # search in the wrong place. `default_runs` stands in only for a row that recorded
        # no allocation, which is a store from before one could be recorded, where it is
        # what that cell actually got.
        runs += sum((n or default_runs) for n in batch.reps)
        best = fold_best(best, batch.evaluations)
        if best is not None:
            best_per_batch.append(best)
    return SearchPosition(batches=len(batches), evaluations=evaluations, runs=runs,
                          history=history, best_objective=best,
                          best_per_batch=best_per_batch, age_s=age_s)


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


@dataclass(frozen=True)
class UnfinishedBatch:
    """A search's last batch, interrupted before it recorded all of its units.

    Attributes:
        batch_id: its row, which the rest of its units are recorded into.
        idx: its index.
        asked: how many parameter sets it proposed; ``None`` for a row written before
            ``batch.asked`` existed, where the loop asked for ``search.per_batch``.
        units: ``paramset_id`` -> ``(status, evaluation)`` for every cell it recorded as run
            or tried; ``evaluation`` is ``None`` unless the status is ``evaluated``.
        recalled: the ``paramset_id`` of every cell it recorded as recalled.
    """

    batch_id: int
    idx: int
    asked: Optional[int]
    units: dict = field(default_factory=dict)
    recalled: frozenset = frozenset()


def unfinished_batch(store, campaign_row_id: int) -> Optional[UnfinishedBatch]:
    """The last batch of *campaign_row_id* when it is not ``complete``, else ``None``."""
    batches = store.batches(campaign_row_id)
    if not batches or batches[-1]["complete"]:
        return None
    batch = batches[-1]
    units, recalled = {}, set()
    for row in store.units(batch["id"]):
        if row["status"] == "recalled":
            recalled.add(row["paramset_id"])
        else:
            units[row["paramset_id"]] = (
                row["status"], _evaluation(row) if row["status"] == "evaluated" else None)
    return UnfinishedBatch(batch_id=batch["id"], idx=batch["idx"], asked=batch["asked"],
                           units=units, recalled=frozenset(recalled))


def recorded_batches(store, campaign_row_id: int) -> list:
    """Every complete batch of *campaign_row_id*, in execution order.

    An incomplete last batch is left out (:func:`unfinished_batch`). An incomplete batch
    followed by another is a store that contradicts itself, and raises.

    Reads through the store's own ``batches``/``units`` accessors rather than SQL of its
    own: those already answer "in execution order", which is the property the replay
    depends on and the only one that would be silently wrong if it were re-derived here.

    A ``recalled`` row resolves to the evaluation of the unit it names, which an earlier
    batch recorded and this walk has therefore already read. One that names no unit read so
    far is a store that contradicts itself, and raises rather than tell a replay less.
    """
    out = []
    by_unit: dict = {}
    batches = store.batches(campaign_row_id)
    for position, batch in enumerate(batches):
        if not batch["complete"]:
            if position == len(batches) - 1:
                break
            raise ValueError(
                f"batch {batch['idx']} is not complete, but batch "
                f"{batches[position + 1]['idx']} follows it")
        rows = store.units(batch["id"])
        evaluations, recalled = [], []
        for row in rows:
            if row["status"] == "evaluated":
                by_unit[row["id"]] = _evaluation(row)
                evaluations.append(by_unit[row["id"]])
            elif row["status"] == "recalled":
                source = by_unit.get(row["recalled_from"])
                if source is None:
                    raise ValueError(
                        f"unit {row['id']} of batch {batch['idx']} recalls unit "
                        f"{row['recalled_from']}, which no earlier batch evaluated")
                recalled.append(source)
        out.append(RecordedBatch(
            asked=_asked(batch, rows), evaluations=evaluations, recalled=recalled,
            reps=[_reps(r) for r in rows if r["status"] != "recalled"],
            recalls_recorded=bool(batch["recalls_recorded"])))
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
