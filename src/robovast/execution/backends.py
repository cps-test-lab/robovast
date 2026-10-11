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

The ``KubernetesBackend`` runs each batch as Kubernetes Jobs into a
fixed campaign root (no per-batch campaign-id nesting); the test suite's null backend
runs nothing. Both drive batch and search through the same controller.
"""

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

# Re-exported: config generation and campaign staging raise the same user-error type,
# and the stop-aware helpers raise the stop one, and all of them live in ``common``
# (which the execution layer imports), so the classes themselves have to live there
# too. Every caller keeps importing them from here.
from robovast.common.errors import \
    CampaignConfigError, CampaignStopped  # noqa: F401  # pylint: disable=unused-import
from robovast.common.execution import resolve_robovast_image

logger = logging.getLogger(__name__)


class ShareStopped(Exception):
    """Raised out of an upload's progress callback when its stop scope was set.

    A cancellation, not a failure, and the two must not be conflated: an upload that
    failed says something about the share, while this one says the operator asked. The
    provider contract already routes it -- "raise on failure (the caller treats any
    exception as a failed upload and keeps the controller alive for a retrigger)" -- so
    raising is how a streamed upload is interrupted at all; being its own type is what
    lets the callers report it as the deliberate act it is.

    Carries the object name so whoever handles it can clean up, or say what it left:
    a cancelled stream leaves a truncated archive that lists and downloads exactly like
    a whole one.
    """

    def __init__(self, message: str, object_name: str = ""):
        super().__init__(message)
        self.object_name = object_name


@dataclass
class RunOptions:
    """Per-campaign execution options, handed to the backend with every batch."""
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
    # Concrete refs keyed by container name (and role): the built ones, filled by the build
    # lifecycle before the backend runs, or on a replay every container's recorded digest. A
    # container absent from here uses its declared image, fixed to a digest at launch.
    images: dict = field(default_factory=dict)
    # The sidecar image every pod of this campaign runs, as a digest. Fixed once per campaign
    # before its first pod -- an aux pod's transfer container, or a Job's ``fetch-inputs`` --
    # so every pod of it runs the same bytes; ``None`` until then.
    sidecar_image: str | None = None
    # ``{aux container name: digest}`` for the auxiliary containers composition runs, filled
    # as each is fixed or, on a replay, from the launch record.
    aux_images: dict = field(default_factory=dict)
    # True when this campaign replays a launch record (a retrigger, or an adoption after a
    # service restart): ``images``, ``sidecar_image`` and ``aux_images`` are every image it
    # may run, and one they do not fix is refused rather than resolved -- a tag, a project or
    # the composition cache could otherwise hand a replay bytes its source never ran.
    images_fixed: bool = False
    log_tree: bool = False
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
    # postprocessing (so the share stays minimal/untouched), which the backend streams
    # to the configured share provider. Off by default; a per-campaign option (travels
    # with the options, not the process env) exactly like ``postprocess``.
    upload_to_share: bool = False
    # -- who ends the campaign ------------------------------------------------
    # True when the builders' finish tail is the campaign's **outermost** scope and
    # must therefore publish the terminal phase, stop the heartbeat and send the one
    # notification (see controller.end_campaign). A caller that runs work *after* the
    # builder returned would set it False, since the builder ending the campaign would
    # then report "finished" with no metrics yet. A per-campaign option rather than an
    # env var for the same reason as ``postprocess``: the service drives many campaigns
    # concurrently in one process.
    finalize_phase: bool = True
    # -- a batch's configurations have been staged ----------------------------
    # Called once a batch's configurations are staged and before any of them runs -- the
    # last thing a batch campaign does with the project it was launched from, and so the
    # moment it can stop reading it (see ``CampaignController._on_configs_staged``). A
    # callback rather than a return value because a backend stages inside its own
    # run_batch, and per-campaign rather than per-process because the service drives many
    # campaigns at once. Set by ``CampaignController``, which refuses a value already
    # here rather than dropping it.
    on_configs_staged: Optional[Callable[[], None]] = None


def _scenario_image(execution: dict, options: RunOptions) -> str:
    """The image for the container the scenario runs in.

    One place, because a backend and the exec path both need it and a second copy would
    be free to drift. Sidecars are *not* resolved here: they come from the container
    plan, which is built once from the same ``execution`` mapping.
    """
    from robovast.common.config import SCENARIO_CONTAINER
    containers = execution.get("containers") or {}
    declared = (containers.get(SCENARIO_CONTAINER) or {}).get("image")
    built = (options.images or {}).get(SCENARIO_CONTAINER)
    return resolve_robovast_image(explicit=options.image,
                                  config_image=built or declared,
                                  project=options.image_project,
                                  tag=options.image_project_tag)


def refuse_unimportable(campaign_root: str) -> None:
    """Refuse to write an archive no deployment could ever take back in.

    The share is a one-way door as far as diagnosis goes: an archive missing its frozen
    configuration uploads, lists and downloads exactly like a good one, and only fails
    at the far end -- on somebody else's service, after a full transfer, with an ingest
    refusal and no way to repair the source. Campaigns that die before their config is
    frozen do occur, so this is a real shape and not a hypothetical one.

    Checked by :meth:`ExecutionBackend.share_campaign` rather than in the archive stream:
    that stream is also how a campaign is *downloaded*, and taking a partial campaign's files
    off a service is legitimate. Offering it as an importable campaign is not.
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


