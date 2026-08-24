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

"""Quality-diversity search (pyribs MAP-Elites).

Fills an *archive* of behaviorally **distinct** high-objective parameter sets:
each cell of the measure space keeps its best-objective config. With
``failure_rate`` as the objective and behavior measures from the extractor, the
archive becomes a map of *different kinds* of failures.

Generic over any ``search_space`` (via the codec) and any number of measures (via
``strategy_parameters.archive``). pyribs is imported lazily; install the ``qd``
extra to use it.
"""

import logging
import math
from typing import Literal, Optional

import numpy as np
from pydantic import BaseModel, ConfigDict, model_validator

from ..space import SearchSpaceCodec
from ..strategy import SearchStrategy
from ..types import Evaluation, ParamSet, SearchReport

logger = logging.getLogger(__name__)


class MeasureSpec(BaseModel):
    """One behaviour axis of the archive: a numeric range, or a set of categories.

    A QD archive answers "how many *distinct kinds* of behaviour are there", and the most
    useful kind is frequently not a number -- "it collided" / "it timed out" / "it never
    reached the goal" is what an engineer wants back. Expressing that on a numeric axis
    would mean the extractor inventing an encoding and the reader decoding it from a
    comment, with nothing checking that the two agreed.

    So an axis either states ``low``/``high`` (numeric) or ``values`` (categorical), never
    both: the bounds and bin count of a categorical axis follow from its categories, and
    stating them separately would be two sources of truth for one fact.
    """
    model_config = ConfigDict(extra='forbid')
    low: Optional[float] = None
    high: Optional[float] = None
    bins: int = 20          # grid archive only
    #: Category names, in archive order. Their count fixes the axis and its bins.
    values: Optional[list[str]] = None

    @model_validator(mode='after')
    def _one_kind_of_axis(self):
        if self.values is not None:
            if self.low is not None or self.high is not None:
                raise ValueError(
                    "a measure states either 'values' (categorical) or 'low'/'high' "
                    "(numeric), not both -- the categories already fix the axis")
            if not self.values:
                raise ValueError("measure 'values' must not be empty")
            if len(set(self.values)) != len(self.values):
                raise ValueError(f"measure 'values' must be unique, got {self.values}")
            # Derived, so a k-category axis has exactly k bins and no two categories can
            # share one.
            object.__setattr__(self, 'low', 0.0)
            object.__setattr__(self, 'high', float(len(self.values)))
            object.__setattr__(self, 'bins', len(self.values))
        elif self.low is None or self.high is None:
            raise ValueError(
                "a numeric measure needs 'low' and 'high' (or declare 'values' for a "
                "categorical one)")
        return self


def measure_value(spec: MeasureSpec, raw, name: str) -> float:
    """One evaluation's value on ``spec``'s axis, as the archive's coordinate.

    A categorical value arrives as its name and leaves as the **centre** of its bin: an
    integer index sits exactly on a bin edge, where which side it falls is the binning
    library's business rather than ours.

    An unrecognised category is refused rather than clamped or dropped. Both alternatives
    put a behaviour the archive cannot represent into a cell that means something else,
    and a diversity map whose cells mean the wrong thing is worse than one that is missing
    a cell.
    """
    if spec.values is not None:
        try:
            index = spec.values.index(raw)
        except ValueError:
            raise ValueError(
                f"measure '{name}' got category {raw!r}, which the archive does not "
                f"declare (have: {spec.values})") from None
        return index + 0.5
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"measure '{name}' is numeric (low/high) but got {raw!r}; declare "
            f"'values' if it is categorical") from None


class ArchiveConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    type: Literal['grid', 'cvt'] = 'grid'
    cells: int = 512        # cvt only
    measures: dict[str, MeasureSpec]


