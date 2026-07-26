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

"""Execution backends for the campaign controller.

A backend runs **one batch** of jobs and is otherwise dumb: the
:class:`~robovast.execution.controller.CampaignController` owns the campaign
lifecycle (campaign id, results layout, store, the batch loop and scoring); a
backend only dispatches a batch's jobs so results land at
``<campaign_root>/<config>/<run>/``.

:class:`DockerBackend` is the local backend; it reuses the existing
docker-compose run-script generation but executes each batch **into a fixed
campaign root** (no per-batch campaign-id nesting). A ``KubernetesBackend`` with
the same interface can be added later to drive cluster batch and search through
the same controller.
"""

import logging
import os
import re
import signal
import subprocess  # nosec - invokes the generated, trusted robovast run script
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass

from robovast.common import prepare_campaign_configs
from robovast.common.execution import (DEFAULT_ROBOVAST_IMAGE,
                                       resolve_robovast_image)
from robovast.execution.execution_utils.execute_local import \
    generate_compose_run_script

logger = logging.getLogger(__name__)


class CampaignStopped(Exception):
    """Raised when a batch is abandoned because a cooperative stop was requested.

    A *clean* terminal signal (Ctrl+C on ``vast serve``, the Stop button, an MCP
    stop) — distinct from a genuine failure. Callers set the campaign phase to
    ``"stopped"`` and skip the finish work (result download, postprocessing,
    finalize upload) that would otherwise fail noisily against a torn-down cluster
    tunnel and produce misleading tracebacks.
    """


class CampaignConfigError(Exception):
    """Raised when the campaign cannot start because of bad user input.

    A typo'd ``--config`` filter, an empty vast-file, etc. — a user error, not a
    bug. The message is self-contained and actionable (it lists the available
    config names), so callers surface it as ``phase=failed`` *without* an
    accompanying stack trace, which would only be noise here.
    """

    # Read by failure_detail(): a clean user error carries no traceback into the
    # durable failure record, matching how the worker already logs it.
    include_traceback = False


@dataclass
class RunOptions:
    """Per-run execution options (mostly local docker-compose specific)."""
    gui: bool = False
    start_only: bool = False
    network_host: bool = False
    abort_on_failure: bool = False
    # None ⇒ resolve via resolve_robovast_image() (config / ROBOVAST_IMAGE / default);
    # a non-None value is an explicit ``--image`` and wins over everything.
    image: str | None = None
    log_tree: bool = False
    debug: bool = False
    skip_resource_allocation: bool = True
    # -- chained analysis postprocessing (cluster backend only) --------------
    # Per-campaign, so it must travel with the options rather than through the
    # process environment: the service drives many campaigns concurrently in one
    # process, where an env var could not distinguish them. ``namespace`` /
    # ``controller_image`` are process-level for the service and fall back to the
    # env / resolve_controller_image() when unset.
    postprocess: bool = False
    namespace: str | None = None
    controller_image: str | None = None
    # -- upload-to-share (pre-postprocess minimal snapshot) -------------------
    # When set, the finish tail produces a raw campaign archive *before* analysis
    # postprocessing (so the share stays minimal/untouched): the local backend just
    # writes a tar.gz; the cluster backend streams it to the configured share
    # provider. Off by default; a per-campaign option (travels with the options, not
    # the process env) exactly like ``postprocess``.
    upload_to_share: bool = False


