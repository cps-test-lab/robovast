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
        """Ingest the evaluations of a previously proposed generation.

        **The list may be shorter than what :meth:`ask` proposed, and an implementation
        must cope.** A draw the variation pipeline cannot realize composes into no
        config, so it never runs and has no evaluation; so does a cell whose every run
        was lost to infrastructure. The batch loop records both and carries on with the
        rest, because discarding a batch — or the campaign — over one unusable draw
        throws away every batch already finished.

        Cope means ingest what arrived. It does not mean invent the rest: a strategy that
        cannot take a short generation must skip the generation, not fill it with a
        fabricated objective or measure (see :meth:`QDStrategy._tell_incomplete`).
        """

    @abstractmethod
    def report(self) -> SearchReport:
        """Return the current deliverable (ranked best, archive, Pareto front)."""

    #: Whether :meth:`resume` reproduces this strategy exactly. ``True`` because the default
    #: implementation below is correct for any strategy that is a function of its seed and
    #: the evaluations it was told, which is every strategy shipped here. A strategy that is
    #: nondeterministic for a reason a seed cannot fix -- it reads the wall clock, or state
    #: outside this process -- sets it ``False``, and its campaigns are then not resumed
    #: rather than resumed into a subtly different search.
    RESUMABLE: bool = True

    def resume(self, batches) -> None:
        """Re-drive this strategy through the ask/tell sequence a past run recorded.

        *batches* is the campaign's own record, in execution order (see
        :func:`robovast.search.history.recorded_batches`). Nothing about a strategy's
        internal state is serialized anywhere; this replays its **public interface**, which
        is what makes the default correct for every strategy at once rather than a
        durability contract each one has to implement.

        The proposals are discarded. They are asked for anyway because ``ask`` is what
        advances a strategy's sequence -- each of the strategies here holds a seeded RNG or
        a sequence index, and one told the evaluations without being asked for the
        proposals would resume with its stream rewound and re-draw points it had already
        spent.

        Batch by batch, in the original order, rather than one bulk ``ask`` and one bulk
        ``tell``, for the same reason turned around: a strategy may consult what it has been
        told *while* proposing (``BoundaryRefinement`` does), so only the original
        interleaving reproduces the original draws.

        ``asked`` is the number **proposed**, which is not always the number told back: a
        draw the variation pipeline could not realize, or one whose every run was lost,
        costs a proposal and produces no evaluation. :meth:`tell` already copes with a
        short generation, and this relies on exactly that contract rather than adding one.

        A resumed search reproduces the original only if the strategy is seeded --
        ``search.seed``. Without it a fresh process re-seeds from entropy, and the replay
        rebuilds a *different* search; the caller checks that before getting here.
        """
        for batch in batches:
            self.ask(batch.asked)
            self.tell(batch.evaluations)


def build_strategy(cfg: SearchConfig, vast_dir: str = "") -> SearchStrategy:
    """Instantiate the strategy plugin named by ``cfg.strategy``.

    The plugin may be an entry-point name or a local file relative to the
    ``.vast``. ``cfg.strategy_parameters`` is validated against the plugin's
    ``PARAMS_MODEL`` when present.

    Leads ``sys.path`` with the ``.vast``'s ``plugins:`` first, for the same reason
    :class:`~robovast.search.evaluator.Evaluator` does: ``load_ref`` exec's a local
    ``./file.py:Class`` strategy's module in *this* process, so its third-party imports
    resolve off this path and nothing else prepares it -- compose only does so in its
    subprocess, and the controller's plugin-install phase is materialize-only. A strategy
    is the same kind of in-process plugin consumer as an extractor and needs the same
    treatment; without it ``plugins:`` was silently useless for one.
    """
    if vast_dir:
        from robovast.common.config_plugins import \
            ensure_plugins_importable  # pylint: disable=import-outside-toplevel
        ensure_plugins_importable(vast_dir)
    strategy_cls = load_ref(cfg.strategy, STRATEGY_GROUP, vast_dir)
    if strategy_cls.PARAMS_MODEL is not None:
        params = strategy_cls.PARAMS_MODEL(**(cfg.strategy_parameters or {}))
    else:
        params = cfg.strategy_parameters or {}
    return strategy_cls(cfg, params)
