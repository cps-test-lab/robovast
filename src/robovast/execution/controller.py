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
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

from robovast.common.campaign_data import aggregate_run_status, list_run_dirs
from robovast.common.logging_config import (add_campaign_log_handler,
                                            remove_campaign_log_handler)
from robovast.common.store import STORE_FILENAME, CampaignStore

from .backends import DockerBackend, ExecutionBackend, RunOptions
from .notify import Notifier

# Use the qualified name rather than __name__: this module is the in-pod cluster
# entrypoint (``python -m robovast.execution.controller``), where __name__ is
# "__main__" and would not propagate to the "robovast" logger that
# add_campaign_log_handler attaches controller.log to — dropping the controller's
# own lines (banners, progress) from the file while they still reach stderr.
logger = logging.getLogger("robovast.execution.controller")

_BAR = "=" * 60


def campaign_id_for(campaign_config) -> str:
    """``<metadata.name>-<timestamp>`` — the campaign directory id (both modes).

    Underscores in the name are normalised to hyphens so a local campaign id
    matches the cluster's: storage bucket names disallow underscores, so the
    cluster sanitises the name to hyphens — doing it here keeps both identical.
    """
    name = (campaign_config.metadata or {}).get("name", "campaign").replace("_", "-")
    return f"{name}-{datetime.now().strftime('%Y-%m-%d-%H%M%S')}"


