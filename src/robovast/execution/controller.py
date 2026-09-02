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

from robovast.client.logging_config import add_campaign_log_handler, remove_campaign_log_handler
from robovast.common.campaign_data import (aggregate_run_status, invalid_runs,
                                           list_run_dirs, read_container_failures,
                                           read_execution_metadata, read_run_outcomes)
from robovast.common.config import declared_job_seconds
from robovast.common.store import STORE_FILENAME, CampaignStore
from robovast.search.extractor import NoSampleError

from .backends import (CampaignConfigError, CampaignStopped, DockerBackend, ExecutionBackend,
                       RunOptions)
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
_LAST_CAMPAIGN_ID: str | None = None


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
    global _LAST_CAMPAIGN_ID  # pylint: disable=global-statement  (one counter per process)
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
            if cid != _LAST_CAMPAIGN_ID:
                _LAST_CAMPAIGN_ID = cid
                return cid
            time.sleep(0.005)


class CampaignController:
    """Drives a campaign (batch or search) to completion over one backend."""

    def __init__(self, *, campaign_id, results_dir, runs, backend: ExecutionBackend,
                 options: RunOptions, store: CampaignStore, campaign_config_dump: dict,
                 vast_dir: str, strategy=None, evaluator=None, compose=None,
                 per_batch: int = 1, postprocessing=None, batch_campaign_data=None,
                 stop_conditions=None, repetition_policy=None, state=None,
                 notifier=None, description="", created_by="", origin=None):
        self.campaign_id = campaign_id
        self.campaign_root = os.path.join(results_dir, campaign_id)
        self.runs = runs
        self.backend = backend
        self.options = options
        self.store = store
        # The store learns which MACHINE a run used from the run's own sysinfo, but what
        # that machine IS lives in the cluster API. This is the one place holding both, so
        # it is where they are introduced; a backend that cannot answer says so and the
        # machines are recorded without their hardware.
        if store is not None and backend is not None:
            store.set_node_facts_resolver(backend.node_facts)
        self.campaign_config_dump = campaign_config_dump
        self.vast_dir = vast_dir
        # Free text describing this launch; recorded on the campaign row so it stays
        # with the results (empty when the launcher gave none).
        self.description = description
        #: Who says they launched this. Self-declared; see CampaignStore.create_campaign.
        self.created_by = created_by
        #: Where the configuration came from (a ``CampaignOrigin``), or None when unknown.
        #: Recorded on the campaign row and never read back by anything here -- the
        #: controller runs what it was handed, not what the origin names.
        self.origin = origin
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
        # Optional repetition allocation policy, applied between ask() and compose.
        # None means the .vast declared no `repetitions:` block, and every cell runs
        # `execution.runs` times exactly as before -- absence of a policy, not a
        # policy of uniformity.
        self.repetition_policy = repetition_policy
        #: Every evaluation scored so far, in order. The repetition policy reads it to
        #: judge where the landscape is contested; nothing else depends on it.
        self._history: list = []
        # Optional control-channel state (cluster mode). When set, the controller
        # publishes loop phase/progress and honours the cooperative `stop` command.
        self.state = state
        # ntfy push notifications (no-op when no topic is configured). Built bound
        # to this campaign id so concurrent campaigns report independently.
        self.notifier = notifier or Notifier.from_env(campaign_id)
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
            created_by=self.created_by, origin=self.origin)
        # Before a single job exists. The row just written is the only place the
        # campaign's description, who launched it and where its configuration came from
        # are recorded, and on a lane whose driver disk is scratch a record published at
        # the end is missing from every campaign that did not reach one.
        self.backend.publish_records(self.campaign_root)
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
            self._record_container_failures(campaign_id)
            self._stop_progress_poller()
            # The heartbeat deliberately outlives run(): share and postprocessing are
            # the longest stretch of a campaign in which nothing else reports, and
            # stopping it here left exactly that window silent. `end_campaign` stops it.
            remove_campaign_log_handler(log_handler)

    def _progress_deadline(self) -> int | None:
        """How long this campaign's progress may legitimately stand still, in seconds.

        The **declared** job budget, used as declared: packed runs may publish their
        results in one burst per job, so a per-run figure would accuse a healthy packed
        campaign of stalling. Published on the status because only the controller can see
        the ``.vast``; readers just compare against it.

        This is the same number the cluster lane puts on ``activeDeadlineSeconds``, which
        is the point -- were the two to diverge, a Job could be force-killed while the
        status still called the run healthy.

        ``None`` when the ``.vast`` declares no ``execution.timeout`` — the cluster's
        force-kill backstop is deliberately *not* substituted here. It exists so a run
        cannot hang forever, which is a fine reason to kill at one hour and a terrible
        reason to call a two-minute pilot healthy for the first fifty-nine.
        """
        execution = (self.campaign_config_dump or {}).get("execution") or {}
        return declared_job_seconds(execution)

    def _record_container_failures(self, campaign_id: int) -> None:
        """Lift ``_execution/container_failures.json`` into the ``container_failure`` table.

        In the ``finally`` of :meth:`run` for the same reasons as
        :meth:`_record_execution_provenance`, and one sharper one: the campaign this exists
        for is the one that DIED. A campaign that fails mid-batch records no ``run`` rows
        at all and never postprocesses, so it never reaches the index -- and the query
        interface fetches only ``campaign.db``. Writing here is
        what makes the evidence reachable by SQL for exactly the campaigns that need it.

        The JSON file stays the record; this is an index into it. Best-effort throughout:
        bookkeeping about a failure must never become a second failure.
        """
        try:
            records = read_container_failures(Path(self.campaign_root))
            if records:
                self.store.record_container_failures(campaign_id, records)
        except Exception:  # pylint: disable=broad-except
            logger.debug("Could not record container failures.", exc_info=True)

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
            execution = read_execution_metadata(Path(self.campaign_root))
            self.store.record_execution(campaign_id, execution)
        except FileNotFoundError:
            logger.debug("No execution.yaml yet; provenance not recorded.")
            execution = {}
        except Exception:  # pylint: disable=broad-except
            logger.debug("Could not record execution provenance.", exc_info=True)
            execution = {}
        self._persist_build_manifests(execution)

    def _persist_build_manifests(self, execution: dict) -> None:
        """Copy each image's build lock into the campaign, while the images are still here.

        Done here because this is the one point that runs in Python, on both lanes, with the
        campaign root and the resolved images both in hand -- the local lane writes execution.yaml
        from a generated shell script, so nothing earlier knows the directory.

        And it has to happen at all because the lock is baked *into* the image: leave it there and
        it disappears with the image, which is precisely when a rebuild would need it.

        Best-effort like everything else in this method: bookkeeping must never turn a finished
        campaign into a failed one.
        """
        images = execution.get("images") or {}
        if not images:
            return
        try:
            from robovast.common.campaign_data import write_build_manifests
            from robovast.service.image_build import read_image_build_manifest

            manifests = {role: read_image_build_manifest(image)
                         for role, image in sorted(images.items())}
            write_build_manifests(self.campaign_root,
                                  {role: m for role, m in manifests.items() if m})
        except Exception:  # pylint: disable=broad-except
            logger.debug("Could not persist build manifests.", exc_info=True)

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
        # ``batch_since`` is stamped with them, and for the same reason: the counters
        # are only readable as a rate against the clock they reset with.
        self.state.update(runs={"completed": 0, "total": total, "no_result": 0,
                                "failed": 0},
                          batch_since=time.time())
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
        invalid_runs_count = 0
        # Resolved once for the whole batch: the ledger is a single ``is_file`` miss for
        # every campaign nobody intervened in, which is nearly all of them.
        invalidated = set(invalid_runs(Path(self.campaign_root)))
        for cfg in configs:
            name = cfg["name"]
            cdir = os.path.join(self.campaign_root, name)
            run_dirs = list_run_dirs(cdir)
            unit_id = self.store.record_unit(
                batch_id=batch_id, paramset_id=name, config_name=name,
                params=cfg.get("config", {}) or {}, objectives={}, measures={},
                n_samples=len(run_dirs),
                status=aggregate_run_status(run_dirs, invalid=invalidated),
                result_dir=os.path.relpath(cdir, self.campaign_root))
            outcomes = read_run_outcomes(Path(cdir), Path(self.campaign_root))
            self.store.record_runs(unit_id, outcomes)
            # The verdicts are parsed here anyway for the store; tallying them into the
            # live state is what makes a failing trial visible to a status poll. Without
            # it the only failure count was "produced no result", which a scenario that
            # ran and failed does not trip — so the campaign reported itself clean.
            cfg_failed, cfg_killed, cfg_invalid = _tally_outcomes(outcomes)
            failed_runs += cfg_failed
            killed_runs += cfg_killed
            invalid_runs_count += cfg_invalid
        if self.state is not None:
            if failed_runs:
                logger.warning("Batch complete: %d run(s) did not pass.", failed_runs)
            if killed_runs:
                logger.warning("Batch complete: %d run(s) stopped manually.", killed_runs)
            if invalid_runs_count:
                logger.warning("Batch complete: %d trial(s) invalidated by the runner.",
                               invalid_runs_count)
            self.state.update_runs(failed=failed_runs, killed=killed_runs,
                                   invalid=invalid_runs_count)
            self.state.update(batches_done=1)
        self.notifier.batch_finished(0, len(configs))
        # The same per-batch checkpoint the search loop takes. Redundant with
        # ``finalize_campaign`` when the campaign goes on to finish -- and not when it does
        # not, which is the case this exists for: the unit and run rows just written are the
        # campaign's tally, and losing them to a crash in the finish tail would leave a
        # campaign whose results are all in the store reading as if nothing had run.
        self.backend.publish_records(self.campaign_root)
        logger.info("\n%s\n✅  Batch run complete  —  %d configuration(s) in %s\n%s",
                    _BAR, len(configs), self.campaign_root, _BAR)
        return {"mode": "batch", "configs": len(configs), "campaign_root": self.campaign_root}

    # -- search mode --------------------------------------------------------

    def _run_search(self, campaign_id: int):
        from robovast.search.stopping import StopSnapshot
        stop = self.stop_conditions
        # A scalar best exists only when there is one objective. With several, "best" is not
        # merely unknown but undefined -- nothing ranks "close but fast" against "slow but
        # safe" without a weighting nobody has -- so the loop folds no best and the
        # deliverable is the non-dominated front instead. Asking the strategy for
        # `single_objective` here kills a two-objective campaign before its first batch, on
        # behalf of a strategy that has not been consulted.
        objectives = self.strategy.objectives
        obj_name = objectives[0].name if len(objectives) == 1 else None
        if not stop.has_budget:
            logger.warning("No 'budget' cap configured — this search is bounded "
                           "only by its 'stopping' criteria; it may run a long time.")
        start = time.monotonic()
        best_objective = None          # best-so-far, in raw objective units
        result = None
        # On `self`, not a local, and that is the whole point: the loop below runs in a
        # callee, so a local here would still read 0 after the callee raised -- which is
        # how the first campaign to exercise this path recorded `batches: 0` for 22
        # batches of completed work. A count that survives the raise is the one fact this
        # record exists to carry.
        self._batches_done = 0
        # Same reason as _batches_done above: on `self`, so a raise in the callee does
        # not lose the counts the stop record and the progress line are built from.
        self._evaluations_done = 0
        self._runs_done = 0
        # Zero for a campaign starting now; for one being re-entered after a service
        # restart, everything its earlier life recorded. `_search_loop` already begins at
        # `self._batches_done` -- it was written that way so an abort mid-loop still counted
        # the batches behind it -- so seeding these IS the resume.
        start -= self._rehydrate_search(campaign_id)
        # The search's time ORIGIN, published once, so every reader can derive elapsed itself
        # instead of waiting for the next batch to republish it.
        #
        # `start` is a time.monotonic() reading and cannot cross a process boundary, so it is
        # converted to the time.time() epoch the other `*_since` fields use. The subtraction
        # above is carried across with it, which is what makes a resumed search report the
        # wall-clock age its `time` budget is actually capped against rather than a fresh clock.
        #
        # Published here and never again. Refreshing a `time` budget's `current` on a timer was
        # the obvious alternative and would have broken stall detection outright:
        # `_progress_signal` includes each budget row's `current`, so a row rewritten from
        # wall-clock advances the progress signal on every poll forever and no time-budgeted
        # search could be called stalled again. An origin cannot do that.
        if self.state is not None:
            self.state.update(search_since=time.time() - (time.monotonic() - start))
        # Publish the budget BEFORE the first batch, not only after it.
        #
        # Every criterion is reported from the end of the loop below, so until the first
        # round closed a search published no budget at all -- no batches counter, no time
        # cap, nothing -- and a reader saw the runs bar alone for however long that round
        # took. On a batch of forty runs that is the entire window in which somebody is
        # asking whether the search is going anywhere, and it is also the window in which
        # the campaign card has no batches row to offer (or, since the objective chart
        # hangs off that row, to open).
        #
        # Honest at t=0 rather than optimistic: `0 / 50` batches and `0s` elapsed are
        # facts, and a `target_objective` with nothing to report yet comes through as NaN,
        # which `_budget_item` renders as `None` and the readers show as `—`. A `metric`
        # criterion reports nothing at all until the strategy has measured it, and
        # `_progress` already omits that row rather than inventing a value for it.
        self._publish_budget(stop, StopSnapshot(batch=0, elapsed=0.0))
        try:
            result, best_objective = self._search_loop(
                campaign_id, stop, obj_name, start, best_objective)
        except BaseException as exc:
            # A search that dies mid-loop never reached `record_outcome` below, so its
            # campaign row said NOTHING about why it stopped -- stop_kind, stop_reason and
            # batches all NULL, indistinguishable from a campaign that never ran a batch.
            # The one that motivated this had two batches of real data behind that silence.
            self.store.record_outcome(
                campaign_id, batches=self._batches_done,
                elapsed_s=time.monotonic() - start, **self._abort_outcome(exc))
            raise
        return self._finish_search(campaign_id, result, self._batches_done, start)

    def _rehydrate_search(self, campaign_id: int) -> float:
        """Re-drive the strategy and the counters from what this campaign already recorded.

        Returns how long the campaign has already been alive, in seconds, which the caller
        subtracts from its ``time.monotonic()`` origin. Wall-clock age rather than time
        actually spent computing, because that is what a ``time`` budget caps: a search that
        got a fresh clock on every restart would have no wall-clock bound at all.

        A no-op for a campaign starting now, whose store has no batches -- so there is no
        resume branch here, only a loop over a record that is usually empty.

        The strategy is re-driven through :meth:`~robovast.search.strategy.SearchStrategy.resume`
        rather than restored from a serialized state, because nothing serializes one; see
        that method for why the replay is by batch and asks before it tells.
        """
        from robovast.search.history import recorded_batches

        batches = recorded_batches(self.store, campaign_id)
        if not batches:
            return 0.0
        self.strategy.resume(batches)
        self._batches_done = len(batches)
        for batch in batches:
            self._evaluations_done += len(batch.evaluations)
            self._history.extend(batch.evaluations)
            # What the batch COST, by the same measure the live loop uses: executions
            # attempted -- every cell's ALLOCATION, not what produced a sample. A draw that
            # composed to nothing still occupied the plan its allocation reserved, so this
            # sums over every recorded cell rather than over the scored ones.
            #
            # Read from the record rather than re-derived. `search.repetitions` sizes each
            # cell separately, so re-deriving meant recounting an unevenly-spent campaign as
            # an evenly spent one -- under where the policy had spent above `execution.runs`,
            # over where it had spent below -- and a `runs` budget then stopped the resumed
            # search in the wrong place. `execution.runs` stands in only for a row that
            # recorded no allocation, which is a store from before one could be recorded,
            # where it is what that cell actually got.
            self._runs_done += sum((n or self.runs) for n in batch.reps)
        logger.info("Resuming search after %d recorded batch(es): %d evaluation(s), "
                    "%d run(s) already spent.",
                    self._batches_done, self._evaluations_done, self._runs_done)
        return max(0.0, time.time() - (self._campaign_started_at(campaign_id) or time.time()))

    def _campaign_started_at(self, campaign_id: int):
        """The campaign row's ``created_at``, or ``None`` when it cannot be read.

        ``None`` means the elapsed budget restarts from zero, which is the wrong answer but
        the only honest one available -- and it is reported by the caller's own log line
        rather than silently assumed.
        """
        try:
            row = self.store._conn.execute(  # noqa: SLF001 - no narrower reader exists
                "SELECT created_at FROM campaign WHERE id = ?", (campaign_id,)).fetchone()
            return row[0] if row else None
        except Exception as e:  # noqa: BLE001 - a clock is not worth ending a campaign
            logger.warning("Could not read the start time of campaign %s: %s",
                           self.campaign_id, e)
            return None

    def _abort_outcome(self, exc) -> dict:
        """``stop_kind``/``stop_reason`` for a search that ended by raising.

        A cooperative stop is classified as one even when it arrives as an exception, the
        same way :meth:`run` reclassifies it: the operator asked, and recording that as an
        error would file a deliberate act under faults.
        """
        stopped = isinstance(exc, CampaignStopped) or (
            self.state is not None and self.state.stop_requested)
        if stopped:
            return {"stop_kind": "stopped", "stop_reason": str(exc) or "stop requested"}
        return {"stop_kind": "error", "stop_reason": f"{type(exc).__name__}: {exc}"}

    def _finish_search(self, campaign_id, result, batch_idx, start):
        """Record the outcome of a search that ended the way it meant to, and report."""
        elapsed_s = time.monotonic() - start
        self.store.record_outcome(
            campaign_id, stop_kind=result.kind, stop_reason=result.reason,
            batches=batch_idx, elapsed_s=elapsed_s)
        report = self.strategy.report()
        # Computed here rather than in each strategy: a front is the shape of the answer, not a
        # way of searching, so every strategy that reports its evaluations gets one without
        # knowing the concept. A strategy that fills `front` itself (one whose optimiser tracks
        # it natively) is left alone.
        if len(self.strategy.objectives) > 1 and not report.front:
            from robovast.search.pareto import pareto_front
            report.front = pareto_front(report.evaluations, self.strategy.objectives)
        report.extra['stop'] = {"kind": result.kind, "reason": result.reason,
                                "batches": batch_idx, "elapsed_s": elapsed_s}
        logger.info("\n%s\n✅  Search complete  —  %d batch(es), %d evaluation(s) "
                    "(%s)\n%s", _BAR, batch_idx, len(report.evaluations), result.reason,
                    _BAR)
        return report

    def _search_loop(self, campaign_id, stop, obj_name, start, best_objective):
        """The ask/evaluate/tell loop; returns ``(result, best_objective)``.

        The batch count lives on ``self._batches_done`` rather than being returned,
        because :meth:`_run_search` needs it on the path where this does NOT return.
        """
        from robovast.search.compose import distinct_draws
        from robovast.search.stopping import StopResult, StopSnapshot
        batch_idx = self._batches_done
        result = None
        while True:
            proposed = self.strategy.ask(self.per_batch)
            # A repeated draw is one cell, not two: collapsed before composition, which
            # can only give it one config name and one result directory.
            param_sets = distinct_draws(proposed, f"Batch {batch_idx}")
            if self.repetition_policy is not None:
                param_sets = self.repetition_policy.assign(param_sets, self._history)
            # `asked` is what the STRATEGY proposed, not what survived the line above: a
            # resume re-drives the strategy through the sequence it saw, and asking it for
            # the collapsed count would rewind its stream by every repeat.
            batch_id = self.store.open_batch(campaign_id, batch_idx, ".",
                                             asked=len(proposed))
            if self.state is not None:
                self.state.update(batch=batch_idx)
            logger.info("\n%s\n🔁  Batch %d  —  %d parameter set(s)\n%s",
                        _BAR, batch_idx, len(param_sets), _BAR)
            # Counted BEFORE the batch runs, from what was asked for: this is the
            # wall-clock cap, so it must count executions attempted, not the subset
            # that produced a sample. A draw that composes to nothing costs no run and
            # contributes none, which is why it is summed over param_sets here rather
            # than taken from self.runs * per_batch.
            fresh, recalled = self._split_already_evaluated(param_sets, batch_idx)
            self._runs_done += sum((ps.n_reps or self.runs) for ps in fresh)
            scored = self._run_search_batch(fresh, batch_idx, batch_id)
            # The strategy is told about every cell it proposed, whether this batch
            # measured it or an earlier one did. A recalled cell is a real answer to a
            # real proposal -- it is what that cell measured -- so withholding it would
            # hand back a short generation carrying less than the campaign knows.
            self.strategy.tell(scored + recalled)
            batch_idx += 1
            # Published immediately, so an abort anywhere after this counts this batch.
            self._batches_done = batch_idx
            # Parameter sets newly SCORED -- a composition_failed or no_sample draw never
            # reaches tell(), so it is not an evaluation, and neither is a recalled cell:
            # it was counted by the batch that measured it, and counting it again would
            # let a search exhaust an `evaluations` budget without measuring anything.
            self._evaluations_done += len(scored)
            self._history.extend(scored)
            best_objective = self._update_best(best_objective, scored, obj_name)

            snap = StopSnapshot(batch=batch_idx,
                                elapsed=time.monotonic() - start,
                                best_objective=best_objective,
                                evaluations=self._evaluations_done,
                                runs=self._runs_done,
                                metrics=self.strategy.report().extra if stop.needs_metrics else {})
            progress = stop.progress(snap)
            # Live progress toward every budget/stopping criterion.
            logger.info("📊  %s", " | ".join(
                f"{p.label} {self._fmt(p.current)}/{self._fmt(p.limit)}" for p in progress))
            if self.state is not None:
                self.state.update(batches_done=batch_idx, best_objective=best_objective,
                                  budget=[self._budget_item(p) for p in progress])
            # What this batch MEASURED. A recalled cell was counted by the batch that
            # measured it, and counting it again would report work that did not happen.
            self.notifier.batch_finished(batch_idx - 1, len(scored))
            # The search's checkpoint. Everything the loop would need to pick up here --
            # which batches ran and what each parameter set scored -- is in the rows just
            # written, so publishing them per batch is what makes a search resumable at a
            # batch boundary rather than only from the start.
            self.backend.publish_records(self.campaign_root)
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
        return result, best_objective

    def _split_already_evaluated(self, param_sets, batch_idx):
        """Split a batch into the cells to measure and the ones already on record.

        A strategy revisits: TPE re-proposes a category it likes, and on a discrete space
        every strategy here eventually lands twice on the same cell. Across batches that is
        not merely wasteful but unrecordable -- the result directory is addressed by
        ``ParamSet.id``, so a second evaluation of one cell writes into the first's
        directory, over its runs, and the campaign dies on the conflicting job link that
        guards exactly that ("one run's artifacts cannot live in two jobs"). Measured on a
        four-cell space: batch 0 scored three cells, batch 1 re-proposed one of them and the
        campaign ended there, on batch 1 of 2.

        So a cell is measured once per campaign. The recalled evaluation is the one that
        cell produced -- not a substitute for it -- and re-running would spend a batch's
        compute to answer a question the campaign has already answered.

        The history this reads spans the whole campaign, including what a resumed one
        replayed, so a restart does not make the campaign forget what it has measured.
        """
        by_id = {}
        for ev in self._history:
            by_id.setdefault(ev.params.id, ev)
        fresh, recalled = [], []
        for ps in param_sets:
            previous = by_id.get(ps.id)
            if previous is None:
                fresh.append(ps)
            else:
                recalled.append(previous)
        if recalled:
            logger.info(
                "Batch %d: %d of %d cell(s) were measured by an earlier batch (%s). They "
                "are not run again -- their results are addressed per parameter set, so "
                "there is one place to put them and it already holds the answer -- and the "
                "strategy is told what they scored.",
                batch_idx, len(recalled), len(param_sets),
                ", ".join(ev.params.id for ev in recalled))
        return fresh, recalled

    @staticmethod
    def _fmt(v):
        return f"{v:.4g}" if isinstance(v, float) else str(v)

    def _publish_budget(self, stop, snap) -> None:
        """Publish every criterion's progress for *snap*, when there is a state to publish to.

        A separate step from the loop's own combined update because it is also called once
        before the loop, where there is no batch count or best objective to report yet.
        """
        if self.state is None:
            return
        self.state.update(budget=[self._budget_item(p) for p in stop.progress(snap)])

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
                "limit": float(p.limit), "done": bool(p.done), "kind": p.kind,
                "op": p.op}

    def _update_best(self, best, evaluations, obj_name):
        """Fold this batch's objective values into the best-so-far (raw units,
        direction-aware via the strategy's objective spec).

        ``obj_name`` is ``None`` for a multi-objective search, where there is no scalar to
        fold; the front computed at report time is that search's answer.
        """
        if obj_name is None:
            return None
        spec = self.strategy.objectives[0]
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
        if not param_sets:
            # Every cell this batch proposed was measured by an earlier one. There is
            # nothing to compose, nothing to run and nothing to postprocess, and going
            # through the motions would publish a batch's worth of run progress for a
            # batch that executes no runs.
            logger.info("Batch %d: nothing new to measure.", batch_idx)
            return []
        groups: dict[int, list] = {}
        for ps in param_sets:
            groups.setdefault(ps.n_reps or self.runs, []).append(ps)
        multi = len(groups) > 1

        # Expected runs across the whole batch (all reps-groups), for run progress.
        self._begin_batch_progress(sum((ps.n_reps or self.runs) for ps in param_sets))
        evaluations = []
        failed_runs = 0
        killed_runs = 0
        invalid_runs_count = 0
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
                # The tag identifies this conversion: one per repetitions-group, which is
                # what the conversion Job's name has to be discriminated by.
                self._run_postprocessing(tag)

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
                            n_samples=0, status="composition_failed", result_dir="",
                            n_reps=reps)
                        continue
                    config_dir = Path(self.campaign_root) / config_name
                    result_dir = os.path.relpath(config_dir, self.campaign_root)
                    try:
                        ev = self.evaluator.evaluate(config_dir, ps)
                    except NoSampleError as exc:
                        # The cell ran but produced nothing measurable -- every run lost to
                        # infrastructure (container bringup, a crash before recording started)
                        # rather than to the system under test. Treated exactly like an
                        # unrealizable draw above: recorded and left out of `evaluations`,
                        # which every strategy tolerates. Aborting instead discarded up to 49
                        # completed batches over one cell, and scoring a fallback 0.0 instead
                        # would be a fabricated observation -- the extractor is right to refuse
                        # both, so the framework has to be the one that carries on.
                        #
                        # Only this type is caught. Any other exception is an extractor defect
                        # and must still abort, or a broken objective goes unnoticed -- the
                        # exact failure NoSampleError's docstring records.
                        logger.warning("Batch %d: %s produced no measurable sample, "
                                       "recorded and skipped: %s", batch_idx, config_name, exc)
                        unit_id = self.store.record_unit(
                            batch_id=batch_id, paramset_id=ps.id, config_name=config_name,
                            params=ps.values, objectives={}, measures={},
                            n_samples=0, status="no_sample", result_dir=result_dir,
                            n_reps=reps)
                        # Unlike composition_failed, these runs HAPPENED: record them so the
                        # cell's failures are visible and counted rather than vanishing with
                        # the evaluation that could not use them.
                        outcomes = read_run_outcomes(config_dir, Path(self.campaign_root))
                        self.store.record_runs(unit_id, outcomes)
                        cfg_failed, cfg_killed, cfg_invalid = _tally_outcomes(outcomes)
                        failed_runs += cfg_failed
                        killed_runs += cfg_killed
                        invalid_runs_count += cfg_invalid
                        continue
                    evaluations.append(ev)
                    unit_id = self.store.record_unit(
                        batch_id=batch_id, paramset_id=ps.id, config_name=config_name,
                        params=ps.values, objectives=ev.objectives, measures=ev.measures,
                        n_samples=ev.n_samples, status="evaluated",
                        result_dir=result_dir, n_reps=reps)
                    outcomes = read_run_outcomes(config_dir, Path(self.campaign_root))
                    self.store.record_runs(unit_id, outcomes)
                    cfg_failed, cfg_killed, cfg_invalid = _tally_outcomes(outcomes)
                    failed_runs += cfg_failed
                    killed_runs += cfg_killed
                    invalid_runs_count += cfg_invalid
        finally:
            # Same tally as batch mode: a trial that ran and failed is invisible in the
            # resultless count, so surface it before the batch's progress is closed out.
            if self.state is not None and (failed_runs or killed_runs
                                           or invalid_runs_count):
                if failed_runs:
                    logger.warning("Batch %d: %d run(s) did not pass.",
                                   batch_idx, failed_runs)
                if killed_runs:
                    logger.warning("Batch %d: %d run(s) stopped manually.",
                                   batch_idx, killed_runs)
                if invalid_runs_count:
                    logger.warning("Batch %d: %d trial(s) invalidated by the runner.",
                                   batch_idx, invalid_runs_count)
                self.state.update_runs(failed=failed_runs, killed=killed_runs,
                                       invalid=invalid_runs_count)
            self._end_batch_progress()
        return evaluations

    def _run_postprocessing(self, tag: str = "") -> None:
        """Run search.postprocessing over the campaign root (no-op if none).

        *tag* names which conversion this is (``batch-<n>``, plus ``/reps-<n>`` when a
        batch has more than one repetitions-group). It becomes the conversion Job's
        discriminator, without which the second conversion of a campaign silently
        inherits the first one's completed Job.

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
        from robovast.common.config_plugins import ensure_plugins_importable
        ensure_plugins_importable(self.vast_dir)
        # Imported lazily to avoid importing the results_processing stack (and its
        # heavier deps) unless a search actually configures postprocessing.
        from robovast.results_processing.postprocessing import run_postprocessing_commands

        # Deserializing a rosbag needs the image the runs recorded it with, which is why the
        # campaign-level block dispatches an in-cluster Job rather than importing anything.
        # A search's block runs every batch; running all of it here launches a named
        # converter's aux container from the controller, which resolves the image against the
        # default project instead of the deployment's and exits 1 -- and every plugin after it
        # reads files that were never written.
        container, local = split_container_postprocessing(
            self.postprocessing, config_dir=self.vast_dir)
        if container:
            self._convert_bags_in_cluster(container, tag)
        if local:
            run_postprocessing_commands(
                local, results_dir=self.campaign_root,
                config_dir=self.vast_dir, output=logger.info)

    def _convert_bags_in_cluster(self, rosbag_cmds: list, tag: str = "") -> None:
        """Run a search batch's bag conversion the way the campaign-level path does.

        **One Job per conversion.** *tag* discriminates it. Naming the Job after the campaign
        alone makes the second conversion's create return 409; the wait then reads the FIRST
        conversion's completed Job and reports "rosbag conversion complete" having converted
        nothing -- 0 outputs synced, and an extractor that refuses the batch while naming the
        world as the likely cause.

        **Two steps, not one.** The Job writes its output to the object store; ``sync_outputs``
        pulls it into the campaign root, and nothing can read a CSV before that happens. A
        version of this that ran the Job and skipped the sync logged "rosbag conversion
        complete" and then handed the extractor a directory with no CSVs in it -- which reads
        as a conversion that lied about finishing.

        The sync runs **regardless of the Job's outcome**, for the reason the campaign-level
        path gives: the conversion tees its own error into ``postprocessing.log`` and mirrors
        it out, so skipping the sync on failure discards the only account of what went wrong.

        On a local backend there is no Job to submit and the in-process path is already
        correct, so the commands fall through to it. A failure is reported and does not
        raise: the extractor decides whether a batch is scorable and refuses loudly when its
        inputs are missing, which says more than an exception about a Job.
        """
        cluster_config = getattr(self.backend, "cluster_config", None)
        if cluster_config is None:
            from robovast.results_processing.postprocessing import run_postprocessing_commands
            run_postprocessing_commands(
                rosbag_cmds, results_dir=self.campaign_root,
                config_dir=self.vast_dir, output=logger.info)
            return
        try:
            run_job, sync, image_for, complete_message = _conversion_job_runner()
            ok, message = run_job(
                cluster_config, self.campaign_id,
                os.environ.get("ROBOVAST_NAMESPACE", "default"),
                image_for(self.campaign_root),
                unwrap_conversion_commands(rosbag_cmds),
                kube_context=getattr(self.backend, "kube_context", None),
                discriminator=tag)
            sync(cluster_config, self.campaign_id, self.campaign_root)
            # Only now can the message say where the conversion error is: the sync is what
            # decides whether a POSTPROCESSING section exists to point at.
            message = complete_message(
                message,
                os.path.join(self.campaign_root, "_execution", "postprocessing.log"))
            logger.info("Batch bag conversion: %s", message)
            if not ok:
                logger.warning("Batch bag conversion failed; this batch's metrics will be "
                               "missing and the extractor will say so: %s", message)
        except Exception as exc:  # pylint: disable=broad-except
            # RAISED, not warned. A conversion that could not START is a different failure
            # from one that ran and produced nothing, and reporting them the same way
            # loses that distinction. The second is the extractor's business -- it refuses the batch and names
            # what was missing. The first is a broken campaign: every batch will hit it,
            # nothing will ever score, and the reason is not in the world.
            #
            # Warning here instead sends the reader somewhere correct and useless: the
            # extractor then reports that no run recorded a value and points at the
            # postprocessing plugins, which are fine, while the cause sits in a warning
            # further up the log.
            raise RuntimeError(
                f"batch bag conversion could not run at all, so no batch of this search can "
                f"be scored: {exc}. This is not a missing measurement -- the conversion was "
                f"never attempted, and the extractor's own error would name the world "
                f"instead of this.") from exc



# -- builders ---------------------------------------------------------------

def split_container_postprocessing(commands, config_dir: str = "") -> tuple:
    """Split postprocessing into what needs the campaign's execution image, and what does not.

    Returns ``(container_commands, local_commands)``. The caller runs the container half
    first, because the local half is what reads its output.

    Which half a command belongs in is the **plugin's** call, via
    :attr:`~robovast.results_processing.postprocessing_plugins.BasePostprocessingPlugin.needs_execution_image`,
    not a list kept here. A future plugin that needs the image -- another deserializer, a
    tool only the SUT image carries -- is then dispatched correctly without this function
    changing; a name list here would silently serve only what existed when it was written.

    The ``rosbags_*`` names are the one thing resolved by name rather than by class, and
    they have to be: they are not plugins at all but shorthand the orchestrator batches into
    a single ``rosbags_process`` per bag (so a bag is read once rather than once per
    handler), exactly as the campaign-level path batches them. The batch map is their
    declaration.

    A command that cannot be resolved is left local. It will fail loudly where it runs,
    which is a better message than one invented here about dispatch.
    """
    from robovast.results_processing.postprocessing import (ROSBAG_BATCH_NAMES,
                                                            _batch_rosbags_commands,
                                                            resolve_postprocessing_plugin)
    if not commands:
        return [], []

    def _name(command):
        return command if isinstance(command, str) else next(iter(command))

    def _needs_image(command) -> bool:
        name = _name(command)
        if name in ROSBAG_BATCH_NAMES:
            return True
        try:
            plugin = resolve_postprocessing_plugin(name, config_dir)
        except Exception:  # pylint: disable=broad-except
            return False
        return bool(getattr(plugin, "needs_execution_image", False))

    def _is_rosbag(command) -> bool:
        return _name(command) in ROSBAG_BATCH_NAMES or _name(command) == "rosbags_process"

    rosbag_cmds = [c for c in commands if _is_rosbag(c)]
    # Batched only when there is something to batch: `_batch_rosbags_commands` also injects
    # the infrastructure-bag handlers, and running those for a campaign that asked for no
    # bag conversion at all would convert a bag nobody wanted, once per batch.
    container = list(_batch_rosbags_commands(rosbag_cmds)) if rosbag_cmds else []
    # Anything else that declares it needs the image travels with them, unbatched: batching
    # is a rosbag-specific optimisation, not the dispatch rule.
    container += [c for c in commands if not _is_rosbag(c) and _needs_image(c)]
    local = [c for c in commands if not _is_rosbag(c) and not _needs_image(c)]
    return container, local


def _conversion_job_runner():
    """The four cluster helpers a batch conversion needs, resolved in one place.

    A seam rather than four imports at the call site: it keeps the cluster package out of
    the import path on a local run, and lets a test substitute the whole set -- which is
    the only way to check that the Job and the SYNC both happen, and in that order, without
    a cluster to run them against.

    ``with_log_pointer`` rides along because a failed Job's message is only half-written
    until the sync has run: it says where to read the conversion error, and whether that
    place exists is not known until then.
    """
    from robovast.execution.cluster_execution.postprocess_job import (
        campaign_execution_image, run_conversion_job, sync_outputs, with_log_pointer)
    return run_conversion_job, sync_outputs, campaign_execution_image, with_log_pointer


def unwrap_conversion_commands(commands) -> list:
    """The shape ``run_conversion_job`` takes: the inner ``{plugins, bag_dir}`` dicts.

    The local runner takes ``{'rosbags_process': {...}}``; the Job takes what is inside it.
    The campaign-level path has always unwrapped here (``rosbag_commands_for`` ends in
    ``out.append(cmd["rosbags_process"] or {})``), and a search that dispatched the wrapped
    form created the Job with the right image and then watched it fail -- which reads as a
    broken converter rather than a mismatched argument.

    Anything that is not a ``rosbags_process`` batch is passed through: a plugin declaring
    ``needs_execution_image`` has no wrapper to strip.
    """
    out = []
    for command in commands or []:
        if isinstance(command, dict) and "rosbags_process" in command:
            out.append(command["rosbags_process"] or {})
        else:
            out.append(command)
    return out


def _chain_postprocessing(backend: ExecutionBackend, campaign_root: str,
                          campaign_id: str, state=None,
                          options: "RunOptions | None" = None) -> None:
    """Run analysis postprocessing in-cluster, when the caller asked for it.

    Called from the builders' ``finally`` **after the store is closed** (so
    ``campaign.db`` is flushed — the index ingest mirrors it) and
    **before** :func:`_finalize`, so the resulting CSVs ride the existing
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
    # A campaign this process RESUMED holds only its control plane until now (see
    # cluster_execution.campaign_resume), and the derived data comes from the whole tree.
    # Here rather than in the caller's tail because this is the first reader that needs it:
    # ``finalize_campaign`` only re-uploads, so what is missing locally is simply not
    # re-sent and the store keeps the copy it already has. A no-op for a campaign that ran
    # start to finish in this process.
    # The phase moves BEFORE the root is completed, because completing it is postprocessing's
    # own first step and can take minutes on a resumed campaign. Left until after, the campaign
    # sat in `running` for the whole transfer -- where `status.stall_report` measures silence
    # against the per-run budget and calls it "no progress ... the run is not merely slow",
    # sending a reader to diagnose a run that had already finished. That verdict is suppressed
    # off the running phase, and this is what makes the suppression apply.
    if state is not None:
        state.set_phase(Phase.POSTPROCESSING)
    backend.ensure_campaign_root_complete(campaign_root)
    try:
        from robovast.execution.cluster_execution.postprocess_job import postprocess_campaign
        ok, message = postprocess_campaign(
            cluster_config, campaign_id, campaign_root,
            options.namespace or os.environ.get("ROBOVAST_NAMESPACE", "default"),
            # The context this backend submitted the campaign's Jobs with; postprocessing
            # must schedule against the same cluster the runs went to.
            kube_context=getattr(backend, "kube_context", None),
            # Publishes stage 2's step lines as the live ``stage`` marker: this phase has no
            # run counter, so its narration is all a reader has to tell a long step from a
            # stuck one.
            state=state,
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


#: Statuses that are not a verdict about the system under test, and so are counted apart
#: from ``failed`` everywhere. ``killed``: an operator stopped the job by hand.
#: ``invalid``: the runner threw the trial away because a container it depended on crashed
#: under it. Folding either into ``failed`` would report an intervention or an
#: infrastructure fault as evidence against the thing being measured.
NOT_A_VERDICT = ("killed", "invalid")


def _tally_outcomes(outcomes) -> tuple[int, int, int]:
    """``(failed, killed, invalid)`` over one config's run outcomes.

    One definition, shared by batch and search mode, so the two cannot come to disagree
    about what counts as a failure. ``killed`` and ``invalid`` are counted apart and
    deliberately kept **out** of ``failed`` (see :data:`NOT_A_VERDICT`): neither is a
    verdict about the system under test, so folding them in would report a human
    intervention or a crashed sidecar as a trial failure in the live status, the
    notification, and every reader downstream of them.
    """
    killed = sum(1 for o in outcomes if o.get("status") == "killed")
    invalid = sum(1 for o in outcomes if o.get("status") == "invalid")
    failed = sum(1 for o in outcomes
                 if o.get("status") not in ("passed",) + NOT_A_VERDICT)
    return failed, killed, invalid


def outcome_summary(snap) -> tuple[str, bool]:
    """One line describing what a finished campaign actually produced, and whether it
    is degraded.

    Built here rather than at each reader because "did this campaign succeed?" cannot
    be answered from ``phase`` alone — a campaign whose trials all passed but whose
    postprocessing failed stays ``finished`` with no CSVs and nothing queryable. A channel
    answering from ``phase`` alone reports that as a clean success.
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
                     "no CSVs, nothing queryable")
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
    ``RunOptions.finalize_phase``. ``run()`` does not publish ``finished`` when it
    returns, because share and postprocessing still have to happen; the campaign is over
    when this runs and not before.

    Callers run it from a ``finally``: a campaign left non-terminal would block every
    waiter until its timeout, which is a worse failure than an early ``finished``.
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
        # that could not resolve — which would otherwise go unannounced entirely.
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
    # Read the verdict *before* any step of this tail can move the phase: the share step
    # below advances it to `sharing`, so a check made after it found a failed campaign no
    # longer saying so — and postprocessing then ran on the incomplete campaign root and
    # raised "no .vast under _config" over the real failure. The verdict is a fact about
    # the run that reached this finally; it must not depend on what the tail does next.
    before = state.snapshot() if state is not None else None
    failed = before is not None and before.phase == Phase.FAILED
    try:
        if state is not None and state.stop_requested:
            logger.info(
                "Campaign %s stopped — skipping postprocessing and finalize upload.",
                campaign_id)
            return
        if options.upload_to_share:
            _share_campaign(backend, campaign_root, options, state, notifier)
            # Hand the phase back: the share step borrows `sharing`, and a failed
            # campaign has to keep saying so. `end_campaign` reads the phase to pick the
            # campaign's one notification, and `publish_terminal_phase` treats a
            # non-terminal `sharing` as "not over yet" — so a failure that also uploaded
            # was announced as finished, with a summary of the runs it never produced.
            if failed and state is not None:
                # With the stage it had: a phase change clears the stage, and here that
                # stage is the failure's own reason line.
                state.set_phase(Phase.FAILED, stage=before.stage)
        # A failed campaign (run() set Phase.FAILED before re-raising into this finally)
        # never finished projecting its results, so campaign_root is missing pieces
        # postprocessing needs — e.g. _config/*.vast. Running it anyway only raises a
        # second, misleading error ("no .vast under _config") that masks the real failure.
        # Skip only the derived-data step; _finalize still runs below so the failure
        # outcome is published (as does _record_campaign_failure).
        if failed:
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
        # After the finalize upload, not before: on a lane whose durable home is a store,
        # the data postprocessing derived reaches that home in the upload above, so a total
        # taken earlier would under-report exactly the artifacts the campaign was
        # postprocessed to produce. Skipped for a failed campaign, which never finished
        # projecting its results -- a partial tree's size is a number that invites the wrong
        # conclusion, and `None` already says "not recorded".
        if not failed:
            _record_results_size(backend, campaign_root, campaign_id, state)
    finally:
        if options.finalize_phase:
            end_campaign(campaign_id, state, notifier)


def _record_results_size(backend: ExecutionBackend, campaign_root: str, campaign_id: str,
                         state) -> None:
    """Measure the campaign's results once and make the figure durable.

    Here rather than on every read: a campaign is displayed far more often than it ends, and
    the alternative -- enumerating the results whenever someone opens the campaign -- pays a
    walk that grows with the campaign in order to tell each viewer the same number.

    Re-writes ``outcome.json`` (a second, kilobyte-sized write of a record already produced
    above) because the measurement can only run once the finalize upload has happened, and
    the record is what carries the figure to a service that no longer has this driver.

    Best-effort throughout: a size is a convenience, and no part of it may cost a campaign
    that has otherwise finished.
    """
    if state is None:
        return
    try:
        total = backend.campaign_results_bytes(campaign_root)
    except Exception:  # pylint: disable=broad-except
        logger.warning("Could not measure the results size of %s", campaign_id,
                       exc_info=True)
        return
    if total is None:
        return
    state.update(results_bytes=total)
    _record_controller_outcome(campaign_root, campaign_id, state, backend)


def _share_campaign(backend: ExecutionBackend, campaign_root: str,
                    options: "RunOptions", state, notifier=None) -> None:
    """Produce the upload-to-share artifact, **before** postprocessing.

    Runs the backend's ``share_campaign`` hook (local: tar.gz on disk; cluster:
    streamed to the share provider). Called here, the shared archive is the minimal,
    untouched campaign — postprocessing only *adds* derived data, which stays out of
    the share — and the backend names it ``raw`` because that is what it finds on
    disk. Best-effort: a share failure is logged but never loses the campaign nor
    blocks postprocessing/finalize.

    The ``sharing`` phase is **borrowed**: this step publishes it for the length of the
    upload and the caller hands the campaign's own phase back (see
    :func:`_finish_campaign`). A failed campaign that also uploads must not stop saying
    it failed while it does.
    """
    try:
        if state is not None:
            state.set_phase(Phase.SHARING)
        backend.share_campaign(campaign_root, options,
                               progress_callback=make_upload_progress_cb(state))
    except Exception as e:  # pylint: disable=broad-except
        # A provider's own refusal (bad credentials, a URL that is not the share, a
        # remote that said no) is self-contained and opts out of the tail via
        # ``include_traceback = False`` — the frames through ``requests`` name nothing
        # its sentence does not, and a stack here reads as a crash rather than as the
        # configuration answer it is. Anything else is a bug and keeps its traceback.
        detail = failure_detail(e)
        logger.warning("Upload-to-share failed; continuing with the campaign.\n%s",
                       detail, exc_info=getattr(e, "include_traceback", True))
        # Record the reason on its own field (not swallowed) so it survives to the
        # durable outcome _finish_campaign writes and can be re-triggered from disk
        # (service run_share). The phase is left for postprocessing/finalize to set —
        # a share failure keeps the campaign finished, it is not a run failure.
        if state is not None:
            state.update(share_error=detail)
        return
    if state is not None:
        state.update(share_error=None)
    if notifier is not None:
        # The resolved provider isn't in scope here; the configured share type is what
        # the service was handed via env (ROBOVAST_SHARE_TYPE), matching the old
        # controller's ``provider.SHARE_TYPE``.
        notifier.uploaded(os.environ.get("ROBOVAST_SHARE_TYPE") or "share")


class UploadProgress:
    """Publishes an upload's progress into ``Status.extra['upload']``.

    Callable as the providers’ ``(bytes_sent, total_bytes)`` ``progress_callback``,
    so it drops into every existing call site unchanged.

    **Two counters, because one cannot answer the question.** A campaign is tarred and
    gzipped straight into the request body, so the compressed length is unknown until
    the last byte — the provider can only ever report bytes *sent* against a total of
    ``0``. What a reader wants is "how far through the campaign am I?", and that is
    knowable: :func:`~robovast.execution.campaign_archive.campaign_source_bytes` gives
    the payload total up front and :meth:`on_member` counts it off as the archiver
    consumes it. So the bar tracks the **source** side and ``sent`` reports the wire
    side beside it — they differ by the compression ratio, which is why both are
    published rather than one being derived from the other.

    Either counter alone still works: with no source total the record carries ``sent``
    and a rate and a reader shows an indeterminate bar.
    """

    def __init__(self, state):
        self._state = state
        # The archiver's writer thread calls `on_member` while the sending thread calls
        # `__call__` -- the whole point of the streamed path is that the two run at once.
        self._lock = threading.Lock()
        self._sent = 0
        self._source_done = 0
        self._source_total = 0
        self._last_t = None
        self._last_sent = 0
        self._last_pct = -1.0
        self._rate = None
        # True when the denominator came from the provider rather than the archiver:
        # then `sent` IS the source counter (the body is a finished file, not a stream
        # being built), so the two must stay in step on every sample.
        self._wire_denominated = False

    def set_source_total(self, total: int) -> None:
        """Declare the payload byte count the archive will be built from."""
        with self._lock:
            self._source_total = max(0, int(total or 0))
            self._publish(force=True)

    def on_member(self, nbytes: int) -> None:
        """Count *nbytes* of campaign payload as consumed by the archiver."""
        with self._lock:
            self._source_done += max(0, int(nbytes or 0))
            self._publish()

    def __call__(self, sent, total) -> None:
        """The providers’ progress callback: *sent* bytes on the wire so far."""
        with self._lock:
            self._sent = sent
            # A provider that knows its total (the path-based, resumable upload) has a
            # denominator of its own; adopt it when the source side has none.
            if total and (self._wire_denominated or not self._source_total):
                self._wire_denominated = True
                self._source_total = total
                self._source_done = sent
            self._publish(final=bool(total) and sent >= total)

    def finish(self) -> None:
        """Publish the true final numbers, unthrottled.

        Needed because the two counters do not end together: the source side hits 100%
        when the archiver has read the last file, while bytes keep going out for as long
        as the compressor's buffer takes to drain. Every sample after that has neither a
        percentage advance nor (on a short upload) half a second behind it, so it is
        throttled away — leaving a record that says 100% and ``0 B sent``. Called by the
        backends once the transfer has actually returned.
        """
        with self._lock:
            self._publish(force=True)

    def _percent(self):
        if self._source_total <= 0:
            return None
        return min(100.0, self._source_done / self._source_total * 100.0)

    def _publish(self, *, force: bool = False, final: bool = False) -> None:
        now = time.time()
        pct = self._percent()
        # Throttle on whichever signal exists. The previous rule ANDed a
        # `sent < total` clause in, which on the streamed path (total always 0)
        # is false for every sample -- so nothing was ever throttled and a large
        # campaign published a status update per 256 KiB chunk.
        # Landing on 100% is never throttled: the bar's last frame is the one a reader
        # is most likely to be looking at, and a transfer that stops at 97% reads as one
        # that stopped.
        if pct is not None and self._last_pct < 100.0 <= pct:
            final = True
        if not force and not final and self._last_t is not None:
            advanced = pct is not None and pct - self._last_pct >= 1.0
            if not advanced and now - self._last_t < 0.5:
                return
        # Only re-derive the rate when the wire counter actually moved: `on_member`
        # samples advance the source side while `sent` stands still, and dividing that
        # zero delta by the elapsed time would report a stalled transfer.
        if self._last_t is not None and now > self._last_t and self._sent > self._last_sent:
            self._rate = (self._sent - self._last_sent) / (now - self._last_t)
            self._last_sent = self._sent
        self._last_t = now
        if pct is not None:
            self._last_pct = pct
        self._state.update(extra={"upload": {
            "sent": self._sent,
            # A back-compat alias for `source_total`: this key predates the two-counter
            # record and meant "the denominator", which is what it still is. New readers
            # want the explicit pair below.
            "total": self._source_total,
            "source_done": self._source_done,
            "source_total": self._source_total,
            "percent": pct,
            "rate": self._rate,
            "updated_at": now,
        }})


def make_upload_progress_cb(state):
    """Return an :class:`UploadProgress` for *state*, or ``None`` with no control channel.

    A fresh one per upload attempt, so its rate baseline resets on a retry.
    """
    if state is None:
        return None
    return UploadProgress(state)


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
                        notifier=None, description="", created_by="", origin=None):
    """Build and run a search campaign. Requires ``campaign_config.search``.

    ``config_filter`` exists here only to be **refused**. A search names its
    configurations after parameter sets it has not drawn yet, so there is nothing
    for a glob to select — and a launch path that accepts the filter and silently
    drops it turns the documented "pilot one configuration before the full sweep"
    into a launch of the entire search budget. Failing is the point; ``pilot`` below
    is the affordance that actually works here.
    """
    from robovast.search.compose import Compose
    from robovast.search.evaluator import Evaluator
    from robovast.search.repetitions import build_repetition_policy
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
        evaluator=Evaluator(search_cfg, vast_dir),
        compose=Compose(vast_file, image_project=opts.image_project,
                        image_project_tag=opts.image_project_tag),
        per_batch=search_cfg.per_batch, postprocessing=search_cfg.postprocessing,
        stop_conditions=build_stop_conditions(search_cfg),
        repetition_policy=build_repetition_policy(
            search_cfg.repetitions, search_cfg.search_space, runs),
        state=state, notifier=notifier,
        description=description, created_by=created_by, origin=origin)
    try:
        return controller.run()
    finally:
        store.close()
        _campaign_root = os.path.join(results_dir, campaign_id)
        # After store.close() (campaign.db flushed, which data.db's `runs` table
        # reads) and before _finalize, so the derived data rides the existing upload.
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
        # container_failures.json is here and not only in the whole-root finalize upload
        # because this path runs for a campaign that FAILED, and finalize does not run for
        # one that was stopped -- which is exactly when the evidence matters most.
        for name in ("outcome.json", "controller.log", "variation.log", "build.log",
                     "container_failures.json", "interventions.json"):
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
                        progress_update_callback=None, image_project=None,
                        image_project_tag=None):
    """Generate the batch campaign data and apply the optional ``--config`` filter.

    Shared by :func:`run_batch_campaign` and the host-side ``cluster run``
    pre-flight check so both select configs through exactly the same code path.
    Raises ``CampaignConfigError`` if the vast-file yields no configs or the filter
    matches none (the message lists the available config block names).

    *progress_update_callback* receives the composition narrative (and the
    isolated-plugin subprocess output it forwards); :func:`run_batch_campaign`
    routes it to ``variation.log`` while the host-side pre-flight leaves it ``None``.

    *image_project* / *image_project_tag* select the project this campaign's RoboVAST
    family images resolve from; ``None`` means the process environment's. A run passes
    the campaign's own (see :class:`~robovast.execution.backends.RunOptions`) — the
    pre-flight leaves them unset, since it only counts configs.
    """
    from robovast.common.config_generation import generate_scenario_variations

    campaign_data = generate_scenario_variations(
        variation_file=vast_file, progress_update_callback=progress_update_callback,
        output_dir=output_dir, image_project=image_project,
        image_project_tag=image_project_tag)
    if not campaign_data["configs"]:
        raise CampaignConfigError("No configs found in vast-file")
    if config_filter:
        campaign_data["configs"] = filter_configs_by_name(
            campaign_data["configs"], config_filter)
    return campaign_data


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
                       notifier=None, description="", created_by="", origin=None):
    """Build and run a batch campaign (no ``search:`` block)."""
    vast_dir = os.path.dirname(os.path.abspath(vast_file))
    runs = runs if runs is not None else campaign_config.execution.runs
    campaign_id = campaign_id or campaign_id_for(campaign_config)
    # Resolved before composition, not after: the image family's project is an input to
    # composition (it is what ``family:`` refs resolve against), so the defaulting cannot
    # wait until the backend is picked further down.
    opts = options or RunOptions()

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
            campaign_data = build_campaign_data(
                vast_file, tmp, config_filter,
                progress_update_callback=variation_logger.info,
                image_project=opts.image_project,
                image_project_tag=opts.image_project_tag)
        finally:
            remove_campaign_log_handler(var_handler)

        be = backend or DockerBackend(state=state)
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
            notifier=notifier, description=description, created_by=created_by,
            origin=origin)
        try:
            return controller.run()
        finally:
            store.close()
            _campaign_root = os.path.join(results_dir, campaign_id)
            # After store.close() (campaign.db flushed, which data.db's `runs` table
            # reads) and before _finalize, so the derived data rides the existing upload.
            _finish_campaign(be, _campaign_root, campaign_id, state, opts, notifier)
