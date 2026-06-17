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
from pydantic import BaseModel, ConfigDict

from ..space import SearchSpaceCodec
from ..strategy import SearchStrategy
from ..types import Evaluation, ParamSet, SearchReport

logger = logging.getLogger(__name__)


class MeasureSpec(BaseModel):
    model_config = ConfigDict(extra='forbid')
    low: float
    high: float
    bins: int = 20          # grid archive only


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

        x0 = (0.5 * np.ones(self.codec.dim))     # centre of the unit cube
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
        obj_batch, meas_batch = [], []
        name = self.single_objective.name
        for ps_id, _sol in self._ask:
            ev = by_id[ps_id]
            value = float(ev.objectives[name])
            obj_batch.append(-value if self._direction == 'minimize' else value)
            meas_batch.append([float(ev.measures[m]) for m in self.measure_names])
        self.scheduler.tell(np.array(obj_batch), np.array(meas_batch))
        self._batches_done += 1

    def is_done(self) -> bool:
        return self._batches_done >= self.cfg.budget.batches

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