class CampaignController:
    """Drives a campaign (batch or search) to completion over one backend."""

    def __init__(self, *, campaign_id, results_dir, runs, backend: ExecutionBackend,
                 options: RunOptions, store: CampaignStore, campaign_config_dump: dict,
                 vast_dir: str, strategy=None, evaluator=None, compose=None,
                 per_batch: int = 1, postprocessing=None, batch_campaign_data=None,
                 stop_conditions=None, state=None, notifier=None):
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
        # Optional control-channel state (cluster mode). When set, the controller
        # publishes loop phase/progress and honours the cooperative `stop` command.
        self.state = state
        # ntfy push notifications (no-op when no topic is configured). Built bound
        # to this campaign id so concurrent campaigns report independently.
        self.notifier = notifier or Notifier.from_env(campaign_id)
        self._history: list[dict] = []        # per-batch summaries for /status
        # Run-level progress poller plumbing (set up only when `state` is present
        # and the backend can introspect storage).
        self._poller = None
        self._poller_stop = threading.Event()
        self._batch_active = threading.Event()
        self._batch_baseline = 0
        self._batch_total = 0

    # -- lifecycle ----------------------------------------------------------

    def run(self):
        os.makedirs(self.campaign_root, exist_ok=True)
        # Tee the controller's own log into the campaign artifact. Attached before
        # the loop starts and closed in the finally below, so the file is complete
        # and flushed before the builders' finally calls finalize_campaign (which,
        # for cluster runs, uploads the whole campaign_root — including this file —
        # to storage). Best-effort: a logging failure must never abort a campaign.
        try:
            log_handler = add_campaign_log_handler(
                os.path.join(self.campaign_root, "_execution", "controller.log"))
        except Exception:  # pylint: disable=broad-except
            logger.warning("Could not open controller.log; continuing without it.",
                           exc_info=True)
            log_handler = None
        # Paths are stored relative to the campaign root (the dir holding
        # campaign.db) so the store survives the campaign being moved or
        # downloaded from the container that produced it. config_dir is the
        # in-campaign "_config" copy of the .vast: the base against which this
        # campaign's evaluation.visualization notebooks resolve in the GUI.
        campaign_id = self.store.create_campaign(
            self.campaign_id, self.campaign_config_dump, mode=self.mode,
            config_dir="_config")
        if self.state is not None:
            self.state.update(mode=self.mode, campaign_id=self.campaign_id)
            self.state.set_phase("running")
        self._start_progress_poller()
        self.notifier.start_heartbeat(status_fn=self._notify_status)
        try:
            if self.strategy is None:
                result = self._run_batch_mode(campaign_id)
            else:
                result = self._run_search(campaign_id)
            if self.state is not None:
                self.state.set_phase("finished")
            return result
        except BaseException as exc:
            if self.state is not None:
                self.state.set_phase("failed")
            self.notifier.failed(f"{type(exc).__name__}: {exc}")
            raise
        finally:
            self._stop_progress_poller()
            self.notifier.stop_heartbeat()
            remove_campaign_log_handler(log_handler)

    # -- run-level progress poller ------------------------------------------

    _POLL_INTERVAL = 3.0

    def _start_progress_poller(self) -> None:
        """Start a daemon thread that publishes current-batch run progress.

        Skipped when there is no control channel, or the backend can't introspect
        storage (returns ``None`` — e.g. the local backend). ``backend.run_batch``
        blocks for a whole batch, so this runs the count concurrently.
        """
        if self.state is None:
            return
        try:
            if self.backend.count_run_artifacts(self.campaign_id) is None:
                return
        except Exception:  # pylint: disable=broad-except
            return

        def _poll() -> None:
            while not self._poller_stop.is_set():
                if self._batch_active.is_set():
                    try:
                        done = self.backend.count_run_artifacts(self.campaign_id)
                        if done is not None:
                            completed = min(max(0, done - self._batch_baseline), self._batch_total)
                            self.state.update(
                                runs={"completed": completed, "total": self._batch_total})
                    except Exception:  # pylint: disable=broad-except
                        pass
                self._poller_stop.wait(self._POLL_INTERVAL)

        self._poller = threading.Thread(target=_poll, name="robovast-progress-poller",
                                        daemon=True)
        self._poller.start()

    def _stop_progress_poller(self) -> None:
        if self._poller is not None:
            self._poller_stop.set()

    def _notify_status(self):
        """Heartbeat source: current batch + run progress within it.

        Returns ``(batch, completed, total, batches_done)`` or ``None`` when no
        control channel is available (then the heartbeat skips this tick).
        """
        if self.state is None:
            return None
        s = self.state.snapshot()
        return (s.batch, s.runs.completed, s.runs.total, s.batches_done)

    def _begin_batch_progress(self, total: int) -> None:
        """Capture the cumulative-run baseline before a batch's jobs upload."""
        if self.state is None or self._poller is None:
            return
        try:
            self._batch_baseline = self.backend.count_run_artifacts(self.campaign_id) or 0
        except Exception:  # pylint: disable=broad-except
            self._batch_baseline = 0
        self._batch_total = total
        self.state.update(runs={"completed": 0, "total": total})
        self._batch_active.set()

    def _end_batch_progress(self) -> None:
        if self.state is None or self._poller is None:
            return
        self._batch_active.clear()
        self.state.update(runs={"completed": self._batch_total, "total": self._batch_total})

    # -- batch mode ---------------------------------------------------------

    def _run_batch_mode(self, campaign_id: int) -> dict:
        configs = self.batch_campaign_data["configs"]
        logger.info("\n%s\n📦  Batch run  —  %d configuration(s) × %d run(s)\n%s",
                    _BAR, len(configs), self.runs, _BAR)
        batch_id = self.store.open_batch(campaign_id, 0, ".")
        if self.state is not None:
            self.state.update(batch=0)
        self._begin_batch_progress(len(configs) * self.runs)
        try:
            self.backend.run_batch(
                self.batch_campaign_data, campaign_root=self.campaign_root,
                batch_tag="batch-0", runs=self.runs, options=self.options)
        finally:
            self._end_batch_progress()

        for cfg in configs:
            name = cfg["name"]
            cdir = os.path.join(self.campaign_root, name)
            run_dirs = list_run_dirs(cdir)
            self.store.record_unit(
                batch_id=batch_id, paramset_id=name, config_name=name,
                params=cfg.get("config", {}) or {}, objectives={}, measures={},
                n_samples=len(run_dirs), status=aggregate_run_status(run_dirs),
                result_dir=os.path.relpath(cdir, self.campaign_root))
        if self.state is not None:
            self.state.update(batches_done=1,
                              batch_history=[{"idx": 0, "n_units": len(configs)}])
        self.notifier.batch_finished(0, len(configs))
        logger.info("\n%s\n✅  Batch run complete  —  %d configuration(s) in %s\n%s",
                    _BAR, len(configs), self.campaign_root, _BAR)
        return {"mode": "batch", "configs": len(configs), "campaign_root": self.campaign_root}

    # -- search mode --------------------------------------------------------

    def _run_search(self, campaign_id: int):
        from robovast.search.stopping import StopResult, StopSnapshot
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
            batch_id = self.store.open_batch(campaign_id, batch_idx, ".")
            if self.state is not None:
                self.state.update(batch=batch_idx)
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
            progress = stop.progress(snap)
            # Live progress toward every budget/stopping criterion.
            logger.info("📊  %s", " | ".join(
                f"{p.label} {self._fmt(p.current)}/{self._fmt(p.limit)}" for p in progress))
            if self.state is not None:
                self._history.append({"idx": batch_idx - 1, "n_units": len(evaluations)})
                self.state.update(batches_done=batch_idx, best_objective=best_objective,
                                  budget=[self._budget_item(p) for p in progress],
                                  batch_history=list(self._history))
            self.notifier.batch_finished(batch_idx - 1, len(evaluations))
            result = stop.should_stop(snap)
            if not result and self.state is not None and self.state.stop_requested:
                result = StopResult(kind="external",
                                    reason="stop requested via control API")
            if result:
                if self.state is not None:
                    self.state.set_phase("finishing")
                    self.state.update(stop={"kind": result.kind, "reason": result.reason})
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

    @staticmethod
    def _budget_item(p) -> dict:
        """Convert a CriterionProgress to a JSON-safe /status budget item.

        ``current`` may be NaN (e.g. target_objective before any result); NaN is
        not valid JSON, so it is reported as ``None``.
        """
        import math
        cur = float(p.current)
        return {"label": p.label,
                "current": None if math.isnan(cur) else cur,
                "limit": float(p.limit), "done": bool(p.done)}

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

        # Expected runs across the whole batch (all reps-groups), for run progress.
        self._begin_batch_progress(sum((ps.n_reps or self.runs) for ps in param_sets))
        evaluations = []
        try:
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
                        n_samples=ev.n_samples, status="evaluated",
                        result_dir=os.path.relpath(config_dir, self.campaign_root))
        finally:
            self._end_batch_progress()
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

