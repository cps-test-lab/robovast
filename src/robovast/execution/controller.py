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

"""The unified campaign controller.

One controller drives **both** batch and search locally through one
:class:`~robovast.execution.backends.ExecutionBackend`, producing a single
uniform layout (``<results>/<CAMPAIGN_ID>/<config>/<run>/``) plus a live
``campaign.db`` for every run:

* **batch mode** (no ``search:`` block) — a strategy-less campaign with exactly
  one *batch* of the enumerated configurations.
* **search mode** — the strategy proposes batches; each batch is composed,
  executed, scored (Extractor) and fed back via ``tell``.

A campaign runs one or more *batches*; the batch is a logical grouping recorded
in the store, not a directory level, so batch and search share the flat layout.
"""

import logging
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path

from robovast.common.campaign_data import aggregate_run_status, list_run_dirs
from robovast.common.store import STORE_FILENAME, CampaignStore

from .backends import DockerBackend, ExecutionBackend, RunOptions

logger = logging.getLogger(__name__)

_BAR = "=" * 60


def campaign_id_for(campaign_config) -> str:
    """``<metadata.name>-<timestamp>`` — the campaign directory id (both modes)."""
    name = (campaign_config.metadata or {}).get("name", "campaign")
    return f"{name}-{datetime.now().strftime('%Y-%m-%d-%H%M%S')}"


