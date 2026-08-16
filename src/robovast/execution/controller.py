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

One controller drives **both** batch and search through one
:class:`~robovast.execution.backends.ExecutionBackend`, producing a single
uniform layout (``<results>/<CAMPAIGN_ID>/<config>/<run>/``) plus a live
``campaign.db`` for every run:

* **batch mode** (no ``search:`` block) — a strategy-less campaign with exactly
  one *batch* of the enumerated configurations.
* **search mode** — the strategy proposes batches; each batch is composed,
  executed, scored (Extractor) and fed back via ``tell``.

A campaign runs one or more *batches*; the batch is a logical grouping recorded
in the store, not a directory level, so batch and search share the flat layout.

**Where this runs:** always *in the driving process*, against the backend for
that deployment — the ``vast`` CLI with a ``DockerBackend`` locally, or the
``robovast-service`` with a ``KubernetesBackend`` for cluster campaigns (one
worker thread per campaign). This module is a **library**, not an entrypoint:
the per-campaign controller *pod* (and its in-pod ``main()`` / control server)
is gone, so cluster and local now share the same driver-hosting shape.
"""

import logging
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from robovast.common.campaign_data import (aggregate_run_status, list_run_dirs,
                                           read_execution_metadata,
                                           read_run_outcomes)
from robovast.common.config import declared_per_run_seconds
from robovast.common.logging_config import (add_campaign_log_handler,
                                            remove_campaign_log_handler)
from robovast.common.store import STORE_FILENAME, CampaignStore

from .backends import (CampaignConfigError, CampaignStopped, DockerBackend,
                       ExecutionBackend, RunOptions)
from .control_server import Phase, failure_detail, is_terminal
from .notify import Notifier

# Use the qualified name rather than __name__ so this module's records always
# propagate to the "robovast" logger that add_campaign_log_handler attaches
# controller.log to — otherwise the controller's own lines (banners, progress)
# would be dropped from the file while still reaching stderr.
logger = logging.getLogger("robovast.execution.controller")

# Composition progress is routed here (a child of "robovast", so it propagates to
# the variation.log handler) instead of the module logger's DEBUG default, so the
# variation phase's narrative — and the isolated-plugin subprocess output it
# forwards — is captured into _execution/variation.log at INFO.
variation_logger = logging.getLogger("robovast.variation")

_BAR = "=" * 60


#: Serialises id minting and remembers the last id handed out, so back-to-back
#: launches of the *same* campaign name never collide (see ``campaign_id_for``).
_campaign_id_lock = threading.Lock()
_last_campaign_id: str | None = None


def _sanitise_campaign_name(name: str) -> str:
    """Bucket/dir-safe slug for a user-supplied campaign name.

    Storage bucket names disallow underscores and other punctuation, so an
    override coming from a human (UI field, ``--campaign-name``, MCP arg) is
    normalised to ``[A-Za-z0-9-]`` and collapsed — keeping local and cluster ids
    identical. Falls back to ``campaign`` if nothing usable remains.
    """
    slug = re.sub(r"[^A-Za-z0-9-]+", "-", name).strip("-")
    return slug or "campaign"


def campaign_id_for(campaign_config, name_override: str | None = None) -> str:
    """``<name>-<timestamp>`` — the campaign directory id (both modes).

    ``name`` is ``name_override`` when a caller supplies one (the UI/CLI/MCP
    "campaign name" knob), otherwise ``metadata.name``. Underscores in the
    latter are normalised to hyphens so a local campaign id matches the
    cluster's: storage bucket names disallow underscores, so the cluster
    sanitises the name to hyphens — doing it here keeps both identical. A
    human-supplied override goes through the stricter ``_sanitise_campaign_name``.
    """
    global _last_campaign_id
    if name_override and name_override.strip():
        name = _sanitise_campaign_name(name_override)
    else:
        name = (campaign_config.metadata or {}).get("name", "campaign").replace("_", "-")
    # Hundredths of a second so two launches within the same second get distinct
    # ids (``is_campaign_dir`` accepts 6-8 trailing digits, and HHMMSS + 2 fills
    # them). That resolution alone is still only 10 ms, so a control plane firing
    # campaigns back-to-back can land two in the same tick; hold a lock and, on an
    # exact repeat, wait out the tick — guaranteeing distinct ids (hence distinct
    # campaign directories) without ever exceeding the 8-digit suffix.
    with _campaign_id_lock:
        while True:
            now = datetime.now()
            cid = f"{name}-{now.strftime('%Y-%m-%d-%H%M%S')}{now.microsecond // 10000:02d}"
            if cid != _last_campaign_id:
                _last_campaign_id = cid
                return cid
            time.sleep(0.005)


class CampaignController:
    """Drives a campaign (batch or search) to completion over one backend."""

    def __init__(self, *, campaign_id, results_dir, runs, backend: ExecutionBackend,
                 options: RunOptions, store: CampaignStore, campaign_config_dump: dict,
                 vast_dir: str, strategy=None, evaluator=None, compose=None,
                 per_batch: int = 1, postprocessing=None, batch_campaign_data=None,
                 stop_conditions=None, state=None, notifier=None, description="",
                 created_by=""):
        self.campaign_id = campaign_id
        self.campaign_root = os.path.join(results_dir, campaign_id)
        self.runs = runs
        self.backend = backend
        self.options = options
        self.store = store
        self.campaign_config_dump = campaign_config_dump
        self.vast_dir = vast_dir
        # Free text describing this launch; recorded on the campaign row so it stays
        # with the results (empty when the launcher gave none).
        self.description = description
        #: Who says they launched this. Self-declared; see CampaignStore.create_campaign.
        self.created_by = created_by
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
            config_dir="_config", description=self.description,
            created_by=self.created_by)
        if self.state is not None:
            self.state.update(mode=self.mode, campaign_id=self.campaign_id,
                              progress_deadline_s=self._progress_deadline())
            self.state.set_phase(Phase.RUNNING)
        self._start_progress_poller()
        self.notifier.start_heartbeat(status_fn=self._notify_status)
        self.notifier.started(self.mode)
        run_started = time.monotonic()
        try:
            if self.strategy is None:
                result = self._run_batch_mode(campaign_id)
            else:
                result = self._run_search(campaign_id)
            # Not FINISHED: share and postprocessing still have to run in the builders'
            # finally, and a campaign that reports terminal before its metrics exist
            # sends every reader — the waiter, the webui, the phone — away with an
            # answer that is wrong for as long as those steps take. `end_campaign`
            # publishes the terminal phase once, from whichever scope is outermost.
            if self.state is not None:
                self.state.set_phase(Phase.FINISHING)
            return result
        except CampaignStopped:
            # A clean cooperative stop (Ctrl+C / Stop button / MCP stop).
            if self.state is not None:
                self.state.set_phase(Phase.STOPPED)
            logger.info("Campaign %s stopped by request.", self.campaign_id)
            raise
        except BaseException as exc:
            # A stop can also surface as an arbitrary error — e.g. the storage tunnel
            # dies on Ctrl+C mid-operation. If a stop was requested, that is the clean
            # reason: report "stopped", not a failure with a misleading traceback.
            if self.state is not None and self.state.stop_requested:
                self.state.set_phase(Phase.STOPPED)
                logger.info("Campaign %s stopped by request.", self.campaign_id)
                raise CampaignStopped(str(exc)) from None
            if self.state is not None:
                self.state.set_phase(Phase.FAILED)
            # Not notified here: `end_campaign` sends the campaign's one notification,
            # so a failure *before* the controller ever ran (an image build that could
            # not resolve) is announced by the same path as one inside it. Notifying
            # from both left build failures silent and run failures double-reported.
            raise
        finally:
            self._record_execution_provenance(campaign_id, time.monotonic() - run_started)
            self._stop_progress_poller()
            # The heartbeat deliberately outlives run(): share and postprocessing are
            # the longest stretch of a campaign in which nothing else reports, and
            # stopping it here left exactly that window silent. `end_campaign` stops it.
            remove_campaign_log_handler(log_handler)

    def _progress_deadline(self) -> int | None:
        """How long this campaign's progress may legitimately stand still, in seconds.

        The **declared** per-run budget scaled by ``runs_per_job``: packed runs may
        publish their results in one burst per job, so the unpacked figure would accuse
        a healthy packed campaign of stalling. Published on the status because only the
        controller can see the ``.vast``; readers just compare against it.

        ``None`` when the ``.vast`` declares no ``execution.timeout`` — the cluster's
        force-kill backstop is deliberately *not* substituted here. It exists so a run
        cannot hang forever, which is a fine reason to kill at one hour and a terrible
        reason to call a two-minute pilot healthy for the first fifty-nine.
        """
        execution = (self.campaign_config_dump or {}).get("execution") or {}
        declared = declared_per_run_seconds(execution)
        if declared is None:
            return None
        return declared * int(execution.get("runs_per_job") or 1)

    def _record_execution_provenance(self, campaign_id: int, elapsed_s: float) -> None:
        """Lift ``_execution/execution.yaml`` onto the campaign row, and stamp elapsed.

        In the ``finally`` of :meth:`run` deliberately, for two reasons. It is the one
        place **both** modes converge — batch mode has no ``record_outcome``, so before
        this its campaign row was never written again after creation, leaving
        ``elapsed_s`` NULL and the provenance unqueryable for every batch campaign. And a
        campaign that was stopped or crashed still ran on some image for some time; that
        is more worth recording than less.

        The file is produced by the backend during execution (on the local lane by a
        generated shell script inside the run), so it cannot exist at campaign creation
        and may legitimately be absent here — a campaign that died before execution
        started. Best-effort throughout: this is bookkeeping and must never convert a
        finished campaign into a failed one.
        """
        try:
            self.store.record_elapsed(campaign_id, elapsed_s)
        except Exception:  # pylint: disable=broad-except
            logger.debug("Could not record elapsed time.", exc_info=True)
        try:
            self.store.record_execution(
                campaign_id, read_execution_metadata(Path(self.campaign_root)))
        except FileNotFoundError:
            logger.debug("No execution.yaml yet; provenance not recorded.")
        except Exception:  # pylint: disable=broad-except
            logger.debug("Could not record execution provenance.", exc_info=True)

    # -- run-level progress poller ------------------------------------------

    _POLL_INTERVAL = 3.0

    def _start_progress_poller(self) -> None:
        """Start a daemon thread that publishes current-batch run progress.

        Skipped when there is no control channel, or the backend cannot count finished
        runs. ``backend.run_batch`` blocks for a whole batch, so this runs the count
        concurrently.

        A backend that cannot count is **logged**, not passed over silently: without
        the poller the campaign publishes a ``progress`` that never advances, which is
        exactly what a hung run looks like, so the reason has to be on the record.
        """
        if self.state is None:
            return
        try:
            probe = self.backend.count_run_artifacts(self.campaign_id, self.campaign_root)
        except Exception:  # pylint: disable=broad-except
            logger.warning("Backend %s could not count run artifacts; run-level "
                           "progress is disabled for this campaign.",
                           type(self.backend).__name__, exc_info=True)
            return
        if probe is None:
            logger.warning("Backend %s does not report finished runs; this campaign's "
                           "progress will stay at 0 until each batch completes.",
                           type(self.backend).__name__)
            return

        def _poll() -> None:
            while not self._poller_stop.is_set():
                if self._batch_active.is_set() and not self.state.progress_suspended:
                    try:
                        done = self.backend.count_run_artifacts(
                            self.campaign_id, self.campaign_root)
                        if done is not None:
                            completed = min(max(0, done - self._batch_baseline), self._batch_total)
                            self.state.update_runs(
                                completed=completed, total=self._batch_total)
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
            self._batch_baseline = self.backend.count_run_artifacts(
                self.campaign_id, self.campaign_root) or 0
        except Exception:  # pylint: disable=broad-except
            self._batch_baseline = 0
        self._batch_total = total
        # A new batch resets every counter, so this one replaces the sub-model rather
        # than merging: a previous batch's failures must not carry into this one.
        self.state.update(runs={"completed": 0, "total": total, "no_result": 0,
                                "failed": 0})
        self._batch_active.set()

    def _end_batch_progress(self) -> None:
        if self.state is None or self._poller is None:
            return
        self._batch_active.clear()
        # The batch's jobs have all reached a terminal state. Any expected run that
        # never produced a result artifact by now has failed, so report the real
        # completed count and the failed remainder — rather than optimistically
        # claiming completed == total, which would hide partial-batch failures.
        try:
            done = self.backend.count_run_artifacts(
                self.campaign_id, self.campaign_root)
        except Exception:  # pylint: disable=broad-except
            done = None
        if done is None:
            completed = self._batch_total
        else:
            completed = min(max(0, done - self._batch_baseline), self._batch_total)
        no_result = max(0, self._batch_total - completed)
        if no_result:
            logger.warning("Batch complete: %d/%d run(s) produced results; %d produced "
                           "none.", completed, self._batch_total, no_result)
        self.state.update_runs(completed=completed, total=self._batch_total,
                               no_result=no_result)

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
                batch_tag="batch-0", runs=self.runs, options=self.options,
                whole_campaign=True)
        finally:
            self._end_batch_progress()

        failed_runs = 0
        killed_runs = 0
        for cfg in configs:
            name = cfg["name"]
            cdir = os.path.join(self.campaign_root, name)
            run_dirs = list_run_dirs(cdir)
            unit_id = self.store.record_unit(
                batch_id=batch_id, paramset_id=name, config_name=name,
                params=cfg.get("config", {}) or {}, objectives={}, measures={},
                n_samples=len(run_dirs), status=aggregate_run_status(run_dirs),
                result_dir=os.path.relpath(cdir, self.campaign_root))
            outcomes = read_run_outcomes(Path(cdir), Path(self.campaign_root))
            self.store.record_runs(unit_id, outcomes)
            # The verdicts are parsed here anyway for the store; tallying them into the
            # live state is what makes a failing trial visible to a status poll. Without
            # it the only failure count was "produced no result", which a scenario that
            # ran and failed does not trip — so the campaign reported itself clean.
            cfg_failed, cfg_killed = _tally_outcomes(outcomes)
            failed_runs += cfg_failed
            killed_runs += cfg_killed
        if self.state is not None:
            if failed_runs:
                logger.warning("Batch complete: %d run(s) did not pass.", failed_runs)
            if killed_runs:
                logger.warning("Batch complete: %d run(s) stopped manually.", killed_runs)
            self.state.update_runs(failed=failed_runs, killed=killed_runs)
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
                    self.state.set_phase(Phase.FINISHING)
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

    @contextmanager
    def _variation_log(self):
        """Route composition output to the campaign's ``variation.log`` phase file.

        Appends, so every batch's composition accumulates into the one VARIATION
        phase. Never fails the campaign over its own logging: a handler that cannot
        be opened is warned about and the composition proceeds unlogged.
        """
        handler = None
        try:
            handler = add_campaign_log_handler(
                os.path.join(self.campaign_root, "_execution", "variation.log"))
        except Exception:  # pylint: disable=broad-except
            logger.warning("Could not open variation.log; continuing without it.",
                           exc_info=True)
        try:
            yield
        finally:
            if handler is not None:
                remove_campaign_log_handler(handler)

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
        failed_runs = 0
        killed_runs = 0
        try:
            for reps, group in sorted(groups.items()):
                tag = f"batch-{batch_idx}" + (f"/reps-{reps}" if multi else "")
                # Compose into a temp dir (intermediate config artifacts); the backend
                # stages from it and only results land under the campaign root.
                with tempfile.TemporaryDirectory(prefix="robovast_compose_") as artifacts:
                    # Composition happens once per batch here, rather than once up front
                    # as in batch mode -- but it is the same phase, and it is where an
                    # unrealizable draw is reported. Without this handler that narrative
                    # lands in controller.log among the run output, and get_campaign_log's
                    # VARIATION phase (documented as where a campaign that failed before
                    # it ever ran explains itself) never appears for a search at all.
                    with self._variation_log():
                        campaign_data, name_by_id = self.compose.compose(group, artifacts)
                    self.backend.run_batch(
                        campaign_data, campaign_root=self.campaign_root, batch_tag=tag,
                        runs=reps, options=self.options)
                self._run_postprocessing()

                for ps in group:
                    config_name = name_by_id.get(ps.id)
                    if config_name is None:
                        # Composition itself failed for this param set (see
                        # Compose._resolve_names) -- no config_dir was ever produced, so
                        # there is nothing to evaluate or run. Record it for visibility
                        # and leave it out of `evaluations`: every strategy this campaign
                        # can use tolerates a shorter list than it asked for.
                        self.store.record_unit(
                            batch_id=batch_id, paramset_id=ps.id, config_name="",
                            params=ps.values, objectives={}, measures={},
                            n_samples=0, status="composition_failed", result_dir="")
                        continue
                    config_dir = Path(self.campaign_root) / config_name
                    ev = self.evaluator.evaluate(config_dir, ps)
                    evaluations.append(ev)
                    unit_id = self.store.record_unit(
                        batch_id=batch_id, paramset_id=ps.id, config_name=config_name,
                        params=ps.values, objectives=ev.objectives, measures=ev.measures,
                        n_samples=ev.n_samples, status="evaluated",
                        result_dir=os.path.relpath(config_dir, self.campaign_root))
                    outcomes = read_run_outcomes(config_dir, Path(self.campaign_root))
                    self.store.record_runs(unit_id, outcomes)
                    cfg_failed, cfg_killed = _tally_outcomes(outcomes)
                    failed_runs += cfg_failed
                    killed_runs += cfg_killed
        finally:
            # Same tally as batch mode: a trial that ran and failed is invisible in the
            # resultless count, so surface it before the batch's progress is closed out.
            if self.state is not None and (failed_runs or killed_runs):
                if failed_runs:
                    logger.warning("Batch %d: %d run(s) did not pass.",
                                   batch_idx, failed_runs)
                if killed_runs:
                    logger.warning("Batch %d: %d run(s) stopped manually.",
                                   batch_idx, killed_runs)
                self.state.update_runs(failed=failed_runs, killed=killed_runs)
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
        # Make the workspace's `plugins:` importable here: compose staged them into
        # <vast_dir>/.robovast_plugins/ but only led sys.path in its *subprocess*, so
        # this controller process needs them prepended before resolving search
        # postprocessing plugins (and their deps). Same helper the analysis path uses.
        from robovast.common.config_plugins import ensure_postprocessing_plugins
        ensure_postprocessing_plugins(self.vast_dir)
        # Imported lazily to avoid importing the results_processing stack (and its
        # heavier deps) unless a search actually configures postprocessing.
        from robovast.results_processing.postprocessing import \
            run_postprocessing_commands
        run_postprocessing_commands(
            self.postprocessing, results_dir=self.campaign_root,
            config_dir=self.vast_dir, output=logger.info)


# -- builders ---------------------------------------------------------------

def _chain_postprocessing(backend: ExecutionBackend, campaign_root: str,
                          campaign_id: str, state=None,
                          options: "RunOptions | None" = None) -> None:
    """Run analysis postprocessing in-cluster, when the caller asked for it.

    Called from the builders' ``finally`` **after the store is closed** (so
    ``campaign.db`` is flushed — ``generate_data_db``'s ``runs`` table reads it) and
    **before** :func:`_finalize`, so the resulting ``data.db``/CSVs ride the existing
    campaign upload instead of needing one of their own.

    Opt-in via ``RunOptions.postprocess`` (set by ``create_campaign(postprocess=True)``)
    and a no-op otherwise. This is an **option, not an env var**, because the service
    drives many campaigns concurrently in one process — a process-global env could not
    tell them apart. Best-effort: a failure is surfaced on the status channel but never
    loses the campaign's results.
    """
    options = options or RunOptions()
    if not options.postprocess:
        return
    cluster_config = getattr(backend, "cluster_config", None)
    if cluster_config is None:  # local backend — the in-process chain handles it
        return
    try:
        from robovast.execution.cluster_execution.postprocess_job import \
            postprocess_campaign
        if state is not None:
            state.set_phase(Phase.POSTPROCESSING)
        ok, message = postprocess_campaign(
            cluster_config, campaign_id, campaign_root,
            options.namespace or os.environ.get("ROBOVAST_NAMESPACE", "default"),
            # The context this backend submitted the campaign's Jobs with; postprocessing
            # must schedule against the same cluster the runs went to.
            kube_context=getattr(backend, "kube_context", None),
        )
        logger.info("Analysis postprocessing: %s", message)
        if state is not None:
            if ok:
                # Success must transition off "postprocessing"; otherwise the
                # phase (and the webui monitor) stays stuck there — run() set
                # "finished" before this finally-block chained postprocessing,
                # and only the failure branch below moved it since. Mirrors the
                # local path in ClusterService._postprocess.
                from robovast.results_processing.postprocessing import \
                    campaign_defines_postprocessing
                if campaign_defines_postprocessing(campaign_root):
                    state.update(postprocessed=True)
                state.update(postprocessing_error=None)
                state.set_phase(Phase.FINISHED)
            else:
                # The runs finished — postprocessing is a separate step, so a
                # postprocessing failure keeps ``phase == finished`` (the runs are
                # the deliverable) and records the reason on its own field, distinct
                # from a run failure (``phase == failed``). Re-run corrects it.
                state.update(postprocessing_error=message, postprocessed=False)
                state.set_phase(Phase.FINISHED, stage=f"postprocessing failed: {message}")
            # The durable outcome record is written once by _finish_campaign after
            # both share and postprocessing have run, so a single outcome.json carries
            # phase=finished + share_error + postprocessing_error (and covers the
            # postproc-off / share-only cases too).
    except Exception as e:  # pylint: disable=broad-except
        # A clean failure (an unreachable cluster, a bad config) states its cause and
        # its remedy in the message; the traceback would only bury it. Genuine bugs
        # still carry one — that is what ``include_traceback`` distinguishes.
        logger.warning("Analysis postprocessing failed: %s", e,
                       exc_info=getattr(e, "include_traceback", True))
        if state is not None:
            state.update(postprocessing_error=failure_detail(e), postprocessed=False)
            state.set_phase(Phase.FINISHED, stage="postprocessing failed")


def _finalize(backend: ExecutionBackend, campaign_root: str) -> None:
    """Run the backend's campaign finalize hook, best-effort.

    Called from the builders' ``finally`` after the store is closed (so
    ``campaign.db`` is fully flushed). Never masks an in-flight exception.
    """
    try:
        backend.finalize_campaign(campaign_root)
    except Exception:  # pylint: disable=broad-except
        logger.warning("Campaign finalize hook failed", exc_info=True)


def _tally_outcomes(outcomes) -> tuple[int, int]:
    """``(failed, killed)`` over one config's run outcomes.

    One definition, shared by batch and search mode, so the two cannot come to disagree
    about what counts as a failure. ``killed`` — a job an operator stopped by hand — is
    counted apart and deliberately kept **out** of ``failed``: it is not a verdict about
    the system under test, so folding it in would report a human intervention as a trial
    failure in the live status, the notification, and every reader downstream of them.
    """
    killed = sum(1 for o in outcomes if o.get("status") == "killed")
    failed = sum(1 for o in outcomes if o.get("status") not in ("passed", "killed"))
    return failed, killed


def outcome_summary(snap) -> tuple[str, bool]:
    """One line describing what a finished campaign actually produced, and whether it
    is degraded.

    Built here rather than at each reader because "did this campaign succeed?" cannot
    be answered from ``phase`` alone — a campaign whose trials all passed but whose
    postprocessing failed stays ``finished`` with no CSVs and no ``data.db``. Every
    channel that used to answer from ``phase`` reported that as a clean success.
    """
    runs = snap.runs
    parts = [f"{runs.completed}/{runs.total} runs"]
    if runs.failed:
        parts.append(f"{runs.failed} failed trial(s)")
    # A killed run delivered nothing, so it is already inside ``no_result``. Naming both
    # in full would report the same run twice, in two different vocabularies — so the
    # resultless count is the ones nobody chose to end, and the kills are named as such.
    if runs.killed:
        parts.append(f"{runs.killed} stopped manually")
    unexplained = max(0, runs.no_result - runs.killed)
    if unexplained:
        parts.append(f"{unexplained} without result")
    # A manual kill still degrades the campaign: it delivered fewer usable runs than it
    # was asked for, and whoever reads this later is not necessarily whoever killed it.
    degraded = bool(runs.failed or runs.no_result)
    if snap.postprocessing_error:
        parts.append(f"POSTPROCESSING FAILED ({snap.postprocessing_error}) — "
                     "no CSVs or data.db")
        degraded = True
    elif snap.postprocessed:
        parts.append("postprocessed")
    if snap.share_error:
        parts.append(f"upload-to-share failed ({snap.share_error})")
        degraded = True
    return ", ".join(parts), degraded


def publish_terminal_phase(state) -> None:
    """Mark the campaign over — once.

    Idempotent by the terminal test, because the stop and failure paths and
    ``_chain_postprocessing``'s failure branches already published one, each carrying a
    ``stage`` string that a blanket re-set would wipe.
    """
    if state is not None and not is_terminal(state.snapshot().phase):
        state.set_phase(Phase.FINISHED)


def end_campaign(campaign_id: str, state, notifier=None) -> None:
    """End a campaign exactly once: terminal phase, heartbeat off, one notification.

    Called by whichever scope is **outermost** for the lane, and only by it — see
    ``RunOptions.finalize_phase``. ``run()`` no longer publishes ``finished`` when it
    returns, because share and postprocessing still have to happen; the campaign is over
    when this runs and not before.

    Callers run it from a ``finally``: a campaign left non-terminal would block every
    waiter until its timeout, which is a worse failure than the early ``finished`` this
    seam replaces.
    """
    publish_terminal_phase(state)
    if notifier is None:
        return
    notifier.stop_heartbeat()
    if state is None:
        return
    snap = state.snapshot()
    if snap.phase == Phase.STOPPED:
        notifier.stopped(outcome_summary(snap)[0])
    elif snap.phase in (Phase.FAILED, Phase.CRASHED):
        # The recorded error, not an exception in scope here: this path also carries
        # failures that happened *before* the controller ran at all — an image build
        # that could not resolve — which previously went unannounced entirely.
        notifier.failed(snap.error or snap.stage or "no reason recorded")
    else:
        summary, degraded = outcome_summary(snap)
        notifier.finished(summary, degraded=degraded)


def _finish_campaign(backend: ExecutionBackend, campaign_root: str, campaign_id: str,
                     state, options: "RunOptions | None", notifier=None) -> None:
    """The builders' ``finally`` tail: chain postprocessing, finalize-upload, then end.

    Postprocessing and the finalize upload are skipped when a cooperative **stop** was
    requested: on Ctrl+C the cluster storage tunnel is torn down with the process
    group, so a download/postprocess/upload here would only fail noisily against a dead
    endpoint. A stopped campaign's per-run results were already uploaded by its jobs, so
    there is nothing to salvage. The campaign is still *ended* — a stop that reported
    nothing was indistinguishable from a campaign still running.

    ``end_campaign`` runs from a ``finally`` and only when this tail is the campaign's
    outermost scope (``RunOptions.finalize_phase``). The local service sets that false
    and ends the campaign itself, after the postprocessing it runs once this returns.
    """
    options = options or RunOptions()
    try:
        if state is not None and state.stop_requested:
            logger.info(
                "Campaign %s stopped — skipping postprocessing and finalize upload.",
                campaign_id)
            return
        if options.upload_to_share:
            _share_campaign(backend, campaign_root, options, state, notifier)
        # A failed campaign (run() set Phase.FAILED before re-raising into this finally)
        # never finished projecting its results, so campaign_root is missing pieces
        # postprocessing needs — e.g. _config/*.vast. Running it anyway only raises a
        # second, misleading error ("no .vast under _config") that masks the real failure.
        # Skip only the derived-data step; _finalize still runs below so the failure
        # outcome is published (as does _record_campaign_failure).
        if state is not None and state.snapshot().phase == Phase.FAILED:
            logger.info("Campaign %s failed — skipping analysis postprocessing.",
                        campaign_id)
        else:
            _chain_postprocessing(backend, campaign_root, campaign_id, state, options)
            # End the campaign *before* recording, so the record carries its final
            # phase — but only when this tail owns the ending. When it does not (the
            # local service runs postprocessing after this returns), the record is
            # deliberately written non-terminal: a campaign that is not over must not
            # leave behind a record saying it is, and whoever does end it rewrites this.
            if options.finalize_phase:
                publish_terminal_phase(state)
            # Persist the terminal outcome once, after both share and postprocessing, so
            # a single _execution/outcome.json carries phase=finished + share_error +
            # postprocessing_error. Without this a cleanly-finished (or finished-but-a-
            # post-step-failed) cluster campaign has no durable record and a stateless
            # service reconstructs it as unknown after the driver is gone. A run failure
            # (Phase.FAILED, handled above) is recorded by _record_campaign_failure.
            if state is not None:
                _record_controller_outcome(campaign_root, campaign_id, state, backend)
        _finalize(backend, campaign_root)
    finally:
        if options.finalize_phase:
            end_campaign(campaign_id, state, notifier)


def _share_campaign(backend: ExecutionBackend, campaign_root: str,
                    options: "RunOptions", state, notifier=None) -> None:
    """Produce the raw upload-to-share artifact, **before** postprocessing.

    Runs the backend's ``share_campaign`` hook (local: tar.gz on disk; cluster:
    streamed to the share provider) so the shared archive is the minimal, untouched
    campaign — postprocessing only *adds* derived data, which stays out of the share.
    Best-effort: a share failure is logged but never loses the campaign nor blocks
    postprocessing/finalize.
    """
    try:
        if state is not None:
            state.set_phase(Phase.SHARING)
        backend.share_campaign(campaign_root, options)
    except Exception as e:  # pylint: disable=broad-except
        logger.warning("Upload-to-share failed; continuing with the campaign.",
                       exc_info=True)
        # Record the reason on its own field (not swallowed) so it survives to the
        # durable outcome _finish_campaign writes and can be re-triggered from disk
        # (service run_share). The phase is left for postprocessing/finalize to set —
        # a share failure keeps the campaign finished, it is not a run failure.
        if state is not None:
            state.update(share_error=failure_detail(e))
        return
    if state is not None:
        state.update(share_error=None)
    if notifier is not None:
        # The resolved provider isn't in scope here; the configured share type is what
        # the service was handed via env (ROBOVAST_SHARE_TYPE), matching the old
        # controller's ``provider.SHARE_TYPE``.
        notifier.uploaded(os.environ.get("ROBOVAST_SHARE_TYPE") or "share")

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


def _install_plugins(vast_file, campaign_config, campaign_root: str, state) -> None:
    """Install the campaign's declared ``plugins:`` as a distinct, logged phase.

    A no-op when the campaign declares no plugins (its phase flow is unchanged).
    Otherwise the phase advances to ``plugin install`` and pip's output streams live into
    ``<campaign>/_execution/plugin_install.log`` — served under the campaign log's PLUGIN
    INSTALL divider, the same per-phase pattern as ``variation``/``postprocessing`` — so
    the install is observable rather than a blank "starting", and its log rides the
    campaign into the object store / a re-run like every other phase file.

    Materialize-only (``add_to_path=False``): the packages land in
    ``.robovast_plugins/`` (and travel with the campaign to the pods and a re-run),
    while importing them onto ``sys.path`` stays in the isolated compose subprocess and
    never pollutes the long-lived service process. Composition later finds them already
    installed (marker hit) and only adjusts ``sys.path`` there.
    """
    specs = list(getattr(campaign_config, "plugins", None) or [])
    if not specs:
        return
    vast_dir = os.path.dirname(os.path.abspath(vast_file))
    if state is not None:
        state.set_phase(Phase.PLUGIN_INSTALL)
    handler = None
    try:
        handler = add_campaign_log_handler(
            os.path.join(campaign_root, "_execution", "plugin_install.log"))
    except Exception:  # pylint: disable=broad-except
        logger.warning("Could not open plugin_install.log; continuing without it.",
                       exc_info=True)
    try:
        from robovast.common.config_plugins import ensure_workspace_plugins
        ensure_workspace_plugins(vast_dir, specs, add_to_path=False)
    finally:
        remove_campaign_log_handler(handler)


def run_search_campaign(vast_file, campaign_config, results_dir, runs,
                        config_filter=None,
                        backend: ExecutionBackend | None = None,
                        options: RunOptions | None = None, campaign_id=None, state=None,
                        notifier=None, description="", created_by=""):
    """Build and run a search campaign. Requires ``campaign_config.search``.

    ``config_filter`` exists here only to be **refused**. A search names its
    configurations after parameter sets it has not drawn yet, so there is nothing
    for a glob to select — but the launch path used to accept the filter and
    silently drop it, which turned the documented "pilot one configuration before
    the full sweep" into a launch of the entire search budget. Failing is the
    point; ``pilot`` below is the affordance that actually works here.
    """
    from robovast.search.compose import Compose
    from robovast.search.evaluator import Evaluator
    from robovast.search.stopping import build_stop_conditions
    from robovast.search.strategy import build_strategy

    search_cfg = campaign_config.search
    if search_cfg is None:
        raise ValueError("run_search_campaign called without a 'search' block")
    if config_filter:
        raise CampaignConfigError(
            f"config_filter ({config_filter!r}) does not apply to a search campaign: "
            "its configurations are generated from parameter sets the strategy draws "
            "at run time, so there are no configuration names to match before it "
            "starts. To run a small pilot of a search, reduce 'search.per_batch' and "
            "'search.budget' in the .vast (e.g. per_batch: 1, budget: [{batches: 1}]).")

    vast_dir = os.path.dirname(os.path.abspath(vast_file))
    runs = runs if runs is not None else campaign_config.execution.runs
    campaign_id = campaign_id or campaign_id_for(campaign_config)
    be = backend or DockerBackend(state=state)
    opts = options or RunOptions()
    _preflight_upload_to_share(be, opts)
    # Install declared plugins first, as their own logged phase (no-op if none), before
    # the search loop composes its first batch.
    _install_plugins(vast_file, campaign_config,
                     os.path.join(results_dir, campaign_id), state)
    # One notifier drives the whole campaign: the controller fires the lifecycle
    # events, and _finish_campaign (outside the controller) fires `uploaded`.
    notifier = notifier or Notifier.from_env(campaign_id)
    store = CampaignStore(os.path.join(results_dir, campaign_id, STORE_FILENAME))
    controller = CampaignController(
        campaign_id=campaign_id, results_dir=results_dir, runs=runs,
        backend=be, options=opts,
        store=store, campaign_config_dump=campaign_config.model_dump(),
        vast_dir=vast_dir, strategy=build_strategy(search_cfg, vast_dir),
        evaluator=Evaluator(search_cfg, vast_dir), compose=Compose(vast_file),
        per_batch=search_cfg.per_batch, postprocessing=search_cfg.postprocessing,
        stop_conditions=build_stop_conditions(search_cfg), state=state, notifier=notifier,
        description=description, created_by=created_by)
    try:
        return controller.run()
    finally:
        store.close()
        _campaign_root = os.path.join(results_dir, campaign_id)
        # After store.close() (campaign.db flushed, which data.db's `runs` table
        # reads) and before _finalize, so data.db rides the existing upload.
        _finish_campaign(be, _campaign_root, campaign_id, state, opts, notifier)


def _record_controller_failure(campaign_root, campaign_id, state, exc, backend):
    """Durably record *why* the controller failed, then leave it queryable.

    Writes ``_execution/outcome.json`` (the terminal ``Status`` with ``error``) and
    uploads it — plus ``controller.log`` if present — to the object store, because
    the normal ``_finalize`` upload is skipped when the failure precedes it (e.g. an
    early config-expansion crash). Best-effort: recording a failure must never mask
    the original one.
    """
    from robovast.execution.control_server import ControllerState, failure_detail

    detail = failure_detail(exc)
    if state is None:
        state = ControllerState()
        state.update(campaign_id=campaign_id)
    state.update(error=detail)
    state.set_phase(Phase.FAILED)
    _record_controller_outcome(campaign_root, campaign_id, state, backend)


def _record_controller_outcome(campaign_root, campaign_id, state, backend):
    """Durably record the campaign's current terminal ``Status`` (outcome + upload).

    Writes ``_execution/outcome.json`` from the live ``state`` — whatever phase it
    holds (``failed`` for a crash, ``stopped`` for a cooperative stop) — and uploads
    the control-plane artifacts to the object store, so a **stateless service resolves
    the terminal state after the pod is gone** (a plain ``_finalize`` upload is skipped
    for both failures and stops). Best-effort: never masks the caller's flow.
    """
    from robovast.common import campaign_data

    try:
        campaign_data.write_execution_outcome(campaign_root, state.snapshot())
    except Exception:  # pylint: disable=broad-except
        logger.warning("Could not write outcome.json for %s", campaign_id, exc_info=True)
        return

    # Upload just the control-plane artifacts (outcome + log) to the object store,
    # so the stateless service resolves the reason after the pod is gone.
    cfg = getattr(backend, "cluster_config", None)
    if cfg is None:
        return  # local lane: the artifacts are already on the disk the caller reads
    try:
        # After the guard, not before it. The upload is a cluster-lane concern, and
        # importing it first meant every *local* teardown loaded cluster code to
        # discover it had nothing to do -- which, once that code ships separately,
        # becomes an ImportError caught below and logged as a failed upload that was
        # never going to happen.
        from robovast.execution.cluster_execution import in_pod_storage
        storage = in_pod_storage.storage_client_for(cfg)
        bucket, prefix = in_pod_storage.campaign_storage_location(cfg, campaign_id)
        exec_dir = os.path.join(campaign_root, "_execution")
        # variation.log included: an early config-expansion crash happens before
        # _finalize's whole-root upload, so its log would otherwise be lost. build.log
        # for the same reason and more sharply: a campaign that died waiting for its
        # image never reaches _finalize at all, and the live build log dies with the
        # build Job at ttlSecondsAfterFinished — this copy is the only surviving record
        # of why the image never arrived.
        for name in ("outcome.json", "controller.log", "variation.log", "build.log"):
            path = os.path.join(exec_dir, name)
            if os.path.isfile(path):
                storage.upload_file(path, bucket, f"{prefix}_execution/{name}")
    except Exception as e:  # pylint: disable=broad-except
        # Concise (no traceback): on Ctrl+C the storage tunnel is already gone, so a
        # connection error here is expected and must not re-clutter the shutdown.
        logger.warning("Could not upload outcome record for %s: %s", campaign_id, e)


def filter_configs_by_name(configs, config_filter):
    """Select campaign configs whose expanded name matches ``config_filter``.

    Matching is a glob against the expanded variation name (e.g.
    ``config1-1-1-1``), so a bare config-block name like ``config1`` matches
    nothing — use ``config1*`` to select the whole block.

    Raises ``CampaignConfigError`` listing the available config names when nothing
    matches, so a typo is reported with actionable choices (and no stack trace).
    """
    import fnmatch

    matched = [c for c in configs if fnmatch.fnmatch(c["name"], config_filter)]
    if not matched:
        available = "\n".join(f"  - {c['name']}" for c in configs)
        raise CampaignConfigError(
            f"No configs matched pattern '{config_filter}'.\n"
            f"Available configs:\n{available}")
    return matched


def build_campaign_data(vast_file, output_dir, config_filter=None,
                        progress_update_callback=None):
    """Generate the batch campaign data and apply the optional ``--config`` filter.

    Shared by :func:`run_batch_campaign` and the host-side ``cluster run``
    pre-flight check so both select configs through exactly the same code path.
    Raises ``CampaignConfigError`` if the vast-file yields no configs or the filter
    matches none (the message lists the available config block names).

    *progress_update_callback* receives the composition narrative (and the
    isolated-plugin subprocess output it forwards); :func:`run_batch_campaign`
    routes it to ``variation.log`` while the host-side pre-flight leaves it ``None``.
    """
    from robovast.common.config_generation import generate_scenario_variations

    campaign_data, transient_files = generate_scenario_variations(
        variation_file=vast_file, progress_update_callback=progress_update_callback,
        output_dir=output_dir)
    if not campaign_data["configs"]:
        raise CampaignConfigError("No configs found in vast-file")
    if config_filter:
        campaign_data["configs"] = filter_configs_by_name(
            campaign_data["configs"], config_filter)
    return campaign_data, transient_files


def _preflight_upload_to_share(backend: ExecutionBackend, opts: RunOptions) -> None:
    """Reject a misconfigured ``--upload-to-share`` before the campaign starts.

    Run *before* the store/controller and the ``finally: _finish_campaign`` tail are
    set up, so a raise here is a clean pre-start failure — no half-built campaign, no
    finish-tail work on nothing — surfaced as ``CampaignConfigError`` to the caller.
    """
    if opts.upload_to_share:
        backend.preflight_upload_to_share()


def run_batch_campaign(vast_file, campaign_config, results_dir, runs, config_filter=None,
                       backend: ExecutionBackend | None = None,
                       options: RunOptions | None = None, campaign_id=None, state=None,
                       notifier=None, description="", created_by=""):
    """Build and run a batch campaign (no ``search:`` block)."""
    vast_dir = os.path.dirname(os.path.abspath(vast_file))
    runs = runs if runs is not None else campaign_config.execution.runs
    campaign_id = campaign_id or campaign_id_for(campaign_config)

    with tempfile.TemporaryDirectory(prefix="robovast_batch_") as tmp:
        # Capture the variation (config-generation) phase into its own phase file,
        # which the unified campaign log serves under the VARIATION divider. The
        # handler is thread-isolated (same worker thread as the composition), so
        # concurrent service campaigns keep separate variation.log files. It is
        # attached only around composition — the run phase writes controller.log.
        campaign_root = os.path.join(results_dir, campaign_id)
        # Install declared plugins first, as their own logged phase (no-op if none),
        # so the pip install is observable and its log lands beside the others.
        _install_plugins(vast_file, campaign_config, campaign_root, state)
        var_handler = None
        try:
            var_handler = add_campaign_log_handler(
                os.path.join(campaign_root, "_execution", "variation.log"))
        except Exception:  # pylint: disable=broad-except
            logger.warning("Could not open variation.log; continuing without it.",
                           exc_info=True)
        # Config-variation expansion is a distinct pre-run step (it can be slow for
        # a large campaign); surface it as its own phase so the campaign is not stuck
        # showing "starting" while it expands. run() advances to RUNNING once the
        # controller loop begins, so variation → running is automatic.
        if state is not None:
            state.set_phase(Phase.VARIATION)
        try:
            campaign_data, _ = build_campaign_data(
                vast_file, tmp, config_filter,
                progress_update_callback=variation_logger.info)
        finally:
            remove_campaign_log_handler(var_handler)

        be = backend or DockerBackend(state=state)
        opts = options or RunOptions()
        _preflight_upload_to_share(be, opts)
        # One notifier drives the whole campaign: the controller fires the lifecycle
        # events, and _finish_campaign (outside the controller) fires `uploaded`.
        notifier = notifier or Notifier.from_env(campaign_id)
        store = CampaignStore(os.path.join(results_dir, campaign_id, STORE_FILENAME))
        controller = CampaignController(
            campaign_id=campaign_id, results_dir=results_dir, runs=runs,
            backend=be, options=opts,
            store=store, campaign_config_dump=campaign_config.model_dump(),
            vast_dir=vast_dir, batch_campaign_data=campaign_data, state=state,
            notifier=notifier, description=description, created_by=created_by)
        try:
            return controller.run()
        finally:
            store.close()
            _campaign_root = os.path.join(results_dir, campaign_id)
            # After store.close() (campaign.db flushed, which data.db's `runs` table
            # reads) and before _finalize, so data.db rides the existing upload.
            _finish_campaign(be, _campaign_root, campaign_id, state, opts, notifier)
