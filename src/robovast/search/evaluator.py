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

"""Scoring: per-config result directory -> :class:`Evaluation`.

Strategy-independent. Instantiates the one configured :class:`Extractor` (built-in
or a local file, parameterized from the ``.vast``), runs it per config, and wraps
its objectives + measures into an :class:`Evaluation`. The framework counts
``n_samples`` so it always matches what the extractor aggregated over.
"""

import logging
from pathlib import Path

from robovast.common.config import SearchConfig

from .extractor import Extractor, NoSampleError, completed_run_dirs
from .plugins import EXTRACTOR_GROUP, load_ref
from .types import Evaluation, ParamSet

logger = logging.getLogger(__name__)


class Evaluator:
    """Applies the configured extractor to score parameter sets."""

    def __init__(self, cfg: SearchConfig, vast_dir: str = ""):
        # Make the workspace's `plugins:` importable HERE before resolving the extractor.
        # load_ref exec's a local `./file.py:Class` extractor's module in *this* process, so
        # its third-party imports resolve off this sys.path -- and nothing else leads it with
        # .robovast_plugins/: compose only does so inside its subprocess, and the controller's
        # plugin-install phase is materialize-only by design. Without this, `plugins:` was
        # silently useless for a search extractor (ModuleNotFoundError however it was declared),
        # while the same declaration worked for postprocessing, which does call this.
        if vast_dir:
            from robovast.common.config_plugins import \
                ensure_plugins_importable  # pylint: disable=import-outside-toplevel
            ensure_plugins_importable(vast_dir)
        extractor_cls = load_ref(cfg.extract.plugin, EXTRACTOR_GROUP, vast_dir)
        self.extractor: Extractor = extractor_cls(**cfg.extract.params)
        self.objective_names = [o.name for o in cfg.objectives]
        # Kept for the messages below: a refusal that names the extractor is one whose
        # reader knows which file to go and look at.
        self._plugin = cfg.extract.plugin

    def _require_declared_files(self, config_dir: Path, completed) -> None:
        """Refuse a cell whose extractor declared a file none of its runs has.

        A name absent from *every* completed run is the structural case, and the one that
        has actually happened: the file is written by a ``postprocessing:`` step, and
        extraction runs between batches, before postprocessing runs at all (see
        :mod:`robovast.search.extractor`). Nothing about that was visible -- the
        extractor's own code met a path that was not there and returned a number, so the
        objective was the same value for every parameter set in the space and the search
        had no gradient to climb while reporting itself converged.

        Missing from only some runs is not touched: that is one trial's data being odd,
        and an extractor aggregating over runs is where the decision about it belongs.
        """
        declared = getattr(self.extractor, "requires_run_files", ()) or ()
        if isinstance(declared, str):  # a bare string iterates as characters
            declared = (declared,)
        for name in declared:
            if any((run_dir / name).exists() for run_dir in completed):
                continue
            raise NoSampleError(
                f"{config_dir}: '{self._plugin}' declares it reads '{name}', and no "
                f"completed run of this configuration has that file. If a "
                f"`postprocessing:` step produces it, that is the whole explanation: "
                f"extraction scores a batch so the strategy can be asked for the next "
                f"one, and postprocessing runs once over the finished campaign -- so "
                f"nothing it writes exists yet. Read what the runner wrote, or move the "
                f"measurement into the scenario.")

    def evaluate(self, config_dir: Path, params: ParamSet) -> Evaluation:
        completed = completed_run_dirs(config_dir)
        # Before the extractor, not after: once it has been handed a directory whose files
        # are not there, what happens is up to code this class did not write, and what
        # that code does is return a number.
        self._require_declared_files(config_dir, completed)
        result = self.extractor.extract(config_dir)
        missing = [n for n in self.objective_names if n not in result.objectives]
        if missing:
            raise ValueError(
                f"Extractor did not return configured objective(s) {missing} for "
                f"{config_dir}; it returned {sorted(result.objectives)}")
        # An extractor may report more than it was asked for -- a nav extractor returns a
        # failure rate and a time to goal beside the robustness the .vast optimizes. Those
        # are measurements, not objectives, and passing them through made `objectives` mean
        # "whatever came back" instead of "the objectives the campaign declared". That is
        # not cosmetic: `CampaignStore.record_unit` lifts the queryable scalar
        # `unit.objective` only out of a single-objective dict, so a single-objective search
        # whose extractor also reported two diagnostics stored NULL in every row -- taking
        # `run_view.objective`, `runs.objective` and the whole objective trajectory with it,
        # while the campaign looked fully recorded. Narrowed HERE, in declared order,
        # because this is the one place that knows what the campaign declared; every reader
        # downstream then gets a dict that means what its name says.
        objectives = {n: result.objectives[n] for n in self.objective_names}
        extras = {n: v for n, v in result.objectives.items() if n not in objectives}
        clashing = sorted(set(extras) & set(result.measures))
        if clashing:
            # The same class of defect as a missing objective, and no safer to guess at:
            # one of the two values would have to win silently.
            raise ValueError(
                f"Extractor reported {clashing} both as a measure and beside the objectives "
                f"for {config_dir}; a name must mean one thing.")
        # Kept rather than dropped: the extractor measured them, and `measures` is where a
        # named measurement that is not optimized already lives.
        measures = {**result.measures, **extras}
        n_samples = len(completed)
        logger.debug("Evaluated %s -> objectives=%s measures=%s n=%d",
                     params.id, objectives, measures, n_samples)
        return Evaluation(params=params, objectives=objectives,
                          measures=measures, n_samples=n_samples,
                          raw={"config_dir": str(config_dir)})