def _finalize(backend: ExecutionBackend, campaign_root: str) -> None:
    """Run the backend's campaign finalize hook, best-effort.

    Called from the builders' ``finally`` after the store is closed (so
    ``campaign.db`` is fully flushed). Never masks an in-flight exception.
    """
    try:
        backend.finalize_campaign(campaign_root)
    except Exception:  # pylint: disable=broad-except
        logger.warning("Campaign finalize hook failed", exc_info=True)

def _make_upload_progress_cb(state):
    """Return a ``(bytes_sent, total_bytes)`` callback that publishes throttled
    upload progress into ``Status.extra['upload']``, or ``None`` if there is no
    control channel.

    The callback derives transfer *rate* from the gap between published samples
    (the providers report only sent/total) and throttles writes to ≥1% advance or
    ≥0.5 s elapsed (plus the final 100% sample) to keep lock churn low. A fresh
    callback is created per upload attempt so its rate baseline resets on retry.
    """
    if state is None:
        return None
    last = {"t": None, "sent": 0, "pushed_pct": -1.0}

    def _cb(sent, total):
        now = time.time()
        pct = (sent / total * 100.0) if total else 0.0
        if (last["t"] is not None and pct - last["pushed_pct"] < 1.0
                and now - last["t"] < 0.5 and sent < total):
            return
        rate = None
        if last["t"] is not None and now > last["t"]:
            rate = (sent - last["sent"]) / (now - last["t"])
        last.update(t=now, sent=sent, pushed_pct=pct)
        state.update(extra={"upload": {"sent": sent, "total": total,
                                       "rate": rate, "updated_at": now}})

    return _cb


