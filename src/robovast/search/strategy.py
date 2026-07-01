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

"""The generic search-strategy interface.

A strategy is the "how to choose next" half of the loop; the extractor is the
orthogonal "what to measure" half. Keep this contract minimal so any algorithm
(random, grid, quality-diversity, Optuna, evolutionary, …) fits without interface
changes. Algorithm-specific tuning comes from ``strategy_parameters``, validated
against the strategy's optional ``PARAMS_MODEL``.
"""

from abc import ABC, abstractmethod
from typing import Any, Optional

from robovast.common.config import ObjectiveSpec, SearchConfig

from .plugins import STRATEGY_GROUP, load_ref
from .types import Evaluation, ParamSet, SearchReport


class SearchStrategy(ABC):
    """Proposes parameter sets and learns from their evaluations.

    Subclasses may set ``PARAMS_MODEL`` to a Pydantic model; ``build_strategy``
    validates ``search.strategy_parameters`` against it and passes the parsed
    object as ``params``.
    """

    PARAMS_MODEL: Optional[type] = None

    def __init__(self, cfg: SearchConfig, params: Any):
        self.cfg = cfg
        self.search_space = cfg.search_space
        self.objectives: list[ObjectiveSpec] = cfg.objectives
        self.params = params

    @property
    def single_objective(self) -> ObjectiveSpec:
        """The sole objective (for single-objective strategies)."""
        if len(self.objectives) != 1:
            raise ValueError(
                f"{type(self).__name__} is single-objective but {len(self.objectives)} "
                f"objectives were configured")
        return self.objectives[0]

    def objective_value(self, ev: Evaluation) -> float:
        """The sole objective's value from an evaluation, sign-oriented so that
        **higher is always better** (minimize objectives are negated)."""
        spec = self.single_objective
        value = float(ev.objectives[spec.name])
        return -value if spec.direction == 'minimize' else value

    @abstractmethod
    def ask(self, n: int) -> list[ParamSet]:
        """Propose ``n`` parameter sets to evaluate next."""

    @abstractmethod
    def tell(self, evaluations: list[Evaluation]) -> None:
        """Ingest the evaluations of a previously proposed generation."""

    @abstractmethod
    def report(self) -> SearchReport:
        """Return the current deliverable (ranked best, archive, Pareto front)."""


def build_strategy(cfg: SearchConfig, vast_dir: str = "") -> SearchStrategy:
    """Instantiate the strategy plugin named by ``cfg.strategy``.

    The plugin may be an entry-point name or a local file relative to the
    ``.vast``. ``cfg.strategy_parameters`` is validated against the plugin's
    ``PARAMS_MODEL`` when present.
    """
    strategy_cls = load_ref(cfg.strategy, STRATEGY_GROUP, vast_dir)
    if strategy_cls.PARAMS_MODEL is not None:
        params = strategy_cls.PARAMS_MODEL(**(cfg.strategy_parameters or {}))
    else:
        params = cfg.strategy_parameters or {}
    return strategy_cls(cfg, params)