class CampaignController:
    """Drives a campaign (batch or search) to completion over one backend."""

    def __init__(self, *, campaign_id, results_dir, runs, backend: ExecutionBackend,
                 options: RunOptions, store: CampaignStore, campaign_config_dump: dict,
                 vast_dir: str, strategy=None, evaluator=None, compose=None,
                 per_batch: int = 1, postprocessing=None, batch_campaign_data=None,
                 stop_conditions=None):
        self.campaign_id = campaign_id
        self.campaign_root = os.path.join(results_dir, campaign_id)
        self.runs = runs
        self.backend = backend
        self.options = options
        self.store = store
        self.campaign_config_dump = campaign_config_dump
        self.vast_dir = vast_dir
        self.strategy = strategy
        self.evaluator = evaluator
        self.compose = compose
        self.per_batch = per_batch
        self.batch_campaign_data = batch_campaign_data
        self.mode = "search" if strategy is not None else "batch"
        self.postprocessing = postprocessing or []
        # Combined budget + stopping evaluator (search mode); drives loop end and
        # the per-batch progress line. None in batch mode.
        self.stop_conditions = stop_conditions

    # -- lifecycle ----------------------------------------------------------

    def run(self):
        os.makedirs(self.campaign_root, exist_ok=True)
        # config_dir = the .vast directory: the base against which this campaign's
        # evaluation.visualization notebooks resolve in the results GUI.
        campaign_id = self.store.create_campaign(
            self.campaign_id, self.campaign_config_dump, mode=self.mode,
            config_dir=self.vast_dir)
        if self.strategy is None:
            return self._run_batch_mode(campaign_id)
        return self._run_search(campaign_id)

    # -- batch mode ---------------------------------------------------------

    def _run_batch_mode(self, campaign_id: int) -> dict:
        configs = self.batch_campaign_data["configs"]
        logger.info("\n%s\n📦  Batch run  —  %d configuration(s) × %d run(s)\n%s",
                    _BAR, len(configs), self.runs, _BAR)
        batch_id = self.store.open_batch(campaign_id, 0, self.campaign_root)
        self.backend.run_batch(
            self.batch_campaign_data, campaign_root=self.campaign_root,
            batch_tag="batch-0", runs=self.runs, options=self.options)

        for cfg in configs:
            name = cfg["name"]
            cdir = os.path.join(self.campaign_root, name)
            run_dirs = list_run_dirs(cdir)
            self.store.record_unit(
                batch_id=batch_id, paramset_id=name, config_name=name,
                params=cfg.get("config", {}) or {}, objectives={}, measures={},
                n_samples=len(run_dirs), status=aggregate_run_status(run_dirs),
                result_dir=cdir)
        logger.info("\n%s\n✅  Batch run complete  —  %d configuration(s) in %s\n%s",
                    _BAR, len(configs), self.campaign_root, _BAR)
        return {"mode": "batch", "configs": len(configs), "campaign_root": self.campaign_root}

    # -- search mode --------------------------------------------------------

    def _run_search(self, campaign_id: int):
        from robovast.search.stopping import StopSnapshot
        stop = self.stop_conditions
        obj_name = self.strategy.single_objective.name
        if not stop.has_budget:
            logger.warning("No 'budget' cap configured — this search is bounded "
                           "only by its 'stopping' criteria; it may run a long time.")
        batch_idx = 0
        start = time.monotonic()
        best_objective = None          # best-so-far, in raw objective units
        result = None
        while True:
            param_sets = self.strategy.ask(self.per_batch)
            batch_id = self.store.open_batch(campaign_id, batch_idx, self.campaign_root)
            logger.info("\n%s\n🔁  Batch %d  —  %d parameter set(s)\n%s",
                        _BAR, batch_idx, len(param_sets), _BAR)
            evaluations = self._run_search_batch(param_sets, batch_idx, batch_id)
            self.strategy.tell(evaluations)
            batch_idx += 1
            best_objective = self._update_best(best_objective, evaluations, obj_name)

            snap = StopSnapshot(batch=batch_idx,
                                elapsed=time.monotonic() - start,
                                best_objective=best_objective,
                                metrics=self.strategy.report().extra if stop.needs_metrics else {})
            # Live progress toward every budget/stopping criterion.
            logger.info("📊  %s", " | ".join(
                f"{p.label} {self._fmt(p.current)}/{self._fmt(p.limit)}"
                for p in stop.progress(snap)))
            result = stop.should_stop(snap)
            if result:
                logger.info("\n%s\n⏹  Stopping — %s\n%s", _BAR, result.reason, _BAR)
                break

        elapsed_s = time.monotonic() - start
        self.store.record_outcome(
            campaign_id, stop_kind=result.kind, stop_reason=result.reason,
            batches=batch_idx, elapsed_s=elapsed_s)
        report = self.strategy.report()
        report.extra['stop'] = {"kind": result.kind, "reason": result.reason,
                                "batches": batch_idx, "elapsed_s": elapsed_s}
        logger.info("\n%s\n✅  Search complete  —  %d batch(es), %d evaluation(s) "
                    "(%s)\n%s", _BAR, batch_idx, len(report.evaluations), result.reason, _BAR)
        return report

    @staticmethod
    def _fmt(v):
        return f"{v:.4g}" if isinstance(v, float) else str(v)

    def _update_best(self, best, evaluations, obj_name):
        """Fold this batch's objective values into the best-so-far (raw units,
        direction-aware via the strategy's objective spec)."""
        spec = self.strategy.single_objective
        for ev in evaluations:
            v = ev.objectives.get(obj_name)
            if v is None:
                continue
            v = float(v)
            if best is None:
                best = v
            elif (v < best if spec.direction == 'minimize' else v > best):
                best = v
        return best

    def _run_search_batch(self, param_sets, batch_idx, batch_id):
        """Compose, execute and score one batch.

        Parameter sets are grouped by effective repetition count (``ps.n_reps``
        or the campaign default ``runs``); each group runs with that many reps.
        With the default strategy every set uses the default, so this is a single
        group.
        """
        groups: dict[int, list] = {}
        for ps in param_sets:
            groups.setdefault(ps.n_reps or self.runs, []).append(ps)
        multi = len(groups) > 1

        evaluations = []
        for reps, group in sorted(groups.items()):
            tag = f"batch-{batch_idx}" + (f"/reps-{reps}" if multi else "")
            # Compose into a temp dir (intermediate config artifacts); the backend
            # stages from it and only results land under the campaign root.
            with tempfile.TemporaryDirectory(prefix="robovast_compose_") as artifacts:
                campaign_data, name_by_id = self.compose.compose(group, artifacts)
                self.backend.run_batch(
                    campaign_data, campaign_root=self.campaign_root, batch_tag=tag,
                    runs=reps, options=self.options)
            self._run_postprocessing()

            for ps in group:
                config_name = name_by_id[ps.id]
                config_dir = Path(self.campaign_root) / config_name
                ev = self.evaluator.evaluate(config_dir, ps)
                evaluations.append(ev)
                self.store.record_unit(
                    batch_id=batch_id, paramset_id=ps.id, config_name=config_name,
                    params=ps.values, objectives=ev.objectives, measures=ev.measures,
                    n_samples=ev.n_samples, status="evaluated", result_dir=str(config_dir))
        return evaluations

    def _run_postprocessing(self) -> None:
        """Run search.postprocessing over the campaign root (no-op if none).

        Uses the same loader/runner as ``results_processing.postprocessing`` so a
        plugin (entry-point name or local ``./file.py:Class``) — e.g. one that
        writes per-run ``metrics.csv`` for the extractor — runs identically here.
        """
        if not self.postprocessing:
            return
        # Imported lazily to avoid importing the results_processing stack (and its
        # heavier deps) unless a search actually configures postprocessing.
        from robovast.results_processing.postprocessing import \
            run_postprocessing_commands
        run_postprocessing_commands(
            self.postprocessing, results_dir=self.campaign_root,
            config_dir=self.vast_dir, output=logger.info)