def _upload_to_share_with_retrigger(cluster_config, campaign_id: str, provider,
                                    state, notifier=None) -> int:
    """Run upload-to-share after a finished campaign; on failure stay alive.

    The campaign is already published to storage (``finalize_campaign``). This
    compresses it and uploads it to the share. On success the function returns 0
    (the controller then exits, pod ``Succeeded``). On failure it keeps the
    controller alive — parking the main thread on the control channel — so the
    user can retry with ``vast exec cluster upload-to-share`` (optionally with
    corrected credentials) or abandon with ``stop``.

    Returns a process exit code.
    """
    from robovast.execution.cluster_execution import \
        in_pod_upload  # pylint: disable=import-outside-toplevel

    notifier = notifier or Notifier.from_env(campaign_id)

    if state is not None:
        state.update(share_provider=provider.SHARE_TYPE)
        state.set_phase("uploading", stage="upload-to-share")

    if in_pod_upload.upload_campaign(cluster_config, campaign_id, provider,
                                     progress_cb=_make_upload_progress_cb(state)):
        if state is not None:
            state.update(extra={})  # clear the upload progress bar
            state.set_phase("finished", stage="uploaded")
        logger.info("Campaign uploaded to share (%s).", provider.SHARE_TYPE)
        notifier.uploaded(provider.SHARE_TYPE)
        return 0

    logger.error(
        "upload-to-share failed. The campaign is safe in storage. The controller "
        "will stay alive — retry with 'vast exec cluster upload-to-share' (it "
        "reuses the injected credentials, or pass corrected ones — even a "
        "different share type), or 'vast exec cluster stop' to give up.")
    if state is None:
        notifier.failed("upload-to-share failed (no control channel to retry)")
        return 1  # no control channel → nothing could retrigger
    state.update(extra={})  # drop any stale progress bar from the failed attempt
    state.set_phase("uploading", stage="upload-failed")

    while True:
        action, overrides = state.wait_for_retrigger()
        if action == "abandon":
            logger.warning("Upload abandoned via stop; terminating "
                           "(campaign already published to storage).")
            notifier.failed("upload-to-share abandoned via stop")
            return 1
        logger.info("Retrying upload-to-share...")
        # Rebuild the provider (the retrigger may switch type and/or supply
        # corrected credentials), then re-run the launch-time pre-flight check so
        # a bad/switched target fails fast instead of after a wasted re-compress.
        try:
            retry_provider = in_pod_upload.load_provider_from_env(overrides)
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Cannot build share provider for retry: %s", exc)
            retry_provider = None
        if retry_provider is None:
            logger.error("No usable share provider for retry; waiting again.")
            state.set_phase("uploading", stage="upload-failed")
            continue
        state.update(share_provider=retry_provider.SHARE_TYPE)
        state.set_phase("uploading", stage="upload-to-share")
        try:
            in_pod_upload.verify_share_access(retry_provider)
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Share credential check failed on retry (%s): %s",
                         retry_provider.SHARE_TYPE, exc)
            state.set_phase("uploading", stage="upload-failed")
            continue
        if in_pod_upload.upload_campaign(
                cluster_config, campaign_id, retry_provider,
                progress_cb=_make_upload_progress_cb(state)):
            state.update(extra={})  # clear the upload progress bar
            state.set_phase("finished", stage="uploaded")
            logger.info("Campaign uploaded to share (%s, retry).",
                        retry_provider.SHARE_TYPE)
            notifier.uploaded(retry_provider.SHARE_TYPE)
            return 0
        state.update(extra={})  # drop stale progress bar from the failed retry
        state.set_phase("uploading", stage="upload-failed")


def run_search_campaign(vast_file, campaign_config, results_dir, runs,
                        backend: ExecutionBackend | None = None,
                        options: RunOptions | None = None, campaign_id=None, state=None,
                        notifier=None):
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
    campaign_id = campaign_id or campaign_id_for(campaign_config)
    be = backend or DockerBackend()
    store = CampaignStore(os.path.join(results_dir, campaign_id, STORE_FILENAME))
    controller = CampaignController(
        campaign_id=campaign_id, results_dir=results_dir, runs=runs,
        backend=be, options=options or RunOptions(),
        store=store, campaign_config_dump=campaign_config.model_dump(),
        vast_dir=vast_dir, strategy=build_strategy(search_cfg, vast_dir),
        evaluator=Evaluator(search_cfg, vast_dir), compose=Compose(vast_file),
        per_batch=search_cfg.per_batch, postprocessing=search_cfg.postprocessing,
        stop_conditions=build_stop_conditions(search_cfg), state=state, notifier=notifier)
    try:
        return controller.run()
    finally:
        store.close()
        _finalize(be, os.path.join(results_dir, campaign_id))


def filter_configs_by_name(configs, config_filter):
    """Select campaign configs whose expanded name matches ``config_filter``.

    Matching is a glob against the expanded variation name (e.g.
    ``config1-1-1-1``), so a bare config-block name like ``config1`` matches
    nothing — use ``config1*`` to select the whole block.

    Raises ``ValueError`` listing the available config names when nothing
    matches, so a typo is reported with actionable choices.
    """
    import fnmatch

    matched = [c for c in configs if fnmatch.fnmatch(c["name"], config_filter)]
    if not matched:
        available = "\n".join(f"  - {c['name']}" for c in configs)
        raise ValueError(
            f"No configs matched pattern '{config_filter}'.\n"
            f"Available configs:\n{available}")
    return matched


