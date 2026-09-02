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
from dataclasses import dataclass, field
from pathlib import Path

from robovast.common import prepare_campaign_configs
# Re-exported: config generation and campaign staging raise the same user-error
# type, and they live in ``common`` (which the execution layer imports), so the
# class itself has to live there too. Every caller keeps importing it from here.
from robovast.common.errors import \
    CampaignConfigError  # noqa: F401  # pylint: disable=unused-import
from robovast.common.execution import resolve_robovast_image
from robovast.execution.execution_utils.execute_local import generate_compose_run_script

logger = logging.getLogger(__name__)


class CampaignStopped(Exception):
    """Raised when a batch is abandoned because a cooperative stop was requested.

    A *clean* terminal signal (Ctrl+C on ``vast serve``, the Stop button, an MCP
    stop) — distinct from a genuine failure. Callers set the campaign phase to
    ``"stopped"`` and skip the finish work (result download, postprocessing,
    finalize upload) that would otherwise fail noisily against a torn-down cluster
    tunnel and produce misleading tracebacks.
    """


@dataclass
class RunOptions:
    """Per-run execution options (mostly local docker-compose specific)."""
    gui: bool = False
    start_only: bool = False
    abort_on_failure: bool = False
    # None ⇒ resolve via resolve_robovast_image() (config / the family default); a
    # non-None value is an explicit ``--image``. It addresses the container the scenario
    # runs in — the only one a single ``--image`` flag can mean.
    image: str | None = None
    # The image family's project and tag for THIS campaign, or None for the process
    # environment's. Per-campaign so a dev run can point at another registry without
    # redeploying the service — which drives many campaigns concurrently in one process,
    # where an env var could not distinguish them (same reason as ``postprocess`` below).
    image_project: str | None = None
    image_project_tag: str | None = None
    # Concrete refs for containers whose image was *built*, keyed by container name.
    # Filled by the build lifecycle before the backend runs; a container absent from
    # here uses its declared image verbatim.
    images: dict = field(default_factory=dict)
    log_tree: bool = False
    debug: bool = False
    skip_resource_allocation: bool = True
    # -- chained analysis postprocessing (cluster backend only) --------------
    # Per-campaign, so it must travel with the options rather than through the
    # process environment: the service drives many campaigns concurrently in one
    # process, where an env var could not distinguish them. ``namespace`` falls back
    # to the env when unset. (The conversion scripts now come from a per-campaign
    # ConfigMap built from the driver's own package, so no controller image is needed.)
    postprocess: bool = False
    namespace: str | None = None
    # -- upload-to-share (pre-postprocess minimal snapshot) -------------------
    # When set, the finish tail produces a raw campaign archive *before* analysis
    # postprocessing (so the share stays minimal/untouched): the local backend just
    # writes a tar.gz; the cluster backend streams it to the configured share
    # provider. Off by default; a per-campaign option (travels with the options, not
    # the process env) exactly like ``postprocess``.
    upload_to_share: bool = False
    # -- who ends the campaign ------------------------------------------------
    # True when the builders' finish tail is the campaign's **outermost** scope and
    # must therefore publish the terminal phase, stop the heartbeat and send the one
    # notification (see controller.end_campaign). The local service sets it False:
    # there postprocessing runs *after* the builder returns, so the builder ending the
    # campaign would report "finished" with no metrics yet — the very bug this seam
    # removes. A per-campaign option rather than an env var for the same reason as
    # ``postprocess``: the service drives many campaigns concurrently in one process.
    finalize_phase: bool = True