class ExecutionBackend(ABC):
    """Runs one batch's jobs; results land at ``<campaign_root>/<config>/<run>/``.

    Results stay keyed by config name / run number regardless of how the backend
    packs or dispatches, so the controller's scoring and the store are unaffected
    by the backend choice.
    """

    @abstractmethod
    def run_batch(self, campaign_data: dict, *, campaign_root: str, batch_tag: str,
                  runs: int, options: RunOptions, whole_campaign: bool = False) -> None:
        """Execute the jobs for ``campaign_data`` into ``campaign_root``.

        ``batch_tag`` (e.g. ``"batch-3"``) namespaces job-level artifacts so
        multiple batches sharing one campaign root do not collide.

        ``whole_campaign`` marks this batch as the entire campaign (batch mode,
        no other batches share the prefix), letting a backend that stages results
        through object storage fetch them in one shot rather than per config.
        """

    def finalize_campaign(self, campaign_root: str) -> None:
        """Hook called once after the whole campaign completes (store closed).

        Default no-op: the local :class:`DockerBackend` already materialises the
        full campaign on disk. The :class:`KubernetesBackend` overrides this to
        publish campaign-level artifacts (``campaign.db``, ``_execution/``) to
        storage, so the bucket holds a complete, local-equivalent campaign.
        """

    def preflight_upload_to_share(self) -> None:
        """Validate this backend can honour ``--upload-to-share`` before the campaign runs.

        Called once at campaign start (only when the option is set) so a
        misconfiguration fails *fast and loud* instead of the whole campaign running
        and the upload then silently skipping at the finish tail. The default (local
        :class:`DockerBackend`) always can — it writes a tar.gz to the archive dir, no
        external provider needed. The :class:`KubernetesBackend` overrides this to
        raise :class:`CampaignConfigError` when no share provider is configured.
        """

    def share_campaign(self, campaign_root: str, options: "RunOptions") -> None:
        """Produce the pre-postprocess "upload-to-share" artifact for this campaign.

        Called from the controller's finish tail **before** analysis postprocessing,
        so the archive is the raw campaign (no derived data). The default (local
        :class:`DockerBackend`) writes ``<archive_dir>/<campaign>.tar.gz`` — there is
        no external share locally, so the file is the deliverable. ``archive_dir`` is
        ``$ROBOVAST_ARCHIVE_DIR`` or a ``_archives/`` sibling of the campaign dirs
        (kept outside every campaign dir so it can't perturb postprocessing's
        hash-cache). The :class:`KubernetesBackend` overrides this to stream the
        archive to the configured share provider instead.
        """
        from robovast.execution import campaign_archive
        results_dir = os.path.dirname(os.path.normpath(campaign_root))
        archive_dir = os.environ.get("ROBOVAST_ARCHIVE_DIR") or os.path.join(
            results_dir, "_archives")
        campaign_archive.make_campaign_tarball(campaign_root, archive_dir)

    def count_run_artifacts(self, campaign_id: str) -> int | None:
        """Completed per-run artifacts published so far (controller progress poll).

        Returns the cumulative number of finished runs visible to the backend, or
        ``None`` when the backend can't introspect (the local
        :class:`DockerBackend`, whose results are already on disk). The
        controller's run-level progress poller calls this **concurrently** with
        :meth:`run_batch`, so it must be cheap and read-only.
        """
        return None