# -- builders ---------------------------------------------------------------

def run_search_campaign(vast_file, campaign_config, results_dir, runs,
                        backend: ExecutionBackend | None = None,
                        options: RunOptions | None = None):
    """Build and run a search campaign. Requires ``campaign_config.search``."""
    from robovast.search.compose import Compose
    from robovast.search.evaluator import Evaluator
    from robovast.search.stopping import build_stop_conditions
    from robovast.search.strategy import build_strategy

    search_cfg = campaign_config.search
    if search_cfg is None:
        raise ValueError("run_search_campaign called without a 'search' block")

    vast_dir = os.path.dirname(os.path.abspath(vast_file))
    runs = runs if runs is not None else campaign_config.execution.runs
    campaign_id = campaign_id_for(campaign_config)
    store = CampaignStore(os.path.join(results_dir, campaign_id, STORE_FILENAME))
    controller = CampaignController(
        campaign_id=campaign_id, results_dir=results_dir, runs=runs,
        backend=backend or DockerBackend(), options=options or RunOptions(),
        store=store, campaign_config_dump=campaign_config.model_dump(),
        vast_dir=vast_dir, strategy=build_strategy(search_cfg, vast_dir),
        evaluator=Evaluator(search_cfg, vast_dir), compose=Compose(vast_file),
        per_batch=search_cfg.per_batch, postprocessing=search_cfg.postprocessing,
        stop_conditions=build_stop_conditions(search_cfg))
    try:
        return controller.run()
    finally:
        store.close()


def run_batch_campaign(vast_file, campaign_config, results_dir, runs, config_filter=None,
                       backend: ExecutionBackend | None = None,
                       options: RunOptions | None = None):
    """Build and run a batch campaign (no ``search:`` block)."""
    import fnmatch

    from robovast.common.config_generation import generate_scenario_variations

    vast_dir = os.path.dirname(os.path.abspath(vast_file))
    runs = runs if runs is not None else campaign_config.execution.runs
    campaign_id = campaign_id_for(campaign_config)

    with tempfile.TemporaryDirectory(prefix="robovast_batch_") as tmp:
        campaign_data, _ = generate_scenario_variations(
            variation_file=vast_file, progress_update_callback=None, output_dir=tmp)
        if not campaign_data["configs"]:
            raise ValueError("No configs found in vast-file")
        if config_filter:
            matched = [c for c in campaign_data["configs"]
                       if fnmatch.fnmatch(c["name"], config_filter)]
            if not matched:
                raise ValueError(f"No configs matched pattern '{config_filter}'")
            campaign_data["configs"] = matched

        store = CampaignStore(os.path.join(results_dir, campaign_id, STORE_FILENAME))
        controller = CampaignController(
            campaign_id=campaign_id, results_dir=results_dir, runs=runs,
            backend=backend or DockerBackend(), options=options or RunOptions(),
            store=store, campaign_config_dump=campaign_config.model_dump(),
            vast_dir=vast_dir, batch_campaign_data=campaign_data)
        try:
            return controller.run()
        finally:
            store.close()