class ExecutionBackend(ABC):
    """Runs one batch's jobs; results land at ``<campaign_root>/<config>/<run>/``.

    Results stay keyed by config name / run number regardless of how the backend
    packs or dispatches, so the controller's scoring and the store are unaffected
    by the backend choice.
    """

    @abstractmethod
    def run_batch(self, campaign_data: dict, *, campaign_root: str, batch_tag: str,
                  runs: int, options: RunOptions) -> None:
        """Execute the jobs for ``campaign_data`` into ``campaign_root``.

        ``batch_tag`` (e.g. ``"batch-3"``) namespaces job-level artifacts so
        multiple batches sharing one campaign root do not collide.
        """

    def finalize_campaign(self, campaign_root: str) -> None:
        """Hook called once after the whole campaign completes (store closed).

        Default no-op. The :class:`KubernetesBackend` overrides this to release what the
        cluster held for the campaign -- its node calibration and its place in the queue.
        """

    def read_build_lock(self, image: str) -> dict:  # noqa: ARG002 - backend-specific
        """The build lock inside *image*, for a backend that can read one without a runtime.

        ``{}`` by default. The :class:`KubernetesBackend` overrides it because its controller has no
        container runtime, so the only way to reach the lock is the registry.

        ``{}`` means "could not be read here", never "the image installed nothing" -- see
        ``read_build_manifests``.
        """
        return {}

    def campaign_results_bytes(self, campaign_root: str) -> "int | None":
        """Total bytes this campaign's results occupy.

        Measured once, in the run tail, so that reading the figure later is a field lookup
        rather than a walk of the results: a campaign is displayed far more often than it
        finishes, and enumerating storage per view scales with the campaign while telling
        every viewer the same thing. ``campaign_root`` is the campaign's local results tree.

        ``None`` means the size could not be established, which a reader must render as
        "not recorded" rather than as zero. Best-effort by contract: a campaign's results
        are the deliverable, and failing to measure them must never fail the campaign.
        """
        from robovast.execution.campaign_archive import campaign_source_bytes
        return campaign_source_bytes(campaign_root)

    def preflight_upload_to_share(self) -> None:
        """Validate this backend can honour ``--upload-to-share`` before the campaign runs.

        Called once at campaign start (only when the option is set) so a
        misconfiguration fails *fast and loud* instead of the whole campaign running
        and the upload then silently skipping at the finish tail. The default refuses:
        a backend that has not said where an archive goes cannot deliver one, and
        :meth:`share_campaign` would only say so after the runs. The
        :class:`KubernetesBackend` overrides this to raise :class:`CampaignConfigError`
        when no share provider is configured.
        """
        raise CampaignConfigError(
            f"{type(self).__name__} cannot deliver an archive to a share; drop "
            "--upload-to-share.")

    def share_campaign(self, campaign_root: str, options: "RunOptions",
                       progress_callback=None) -> None:
        """Produce the campaign's "upload-to-share" artifact.

        Called from the controller's finish tail **before** analysis postprocessing,
        so at campaign end the archive is the raw campaign (no derived data) and is
        named as such; a later ``vast share export`` of the same campaign finds
        postprocessing's provenance record there and names it ``postprocessed``. Neither caller
        is told which it is — :func:`~robovast.execution.share_providers.naming.
        campaign_variant` reads it off the directory, so the two cannot disagree.

        *progress_callback* is driven off the bytes read from the campaign, the
        source-side counter. A backend refuses an archive that no deployment could import back
        (``robovast.service.ingest.missing_for_import_in``) before a byte crosses the
        network: such an archive uploads, lists and downloads exactly like a good one
        and fails only at the far end (:func:`refuse_unimportable`).

        The default refuses, as :meth:`preflight_upload_to_share` already did at the
        start of the campaign.
        """
        del campaign_root, options, progress_callback
        self.preflight_upload_to_share()

    def discard_partial_share(self, object_name: str) -> str:
        """Remove what a cancelled or failed upload left; return a note, or ``""``.

        ``""`` by default, for a backend whose writer leaves nothing behind. The
        :class:`KubernetesBackend` overrides this, having put its partial object on
        somebody else's storage.
        """
        del object_name
        return ""

    #: The per-run JUnit report a finished run publishes. Counting these under the
    #: campaign root is what "a run completed" means to the progress poller: the root is
    #: the campaign's durable home, and a run's results are under it the moment the run
    #: has delivered them.
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

        ``None`` -- the default -- is a normal answer rather than a failure: a re-index or
        an import runs with no cluster in reach. The caller records the machine anyway,
        leaving the facts NULL.
        """
        return None

    def count_run_artifacts(self, campaign_id: str,
                            campaign_root: str) -> int | None:
        """Completed per-run artifacts published so far (controller progress poll).

        Returns the cumulative number of finished runs under *campaign_root*, counted as
        the ``test.xml`` files finished runs have written there. That is the base's
        answer because ``campaign_root`` is the campaign's durable home, and a run's
        results are under it as soon as the run delivers them -- a cluster job's uploader
        puts them there before the Job counts as complete. A backend whose runs
        leave results somewhere the root cannot see overrides this; one that genuinely
        cannot introspect answers ``None``, which disables run-level progress entirely,
        so a campaign on it reports a ``progress`` that can never advance --
        indistinguishable from a hang -- and the controller says so in the log rather
        than degrading quietly.

        The poller calls this **concurrently** with :meth:`run_batch`, so it must be
        cheap and read-only. ``campaign_root`` is passed rather than derived because a
        backend may hold no handle on it, and the poller probes this before the first
        batch has run.
        """
        del campaign_id  # the root already identifies the campaign
        try:
            # ``<config_name>/<run_number>/test.xml``. The run number must be numeric,
            # the same convention ``list_run_dirs`` uses, so nothing under a reserved
            # ``_config``/``_jobs``/``_transient`` dir can inflate the count.
            return sum(1 for p in Path(campaign_root).glob(f"*/*/{self.RUN_SENTINEL}")
                       if p.parent.name.isdigit())
        except OSError:
            # The campaign dir may not exist yet when the poller first probes.
            return 0