def _sanitize(tag: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", tag)


def stage_run_script(campaign_data: dict, work_dir: str, runs: int,
                     options: "RunOptions", *, job_prefix: str = "",
                     results_dir: str = "") -> str:
    """Prepare a batch's configs + ``run.sh`` under ``work_dir`` (no execution).

    Writes ``work_dir/out_template/`` (the prepared config tree) and a
    ``work_dir/run.sh`` docker-compose runner whose baked default results dir is
    ``results_dir/<campaign-id>``. Returns the ``run.sh`` path. Shared by
    :class:`DockerBackend` (staging into a temp dir, then running) and
    ``prepare-run`` (staging into a persistent, inspectable directory).
    """
    execution = campaign_data.get("execution", {})
    image = resolve_robovast_image(required=True, explicit=options.image,
                                   config_image=execution.get("image"))
    config_path_result = os.path.join(work_dir, "out_template")
    prepare_campaign_configs(config_path_result, campaign_data)

    run_script = os.path.join(work_dir, "run.sh")
    generate_compose_run_script(
        runs, campaign_data, config_path_result,
        execution.get("pre_command"), execution.get("post_command"),
        image, results_dir, run_script,
        skip_resource_allocation=options.skip_resource_allocation,
        log_tree=options.log_tree, debug=options.debug, job_prefix=job_prefix)
    return run_script


class DockerBackend(ExecutionBackend):
    """Local backend: run a batch via docker compose into the campaign root.

    Reuses :func:`generate_compose_run_script`, but invokes the generated script
    with ``--campaign-dir <campaign_root>`` so the batch writes directly into the
    campaign root (the controller owns the campaign id). The simulator-side
    ``entrypoint.sh`` is unchanged.

    A ``state`` (:class:`~robovast.execution.control_server.ControllerState`) makes
    ``stop`` effective: ``run.sh`` loops over runs in a single subprocess, so
    killing one scenario container only fails that run — the loop starts the next.
    When ``state.stop_requested`` is set we SIGTERM the ``run.sh`` process, which
    fires its own ``SIGTERM`` cleanup trap (``docker compose down``) and exits the
    whole loop.
    """

    #: How often to check ``stop_requested`` while a batch subprocess runs.
    _STOP_POLL_SECONDS = 0.5
    #: Grace period for ``run.sh``'s cleanup trap before escalating to SIGKILL.
    _STOP_GRACE_SECONDS = 15

    def __init__(self, state=None):
        self._state = state

    def run_batch(self, campaign_data: dict, *, campaign_root: str, batch_tag: str,
                  runs: int, options: RunOptions, whole_campaign: bool = False) -> None:
        # whole_campaign is a cluster-only hint (single-shot result fetch); the
        # local backend already writes results straight into campaign_root.
        del whole_campaign
        os.makedirs(campaign_root, exist_ok=True)
        image = resolve_robovast_image(
            required=True, explicit=options.image,
            config_image=campaign_data.get("execution", {}).get("image"))

        # Stage the prepared configs + run.sh in a temp dir (not the results dir);
        # run.sh copies out_template into the campaign root, so only results +
        # campaign metadata remain there. The temp dir is removed afterwards.
        with tempfile.TemporaryDirectory(prefix=f"robovast_{_sanitize(batch_tag)}_") as work_dir:
            run_script = stage_run_script(
                campaign_data, work_dir, runs, options,
                job_prefix=batch_tag, results_dir=campaign_root)

            cmd = [run_script, "--campaign-dir", os.path.abspath(campaign_root)]
            if not options.gui:
                cmd.append("--no-gui")
            if options.start_only:
                cmd.append("--start-only")
            if options.network_host:
                cmd.append("--network-host")
            if options.abort_on_failure:
                cmd.append("--abort-on-failure")
            if image != DEFAULT_ROBOVAST_IMAGE:
                cmd.extend(["--image", image])

            logger.info("Launching batch %s: %s", batch_tag, " ".join(cmd))
            # NOT check=True: in a failure-finding run, scenario runs are *meant*
            # to fail and a non-zero exit is the signal; the controller reads the
            # per-config results either way. --abort-on-failure changes the
            # script's own behaviour, not ours.
            returncode = self._run_watching_stop(cmd)
        if returncode != 0:
            logger.warning(
                "Batch %s run script exited with code %d (some runs failed); "
                "continuing to evaluate produced results.", batch_tag, returncode)

    def _run_watching_stop(self, cmd) -> int:
        """Run *cmd* to completion, terminating it if ``stop_requested`` is set.

        The run script is launched in its own session (process group) so a stop
        can SIGTERM it — firing its cleanup trap — and, if that hangs, SIGKILL the
        whole group as a backstop.

        Because the script runs in its *own* session it is not in the terminal's
        foreground process group, so a terminal Ctrl+C never reaches it directly.
        We forward the ``SIGINT`` to the script's session ourselves so its
        ``handle_sigint`` trap runs exactly as when it ran in the foreground:
        first press → graceful ``docker compose`` shutdown, repeats → force exit.
        """
        # nosec - generated, trusted run script
        proc = subprocess.Popen(cmd, start_new_session=True)  # pylint: disable=consider-using-with
        stopped = False
        interrupted = False
        returncode = None
        while returncode is None:
            try:
                returncode = proc.wait(timeout=self._STOP_POLL_SECONDS)
            except subprocess.TimeoutExpired:
                if not stopped and self._state is not None and self._state.stop_requested:
                    stopped = True
                    self._terminate(proc)
            except KeyboardInterrupt:
                interrupted = True
                self._forward_sigint(proc)
        if interrupted:
            # Ctrl+C tore the batch down — propagate so the command exits instead
            # of proceeding to evaluate a half-finished campaign.
            raise KeyboardInterrupt
        return returncode

    def _forward_sigint(self, proc: "subprocess.Popen") -> None:
        """Relay Ctrl+C to the run script's session so its SIGINT trap fires."""
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGINT)
        except (ProcessLookupError, OSError) as e:
            logger.debug("could not forward SIGINT (process gone?): %s", e)

    def _terminate(self, proc: "subprocess.Popen") -> None:
        """SIGTERM the run script (its trap cleans up), SIGKILL the group if it hangs."""
        logger.info("Stop requested — terminating batch run script (pid %d)", proc.pid)
        try:
            proc.terminate()  # SIGTERM to run.sh → 'trap cleanup; exit 130'
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=self._STOP_GRACE_SECONDS)
            return
        except subprocess.TimeoutExpired:
            logger.warning("Run script did not exit after SIGTERM; killing process group")
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError) as e:
            logger.debug("process group kill failed (already gone?): %s", e)
