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

"""Optuna single-objective search (TPE by default).

Sample-efficiently drives toward the single best (e.g. most failure-prone)
parameter set. Uses Optuna's ask/tell: each batch asks ``per_batch`` trials,
mapping the typed ``search_space`` directly to ``trial.suggest_*`` (no codec
needed). Optuna is imported lazily; install the ``optuna`` extra to use it.
Multi-objective (NSGA-II) is a future addition (the objectives list already
supports it).
"""

import logging
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict

from robovast.common.config import BoolDim, ChoiceDim, FloatDim, IntDim

from ..strategy import SearchStrategy
from ..types import Evaluation, ParamSet, SearchReport

logger = logging.getLogger(__name__)


class OptunaParams(BaseModel):
    """``strategy_parameters`` schema for the Optuna strategy."""
    model_config = ConfigDict(extra='forbid')
    sampler: Literal['tpe', 'cmaes', 'random'] = 'tpe'
    constant_liar: bool = True          # better batched (per-batch) asks for TPE
    n_startup_trials: Optional[int] = None


def _suggest(trial, path, dim):
    if isinstance(dim, BoolDim):
        return trial.suggest_categorical(path, [False, True])
    if isinstance(dim, ChoiceDim):
        return trial.suggest_categorical(path, dim.values)
    if isinstance(dim, FloatDim):
        return trial.suggest_float(path, dim.low, dim.high, log=dim.log)
    if isinstance(dim, IntDim):
        if dim.log:
            return trial.suggest_int(path, dim.low, dim.high, log=True)
        return trial.suggest_int(path, dim.low, dim.high, step=dim.step or 1)
    raise TypeError(f"Unsupported search dimension type: {type(dim).__name__}")


class OptunaStrategy(SearchStrategy):
    PARAMS_MODEL = OptunaParams

    def __init__(self, cfg, params: OptunaParams):
        super().__init__(cfg, params)
        try:
            import optuna
            from optuna.samplers import CmaEsSampler, RandomSampler, TPESampler
        except ImportError as e:  # pragma: no cover - exercised only without the extra
            raise ImportError(
                "Optuna is required for strategy 'optuna'. Install the extra: "
                "pip install 'robovast[optuna]'") from e

        optuna.logging.set_verbosity(optuna.logging.WARNING)
        spec = self.single_objective
        if params.sampler == 'cmaes':
            sampler = CmaEsSampler(seed=cfg.seed)
        elif params.sampler == 'random':
            sampler = RandomSampler(seed=cfg.seed)
        else:
            tpe_kwargs = {"seed": cfg.seed, "constant_liar": params.constant_liar}
            if params.n_startup_trials is not None:
                tpe_kwargs["n_startup_trials"] = params.n_startup_trials
            sampler = TPESampler(**tpe_kwargs)

        self._study = optuna.create_study(direction=spec.direction, sampler=sampler)
        self._objective_name = spec.name
        self._batches_done = 0
        self._trials: dict[str, object] = {}     # ParamSet.id -> Trial (current batch)
        self._history: list[Evaluation] = []

    def ask(self, n: int) -> list[ParamSet]:
        self._trials = {}
        proposals = []
        for _ in range(n):
            trial = self._study.ask()
            values = {path: _suggest(trial, path, dim)
                      for path, dim in self.search_space.items()}
            ps = ParamSet(values=values)
            self._trials[ps.id] = trial
            proposals.append(ps)
        logger.debug("Optuna proposed %d trial(s)", len(proposals))
        return proposals

    def tell(self, evaluations: list[Evaluation]) -> None:
        for ev in evaluations:
            trial = self._trials.get(ev.params.id)
            if trial is None:
                continue
            self._study.tell(trial, float(ev.objectives[self._objective_name]))
        self._history.extend(evaluations)
        self._batches_done += 1

    def report(self) -> SearchReport:
        ranked = sorted(self._history, key=self.objective_value, reverse=True)
        best = ranked[0] if ranked else None
        extra = {"batches": self._batches_done, "n_trials": len(self._history)}
        if self._study.best_trials:
            bt = self._study.best_trials[0]
            extra["optuna_best"] = {"value": bt.value, "params": bt.params}
        return SearchReport(evaluations=list(self._history), best=best, extra=extra)