class QDParams(BaseModel):
    """``strategy_parameters`` schema for the QD strategy (and QD family)."""
    model_config = ConfigDict(extra='forbid')
    archive: ArchiveConfig
    sigma: float = 0.1      # emitter step size (fraction of each dim's range)
    emitters: int = 1


class QDStrategy(SearchStrategy):
    PARAMS_MODEL = QDParams

    def __init__(self, cfg, params: QDParams):
        super().__init__(cfg, params)
        try:
            from ribs.archives import CVTArchive, GridArchive
            from ribs.emitters import EvolutionStrategyEmitter
            from ribs.schedulers import Scheduler
        except ImportError as e:  # pragma: no cover - exercised only without the extra
            raise ImportError(
                "pyribs is required for strategy 'qd'. Install the extra: "
                "pip install 'robovast[qd]'") from e

        self.codec = SearchSpaceCodec(cfg.search_space)
        self.measure_names = list(params.archive.measures.keys())
        #: Kept so an evaluation's raw measure can be turned into the archive's
        #: coordinate -- a categorical axis needs its own declaration to do that.
        self._measure_specs = dict(params.archive.measures)
        ranges = [(m.low, m.high) for m in params.archive.measures.values()]
        # Solution space is the normalized unit cube (see SearchSpaceCodec), so a
        # single scalar sigma is meaningful across all dimensions.
        lower, upper = self.codec.bounds()
        bounds = list(zip(lower.tolist(), upper.tolist()))

        if params.archive.type == 'cvt':
            self.archive = CVTArchive(solution_dim=self.codec.dim,
                                      cells=params.archive.cells, ranges=ranges)
        else:
            dims = [m.bins for m in params.archive.measures.values()]
            self.archive = GridArchive(solution_dim=self.codec.dim, dims=dims, ranges=ranges)

        x0 = 0.5 * np.ones(self.codec.dim)       # centre of the unit cube
        sigma0 = float(params.sigma)             # scalar step (fraction of unit range)
        n_emitters = max(1, params.emitters)
        batch = max(1, math.ceil(cfg.per_batch / n_emitters))
        seed = cfg.seed
        emitters = [
            EvolutionStrategyEmitter(self.archive, x0=x0, sigma0=sigma0, bounds=bounds,
                                     batch_size=batch,
                                     seed=None if seed is None else seed + i)
            for i in range(n_emitters)
        ]
        # Kept on `self`: abandoning a generation (see `_tell_incomplete`) rebuilds the
        # scheduler around these same emitters, which is what preserves their CMA-ES
        # state across the reset.
        self._emitters = emitters
        self.scheduler = Scheduler(self.archive, emitters)
        self._batches_done = 0
        self._ask: list[tuple[str, np.ndarray]] = []   # (ParamSet.id, solution) in ask order
        self._direction = self.single_objective.direction

    def ask(self, n: int) -> list[ParamSet]:
        solutions = self.scheduler.ask()
        self._ask = []
        proposals = []
        for sol in solutions:
            values = self.codec.decode(np.asarray(sol))
            ps = ParamSet(values=values)
            proposals.append(ps)
            self._ask.append((ps.id, np.asarray(sol)))
        logger.debug("QD proposed %d solution(s)", len(proposals))
        return proposals

    def tell(self, evaluations: list[Evaluation]) -> None:
        by_id = {ev.params.id: ev for ev in evaluations}
        missing = [ps_id for ps_id, _ in self._ask if ps_id not in by_id]
        if missing:
            self._tell_incomplete(by_id, missing)
            return
        obj_batch, meas_batch = [], []
        name = self.single_objective.name
        for ps_id, _sol in self._ask:
            ev = by_id[ps_id]
            value = float(ev.objectives[name])
            obj_batch.append(-value if self._direction == 'minimize' else value)
            meas_batch.append([measure_value(self._measure_specs[m], ev.measures[m], m)
                               for m in self.measure_names])
        self.scheduler.tell(np.array(obj_batch), np.array(meas_batch))
        self._batches_done += 1

    def _tell_incomplete(self, by_id: dict, missing: list) -> None:
        """Close a generation that came back short, without inventing the missing rows.

        A draw can be unrealizable — a path too short to hold the obstacles the same
        draw asks for — and then no config is composed, nothing runs, and there is no
        evaluation. The batch loop records that unit and evaluates the rest, so ``tell``
        is handed fewer results than ``ask`` proposed. Every other strategy simply
        ingests what it got; pyribs cannot, and that asymmetry used to be a crash:
        ``Scheduler.tell`` requires exactly one objective and one measure row per
        solution it emitted.

        There is no sentinel that means "not measured". The archive rejects a non-finite
        objective outright, so ``-inf`` raises instead of being ignored — and a
        worst-case *finite* objective would be worse than the crash it avoids, because
        the measures would have to be invented too, and an invented measure vector lands
        the fabrication in a real archive cell, where it becomes an elite the search then
        chases.

        So the generation is closed the only honest way. The evaluations that *did*
        happen go into the archive directly (``add`` is the same insertion
        ``Scheduler.tell`` performs on them), and the emitters go without their CMA-ES
        update for this one round — they resample from the distribution they already
        had. The cost is one generation of adaptation. The cost of the KeyError this
        replaces was a 50-batch search that died on batch 33 with eight hours of
        completed, unpostprocessed work behind it.
        """
        from ribs.schedulers import Scheduler  # noqa: PLC0415 - optional extra

        name = self.single_objective.name
        sols, obj_batch, meas_batch = [], [], []
        for ps_id, sol in self._ask:
            ev = by_id.get(ps_id)
            if ev is None:
                continue
            value = float(ev.objectives[name])
            sols.append(sol)
            obj_batch.append(-value if self._direction == 'minimize' else value)
            meas_batch.append([measure_value(self._measure_specs[m], ev.measures[m], m)
                               for m in self.measure_names])
        logger.warning(
            "QD batch came back short: %d of %d draw(s) produced no evaluation (%s). "
            "The %d measured one(s) still enter the archive; the emitters skip this "
            "generation's update and resample from their current distribution.",
            len(missing), len(self._ask), ", ".join(missing), len(sols))
        if sols:
            self.archive.add(solution=np.array(sols), objective=np.array(obj_batch),
                             measures=np.array(meas_batch))
        # The scheduler has an ask outstanding and offers no public way to abandon it, so
        # the next ask() would raise. A fresh Scheduler over the SAME archive and the SAME
        # emitters is that reset: it carries no per-generation state of its own, and every
        # bit of search state — the archive, each emitter's CMA-ES distribution — lives in
        # the objects handed back to it.
        self.scheduler = Scheduler(self.archive, self._emitters)
        self._batches_done += 1

    def report(self) -> SearchReport:
        data = self.archive.data(return_type="dict")
        solutions = data.get("solution", [])
        objectives = data.get("objective", [])
        measures = data.get("measures", [])
        elites = []
        for sol, obj, meas in zip(solutions, objectives, measures):
            values = self.codec.decode(np.asarray(sol))
            obj = float(obj)
            elites.append({
                "params": values,
                "objective": -obj if self._direction == 'minimize' else obj,
                "measures": dict(zip(self.measure_names, [float(x) for x in meas])),
            })
        stats = self.archive.stats
        extra = {
            "batches": self._batches_done,
            "num_elites": int(getattr(stats, "num_elites", len(elites))),
            "coverage": float(getattr(stats, "coverage", 0.0)),
            "qd_score": float(getattr(stats, "qd_score", 0.0)),
            "elites": elites,
            "measure_names": self.measure_names,
        }
        best = max(elites, key=lambda e: e["objective"], default=None)
        report = SearchReport(extra=extra)
        if best is not None:
            # Surface the best elite as a (params-only) Evaluation-like marker.
            report.extra["best_elite"] = best
        return report
