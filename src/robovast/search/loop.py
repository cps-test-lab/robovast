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

"""The generational search loop.

One generation = ask -> compose -> launch (blocks) -> evaluate -> record ->
tell. The loop owns scoring and persistence; the launcher only dispatches.
"""

import logging
import os
from pathlib import Path

from robovast.common.plugin_ref import load_ref

from .compose import Compose
from .evaluator import Evaluator
from .launcher import Launcher, LocalLauncher
from .strategy import SearchStrategy, build_strategy
from .types import SearchReport

logger = logging.getLogger(__name__)

# Search extraction reuses the postprocessing-plugin interface/registry.
POSTPROCESSING_GROUP = "robovast.postprocessing_commands"


class SearchLoop:
    """Drives a closed-loop search to completion.

    Args:
        vast_file: Path to the campaign ``.vast`` (configs are synthesized from
            its ``search_space``).
        output_dir: Root directory for generation outputs.
        runs: Runs per config (``execution.runs``).
        store: A :class:`~robovast.common.store.CampaignStore`.
        strategy: The search strategy.
        evaluator: The objective/descriptor evaluator.
        compose: The :class:`Compose` bound to ``vast_file``.
        launcher: The :class:`Launcher` used to execute each generation.
        per_step: Parameter sets proposed per generation.
    """

    def __init__(self, vast_file, output_dir, runs, store, strategy: SearchStrategy,
                 evaluator: Evaluator, compose: Compose, launcher: Launcher, per_step: int,
                 postprocessing: list | None = None, vast_dir: str = ""):
        self.vast_file = vast_file
        self.output_dir = output_dir
        self.runs = runs
        self.store = store
        self.strategy = strategy
        self.evaluator = evaluator
        self.compose = compose
        self.launcher = launcher
        self.per_step = per_step
        self.vast_dir = vast_dir or os.path.dirname(os.path.abspath(vast_file))
        # Search-only extraction plugins (postprocessing-style), resolved from
        # entry points or local files; run per generation before scoring.
        self._postprocessors = [
            load_ref(ref, POSTPROCESSING_GROUP, self.vast_dir)()
            for ref in (postprocessing or [])
        ]

    def run(self, campaign_name: str, campaign_config: dict) -> SearchReport:
        os.makedirs(self.output_dir, exist_ok=True)
        # config_dir is the .vast directory: the base against which this campaign's
        # evaluation.visualization notebooks (analysis/*.ipynb) resolve in the GUI.
        campaign_id = self.store.create_campaign(
            campaign_name, campaign_config, mode="search", config_dir=self.vast_dir)
        gen_idx = 0
        while not self.strategy.is_done():
            param_sets = self.strategy.ask(self.per_step)
            gen_dir = os.path.join(self.output_dir, f"generation-{gen_idx}")
            os.makedirs(gen_dir, exist_ok=True)
            gen_id = self.store.open_generation(campaign_id, gen_idx, gen_dir)
            bar = "=" * 60
            logger.info("\n%s\n🔁  Generation %d  —  %d parameter set(s)\n%s",
                        bar, gen_idx, len(param_sets), bar)

            evaluations = self._run_generation(param_sets, gen_dir, gen_id)
            self.strategy.tell(evaluations)
            gen_idx += 1

        report = self.strategy.report()
        bar = "=" * 60
        logger.info("\n%s\n✅  Search complete  —  %d generation(s), %d evaluation(s)\n%s",
                    bar, gen_idx, len(report.evaluations), bar)
        return report

    def _run_generation(self, param_sets, gen_dir, gen_id):
        """Compose, launch and evaluate one generation.

        Parameter sets are grouped by their effective repetition count
        (``ps.n_reps`` or the campaign default ``runs``) and each group is
        launched as its own batch with that many runs. With the default strategy
        every set uses the default, so this is a single batch; a noise-aware
        strategy that requests different repetitions per set gets each honoured.
        """
        groups: dict[int, list] = {}
        for ps in param_sets:
            groups.setdefault(ps.n_reps or self.runs, []).append(ps)

        evaluations = []
        for reps, group in sorted(groups.items()):
            # Isolate each rep-group's campaign output so multiple launches in
            # one generation don't collide; single-group is the common case.
            sub_dir = gen_dir if len(groups) == 1 else os.path.join(gen_dir, f"reps-{reps}")
            os.makedirs(sub_dir, exist_ok=True)
            campaign_data, name_by_id = self.compose.compose(
                group, os.path.join(sub_dir, "_artifacts"))
            result_dir = self.launcher.launch(campaign_data, sub_dir, reps)
            self._run_postprocessing(result_dir)

            for ps in group:
                config_name = name_by_id[ps.id]
                config_dir = Path(result_dir) / config_name
                ev = self.evaluator.evaluate(config_dir, ps)
                evaluations.append(ev)
                self.store.record_unit(
                    generation_id=gen_id, paramset_id=ps.id, config_name=config_name,
                    params=ps.values, objectives=ev.objectives, measures=ev.measures,
                    n_samples=ev.n_samples, status="evaluated", result_dir=str(config_dir),
                )
        return evaluations

    def _run_postprocessing(self, result_dir: str) -> None:
        """Run search-only extraction plugins over a generation's results.

        Reuses the postprocessing-plugin interface directly (NOT the analysis
        ``results_processing`` pipeline). No-op when none are configured.
        """
        for plugin in self._postprocessors:
            logger.debug("Search postprocessing %s on %s", type(plugin).__name__, result_dir)
            plugin(result_dir, self.vast_dir)


def run_search(vast_file, campaign_config, output_dir, runs, launcher: Launcher | None = None):
    """Build the loop's collaborators from config and run the search.

    ``campaign_config`` is the validated :class:`ConfigV1` (as a model). Requires
    ``campaign_config.search`` to be set.
    """
    from robovast.common.store import STORE_FILENAME, CampaignStore

    search_cfg = campaign_config.search
    if search_cfg is None:
        raise ValueError("run_search called without a 'search' block")

    vast_dir = os.path.dirname(os.path.abspath(vast_file))
    strategy = build_strategy(search_cfg, vast_dir)
    evaluator = Evaluator(search_cfg, vast_dir)
    compose = Compose(vast_file)
    launcher = launcher or LocalLauncher()
    store = CampaignStore(os.path.join(output_dir, STORE_FILENAME))

    loop = SearchLoop(
        vast_file=vast_file, output_dir=output_dir, runs=runs, store=store,
        strategy=strategy, evaluator=evaluator, compose=compose, launcher=launcher,
        per_step=search_cfg.per_step, postprocessing=search_cfg.postprocessing,
        vast_dir=vast_dir,
    )
    try:
        return loop.run(os.path.basename(output_dir), campaign_config.model_dump())
    finally:
        store.close()