def build_campaign_data(vast_file, output_dir, config_filter=None):
    """Generate the batch campaign data and apply the optional ``--config`` filter.

    Shared by :func:`run_batch_campaign` and the host-side ``cluster run``
    pre-flight check so both select configs through exactly the same code path.
    Raises ``ValueError`` if the vast-file yields no configs or the filter matches
    none (the message lists the available config block names).
    """
    from robovast.common.config_generation import generate_scenario_variations

    campaign_data, transient_files = generate_scenario_variations(
        variation_file=vast_file, progress_update_callback=None, output_dir=output_dir)
    if not campaign_data["configs"]:
        raise ValueError("No configs found in vast-file")
    if config_filter:
        campaign_data["configs"] = filter_configs_by_name(
            campaign_data["configs"], config_filter)
    return campaign_data, transient_files


def run_batch_campaign(vast_file, campaign_config, results_dir, runs, config_filter=None,
                       backend: ExecutionBackend | None = None,
                       options: RunOptions | None = None, campaign_id=None, state=None,
                       notifier=None):
    """Build and run a batch campaign (no ``search:`` block)."""
    vast_dir = os.path.dirname(os.path.abspath(vast_file))
    runs = runs if runs is not None else campaign_config.execution.runs
    campaign_id = campaign_id or campaign_id_for(campaign_config)

    with tempfile.TemporaryDirectory(prefix="robovast_batch_") as tmp:
        campaign_data, _ = build_campaign_data(vast_file, tmp, config_filter)

        be = backend or DockerBackend()
        store = CampaignStore(os.path.join(results_dir, campaign_id, STORE_FILENAME))
        controller = CampaignController(
            campaign_id=campaign_id, results_dir=results_dir, runs=runs,
            backend=be, options=options or RunOptions(),
            store=store, campaign_config_dump=campaign_config.model_dump(),
            vast_dir=vast_dir, batch_campaign_data=campaign_data, state=state,
            notifier=notifier)
        try:
            return controller.run()
        finally:
            store.close()
            _finalize(be, os.path.join(results_dir, campaign_id))


# -- in-pod entrypoint ------------------------------------------------------

def _build_cluster_backend(namespace, kube_context, log_tree):
    """Reconstruct the cluster config from the env and build a KubernetesBackend.

    The host injects ``ROBOVAST_CLUSTER_CONFIG_NAME`` and (optionally)
    ``ROBOVAST_CLUSTER_CONFIG_KWARGS`` (JSON) when launching the controller pod,
    so the in-pod controller reuses the very same cluster config object the host
    uses for storage and scheduling (its ``get_s3_endpoint()`` is the
    cluster-internal endpoint, so all storage traffic stays in-cluster).
    """
    import json

    from robovast.execution.cluster_execution.cluster_setup import \
        get_cluster_config
    from robovast.execution.cluster_execution.kubernetes_backend import \
        KubernetesBackend

    name = os.environ.get("ROBOVAST_CLUSTER_CONFIG_NAME")
    if not name:
        raise RuntimeError(
            "ROBOVAST_CLUSTER_CONFIG_NAME is not set. The controller is meant to be "
            "launched by 'vast exec cluster run', which injects the cluster config."
        )
    cluster_config = get_cluster_config(name)
    kwargs_json = os.environ.get("ROBOVAST_CLUSTER_CONFIG_KWARGS")
    if kwargs_json:
        cluster_config.restore_from_setup_kwargs(json.loads(kwargs_json))
    return KubernetesBackend(
        cluster_config=cluster_config, namespace=namespace,
        kube_context=kube_context, log_tree=log_tree)