def _scenario_image(execution: dict, options: RunOptions) -> str:
    """The image for the container the scenario runs in.

    One place, because both entry points into the local lane need it and a second copy
    would be free to drift. Sidecars are *not* resolved here: they come from the
    container plan, which is built once from the same ``execution`` mapping.
    """
    from robovast.common.config import SCENARIO_CONTAINER
    containers = execution.get("containers") or {}
    declared = (containers.get(SCENARIO_CONTAINER) or {}).get("image")
    built = (options.images or {}).get(SCENARIO_CONTAINER)
    return resolve_robovast_image(explicit=options.image,
                                  config_image=built or declared,
                                  project=options.image_project,
                                  tag=options.image_project_tag)


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

    def publish_execution_records(self, campaign_root: str) -> None:
        """Hook called before postprocessing reads the campaign from its durable home.

        The driver writes ``_execution/`` and ``_transient/`` on its own disk, and on a
        lane where that disk is scratch they reach the store only at
        :meth:`finalize_campaign` -- which runs *after* the campaign tail. Postprocessing
        happens in that tail, and where it stages the campaign out of the store rather
        than reading the driver's disk, it is given a campaign missing every file only the
        driver has: ``execution.yaml`` among them, without which metadata generation has
        nothing to say what ran.

        Distinct from :meth:`publish_records`, which publishes ``campaign.db`` alone at
        every batch boundary. These directories hold growing logs, so publishing them per
        batch would re-upload a controller log once per batch to no purpose. Once, when a
        reader that is not this process is about to need them, is the whole requirement.

        Default no-op: on the local lane the driver's disk IS the durable home, so
        postprocessing reads what the driver just wrote.
        """

    def publish_records(self, campaign_root: str) -> None:
        """Hook called whenever the campaign's records change materially.

        Twice, at least: once the ``campaign`` row exists (before any compute is spent),
        and once each batch closes. Records here means the small, derived-from-nothing
        files a reader needs to say what a campaign *is* — ``campaign.db``'s campaign,
        batch and unit rows: its description, who launched it, where its configuration
        came from, and, for a search, every parameter set scored so far.

        Default no-op: on the local lane those records are already in their durable home.
        :class:`KubernetesBackend` overrides it, because there the driver's disk is
        scratch and a record published only at the end is missing from exactly the
        campaigns that did not reach one.

        Deliberately **not** :meth:`finalize_campaign`, which is the other end of the same
        idea. That one publishes the whole campaign root — gigabytes of results — and
        releases the campaign's node calibration; neither is wanted mid-run, and calling
        it per batch would re-upload every result of every earlier batch.

        Best-effort in the implementation, never in the contract: a campaign must not fail
        because a record could not be published, but a lane that means to publish must say
        when it could not.
        """

    def finalize_campaign(self, campaign_root: str) -> None:
        """Hook called once after the whole campaign completes (store closed).

        Default no-op: the local :class:`DockerBackend` already materialises the
        full campaign on disk. The :class:`KubernetesBackend` overrides this to
        publish campaign-level artifacts (``campaign.db``, ``_execution/``) to
        storage, so the bucket holds a complete, local-equivalent campaign.
        """

    def ensure_campaign_root_complete(self, campaign_root: str) -> None:
        """Hook called before anything needs the campaign's *whole* directory on disk.

        Default no-op: the local :class:`DockerBackend` writes every artifact straight into
        ``campaign_root``, so it is never incomplete. The :class:`KubernetesBackend` overrides
        this because a campaign it *resumed* starts with only its control plane -- resume runs
        before the service can answer at all, so it takes what it needs to re-enter the
        campaign and leaves the artifacts (see ``campaign_resume``).

        Called from the run tail immediately before postprocessing, which is the first thing
        that reads the whole tree: adoption reads only ``test.xml``, run counts come from the
        store's own table rather than a directory walk, and a resumed search replays its
        earlier evaluations out of ``campaign.db`` instead of re-extracting them. Cheap to
        call when the tree is already whole -- the fetch skips same-size files -- so callers
        that are unsure should call it rather than reason about it.
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

    def share_campaign(self, campaign_root: str, options: "RunOptions",
                       progress_callback=None) -> None:
        """Produce the campaign's "upload-to-share" artifact.

        Called from the controller's finish tail **before** analysis postprocessing,
        so at campaign end the archive is the raw campaign (no derived data) and is
        named as such; a later ``vast share export`` of the same campaign finds
        postprocessing's provenance record there and names it ``postprocessed``. Neither caller
        is told which it is — :func:`~robovast.execution.share_providers.naming.
        campaign_variant` reads it off the directory, so the two cannot disagree.

        The default (local :class:`DockerBackend`) writes the archive into
        ``$ROBOVAST_ARCHIVE_DIR`` or a ``_archives/`` sibling of the campaign dirs
        (kept outside every campaign dir so it can't perturb postprocessing's
        hash-cache) — there is no external share locally, so the file is the
        deliverable. Nothing crosses a network, but writing it still reads the whole
        campaign, so *progress_callback* is driven off the bytes going into the tar —
        the same source-side counter the cluster lane reports, which is what lets one
        reader render both. The :class:`KubernetesBackend` overrides this to stream the
        archive to the configured share provider.
        """
        from robovast.execution import campaign_archive
        from robovast.execution.share_providers.naming import archive_name, campaign_variant
        self._refuse_unimportable(campaign_root)
        results_dir = os.path.dirname(os.path.normpath(campaign_root))
        archive_dir = os.environ.get("ROBOVAST_ARCHIVE_DIR") or os.path.join(
            results_dir, "_archives")
        campaign_id = os.path.basename(os.path.normpath(campaign_root))
        on_member = getattr(progress_callback, "on_member", None)
        if on_member is not None:
            progress_callback.set_source_total(
                campaign_archive.campaign_source_bytes(campaign_root))
        campaign_archive.make_campaign_tarball(
            campaign_root, archive_dir,
            name=archive_name(campaign_id, campaign_variant(campaign_root)),
            on_member=on_member)
        if on_member is not None:
            progress_callback.finish()

    @staticmethod
    def _refuse_unimportable(campaign_root: str) -> None:
        """Refuse to write an archive no deployment could ever take back in.

        The share is a one-way door as far as diagnosis goes: an archive missing its frozen
        configuration uploads, lists and downloads exactly like a good one, and only fails
        at the far end -- on somebody else's service, after a full transfer, with an
        ingest refusal and no way to repair the source. Campaigns that die before their
        config is frozen do occur, so this is a real shape and not a hypothetical one.

        Checked here rather than in ``make_campaign_tarball``: the tarball writer is also
        how a campaign is *downloaded*, and taking a partial campaign's files off a service
        is a legitimate thing to want. It is offering it as an importable campaign that is
        not.
        """
        from robovast.service.ingest import missing_for_import_in
        missing = missing_for_import_in(campaign_root)
        if missing:
            campaign_id = os.path.basename(os.path.normpath(campaign_root))
            raise CampaignConfigError(
                f"Cannot export {campaign_id}: it has no " + " ".join(missing) +
                "\nAn archive written from it could not be imported by any deployment, "
                "including this one, so it is refused here rather than at the far end of "
                "a transfer.")

    #: The per-run JUnit report a finished run publishes. Counting these is what
    #: "a run completed" means to the progress poller, on either lane — the object
    #: store counts keys ending in it, the filesystem counts files named it.
    RUN_SENTINEL = "test.xml"

    def node_facts(self, label: str) -> dict | None:
        """What the machine behind *label* is, or ``None`` if this backend cannot say.

        Answers the hardware half of a run's provenance -- capacity, allocatable, and the
        kernel/OS record -- for a machine identified only by its hashed label. Keyed by the
        label rather than the node's name because the label is all the store ever holds: the
        name was hashed in the pod that wrote it and cannot be recovered here. A backend
        therefore hashes its OWN view of the cluster to answer, which is what keeps the two
        sides honest -- if they ever disagreed this returns ``None`` rather than facts about
        the wrong machine.

        ``None`` -- the default -- is a normal answer rather than a failure: the local lane
        has no nodes, and a re-index or an import runs with no cluster in reach. The caller
        records the machine anyway, leaving the facts NULL.
        """
        return None

    def count_run_artifacts(self, campaign_id: str,
                            campaign_root: str) -> int | None:
        """Completed per-run artifacts published so far (controller progress poll).

        Returns the cumulative number of finished runs visible to the backend, or
        ``None`` when the backend genuinely cannot introspect. Both shipped backends
        count: ``None`` disables run-level progress entirely, so a campaign on such a
        backend reports a ``progress`` that can never advance — indistinguishable from
        a hang — and the controller says so in the log rather than degrading quietly.

        The poller calls this **concurrently** with :meth:`run_batch`, so it must be
        cheap and read-only. ``campaign_root`` is passed rather than derived because a
        backend may hold no handle on it, and the poller probes this before the first
        batch has run.
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
    ``vast workspace run prepare-run`` (staging into a persistent, inspectable
    directory).
    """
    execution = campaign_data.get("execution", {})
    image = _scenario_image(execution, options)
    config_path_result = os.path.join(work_dir, "out_template")
    # gui selects the execution.local.gui parameter overrides, so it has to reach both the
    # staged scenario.config and the packed job documents the local run actually mounts.
    prepare_campaign_configs(config_path_result, campaign_data, gui=options.gui)

    run_script = os.path.join(work_dir, "run.sh")
    generate_compose_run_script(
        runs, campaign_data, config_path_result,
        execution.get("pre_command"), execution.get("post_command"),
        image, results_dir, run_script,
        skip_resource_allocation=options.skip_resource_allocation,
        log_tree=options.log_tree, debug=options.debug, job_prefix=job_prefix,
        gui=options.gui, built_images=options.images)
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
        image = _scenario_image(campaign_data.get("execution", {}), options)

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
            if options.abort_on_failure:
                cmd.append("--abort-on-failure")
            # Always, rather than only when it differs from a default: the default is now
            # computed from the project and this installation's version, so there is no
            # compile-time constant to compare against -- and passing the ref we resolved
            # is what keeps run.sh from re-deriving one of its own.
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

    def count_run_artifacts(self, campaign_id: str,
                            campaign_root: str) -> int | None:
        """Count the ``test.xml`` files finished runs have written under the campaign.

        The local lane writes results straight into ``campaign_root`` as each run
        finishes, so the count is a plain glob — the filesystem counterpart of the
        cluster backend counting object keys with the same sentinel.

        Returning ``None`` ("results are already on disk") switches the controller's
        progress poller off entirely: a live local campaign reports ``batch_runs_total:
        0`` and a ``progress`` that never moves, so a wedged pilot and a working one are
        indistinguishable — and that ``0/0`` reaches the durable ``outcome.json`` of
        campaigns that passed.
        """
        del campaign_id  # the root already identifies the campaign on this lane
        try:
            # ``<config_name>/<run_number>/test.xml``. The run number must be numeric,
            # the same convention ``list_run_dirs`` uses, so nothing under a reserved
            # ``_config``/``_jobs``/``_transient`` dir can inflate the count.
            return sum(1 for p in Path(campaign_root).glob(f"*/*/{self.RUN_SENTINEL}")
                       if p.parent.name.isdigit())
        except OSError:
            # The campaign dir may not exist yet when the poller first probes.
            return 0

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