def main(argv=None):
    """Run a campaign controller inside the cluster controller pod.

    ``vast exec cluster run`` copies the campaign inputs + the dev wheel into the
    controller pod and invokes ``python -m robovast.execution.controller`` here.
    The backend is always :class:`KubernetesBackend` — this entrypoint only runs
    in the controller pod (which has no Docker); local execution uses the separate
    ``vast exec local run`` path.
    """
    import argparse

    from robovast.common.common import load_config
    from robovast.common.config import validate_config

    parser = argparse.ArgumentParser(
        prog="python -m robovast.execution.controller",
        description="Run a robovast campaign controller (batch or search) in-cluster.",
    )
    parser.add_argument("--vast", required=True, help="Path to the .vast campaign file.")
    parser.add_argument("--results-dir", required=True,
                        help="Directory where the campaign (results + campaign.db) is written.")
    parser.add_argument("--runs", type=int, default=None,
                        help="Override the number of runs from the config.")
    parser.add_argument("--namespace", default=os.environ.get("ROBOVAST_NAMESPACE", "default"),
                        help="Kubernetes namespace for the jobs.")
    parser.add_argument("--kube-context", default=os.environ.get("ROBOVAST_KUBE_CONTEXT"),
                        help="Host context name, used only to resolve per-cluster resources.")
    parser.add_argument("--config", default=None,
                        help="Batch mode only: run configurations matching this glob.")
    parser.add_argument("--campaign-id", default=None,
                        help="Campaign id to use (host-generated so it matches the "
                             "controller pod label). Defaults to a fresh timestamped id.")
    parser.add_argument("--log-tree", action="store_true", help="Forward the live scenario tree.")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    campaign_config = validate_config(load_config(args.vast))
    backend = _build_cluster_backend(args.namespace, args.kube_context, args.log_tree)
    # Variations that declare an auxiliary container are served by a sidecar in
    # this controller pod (injected by the host launcher); route their commands
    # through pods/exec instead of the local ``docker run`` default.
    from robovast.common.config_generation import set_container_runner_factory
    from robovast.execution.cluster_execution.container_runner import \
        make_cluster_container_runner_factory
    set_container_runner_factory(make_cluster_container_runner_factory(args.namespace))
    options = RunOptions(log_tree=args.log_tree)
    # The host launcher passes --campaign-id; resolve it here too so we know which
    # campaign to upload after the run (and so the id is stable for both paths).
    campaign_id = args.campaign_id or campaign_id_for(campaign_config)

    # ntfy push notifications (no-op unless ROBOVAST_NTFY_TOPIC is set). Bound to
    # this campaign id so concurrent controller pods report independently.
    notifier = Notifier.from_env(campaign_id)

    # Start the in-pod control channel (state + RPC) so the host can monitor loop
    # progress and issue commands. Best-effort: a failure leaves the campaign
    # running with the monitor falling back to its Kubernetes-only view.
    state = None
    try:
        from robovast.execution.control_server import ControllerState, serve_in_thread
        port = int(os.environ.get("ROBOVAST_CONTROL_PORT", "0")) or None
        state = ControllerState()
        serve_in_thread(state, **({"port": port} if port else {}))
    except Exception:  # pylint: disable=broad-except
        logger.warning("Could not start the control channel; continuing without it.",
                       exc_info=True)
        state = None

    # Pre-flight: verify the share credentials work *before* burning compute on a
    # campaign that could never be delivered. The launcher injects the share env
    # (ROBOVAST_SHARE_TYPE + provider vars) from the host .env.
    from robovast.execution.cluster_execution import \
        in_pod_upload  # pylint: disable=import-outside-toplevel
    try:
        share_provider = in_pod_upload.load_provider_from_env()
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Share provider misconfigured: %s", exc)
        if state is not None:
            state.set_phase("failed", stage="share-config-error")
        notifier.failed(f"share provider misconfigured: {exc}")
        sys.exit(2)
    if share_provider is None:
        # The launcher refuses to start a run without a share destination; guard
        # here too for standalone/manual invocations.
        logger.error("No share destination configured (ROBOVAST_SHARE_TYPE unset); "
                     "refusing to run a campaign whose results have nowhere to go.")
        if state is not None:
            state.set_phase("failed", stage="share-config-error")
        notifier.failed("no share destination configured (ROBOVAST_SHARE_TYPE unset)")
        sys.exit(2)
    try:
        in_pod_upload.verify_share_access(share_provider)
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Pre-flight share credential check failed; aborting "
                     "before starting any batches: %s", exc)
        if state is not None:
            state.set_phase("failed", stage="share-verify-failed")
        notifier.failed(f"pre-flight share credential check failed: {exc}")
        sys.exit(3)

    mode = "search" if campaign_config.search is not None else "batch"
    notifier.started(mode)
    if campaign_config.search is not None:
        report = run_search_campaign(args.vast, campaign_config, args.results_dir, args.runs,
                                     backend=backend, options=options,
                                     campaign_id=campaign_id, state=state, notifier=notifier)
        logger.info("Search campaign finished: %s", report)
    else:
        report = run_batch_campaign(args.vast, campaign_config, args.results_dir, args.runs,
                                    config_filter=args.config, backend=backend, options=options,
                                    campaign_id=campaign_id, state=state, notifier=notifier)
        logger.info("Batch campaign finished: %s", report)
    notifier.finished(f"{mode} campaign complete.")

    # The campaign is now published to storage. Deliver it to the share; stay
    # alive for a manual retrigger if the upload fails.
    sys.exit(_upload_to_share_with_retrigger(
        backend.cluster_config, campaign_id, share_provider, state, notifier))


if __name__ == "__main__":
    main()
