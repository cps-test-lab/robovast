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

"""The one RoboVAST operation interface — the single source of truth.

Every RoboVAST operation is defined here once as an abstract method plus its
pydantic request/response models. Three bindings mirror this contract 1:1:

* the **service** HTTP endpoints (:mod:`robovast.service.app`) call an
  implementation of :class:`RobovastInterface`;
* the **client** (:class:`robovast.service.client.RobovastClient`) implements it
  over a transport (in-process for local Docker, HTTP for a remote/cluster
  service);
* the **MCP tools** and **``vast`` CLI commands** are thin wrappers over the
  client.

Campaign **status** reuses :class:`robovast.client.status.Status`
verbatim — the same model the per-campaign controller already serves over its
``/status`` channel and the ``vast ... monitor`` command already consumes — so
the persistent service is a superset of the existing control channel, not a new
vocabulary.

This module is intentionally dependency-light (only ``pydantic`` + the existing
``Status`` model) so it imports cleanly in any binding. Phase 0 defines the
campaign-lifecycle + version operations; workspace, postprocessing, and data
operations extend :class:`RobovastInterface` in later phases.
"""

from abc import ABC, abstractmethod
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from robovast.client import file_address
# Reused verbatim — the controller's live status model. (The old ``Command`` /
# ``CommandResult`` RPC envelopes are gone: the controller runs in-process now, so
# ``stop`` is a direct call rather than an HTTP command to a controller pod.)
from robovast.client.scene_markers import ConfigViewContribution, SceneMarker  # noqa: F401  # pylint: disable=unused-import
from robovast.client.status import Phase, Status  # noqa: F401

# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

#: Character cap on a campaign description (:attr:`CreateCampaignRequest.description`).
#: One line in a campaign listing, not a notebook — anything longer belongs in the
#: ``.vast`` itself, which is archived with the campaign.
DESCRIPTION_MAX_LEN = 200


class CreateCampaignRequest(BaseModel):
    """Start a campaign from a workspace's current project.

    ``backend`` is normally **absent**: for a single-backend service it is
    implicit in *which* service the client is talking to (an in-process/local
    ``vast serve`` uses Docker; an in-cluster service uses Kubernetes), so every
    service ignores the field: one service runs one lane, chosen by
    ``vast serve --backend``. Retained only so an older client's request still
    parses; ``None`` is the only meaningful value.
    """

    workspace_id: str
    config_path: str = ""            # which .vast to run (workspace-relative); "" = the one .vast
    config_filter: str = ""          # optional glob to run only matching configs
    campaign_name: str = ""          # override the campaign name (id = <name>-<timestamp>); "" = metadata.name
    # Free text about *this* launch ("pilot: 5 reps, DWB vs MPPI"), recorded in the
    # campaign's store and shown in listings. Capped so it stays a listing-sized label
    # rather than a place to put notes; the id-shaped `campaign_name` cannot carry it.
    description: str = Field("", max_length=DESCRIPTION_MAX_LEN)
    #: Who says they launched this. **Filled in by the service** from the caller's
    #: ``X-Robovast-User`` header, not by the client — a client-supplied value here
    #: would be a second, competing answer to a question the request already answers.
    #: Self-declared either way: with one shared secret nobody can prove who they are.
    created_by: str = Field("", max_length=64)
    runs: int = 1                    # runs per configuration
    #: Launch even though a container names an image nobody could later identify.
    #:
    #: The refusal exists because that one field makes the whole campaign untraceable, and the
    #: gap surfaces a year later when it is too late to ask. An override is offered anyway, and
    #: recorded on the campaign, because a hard wall with no escape gets worked around in ways
    #: that leave no trace at all -- whereas an exemption that is written down can be found.
    allow_opaque_image: bool = False
    postprocess: bool = True         # trigger analysis postprocessing once when done
    upload_to_share: bool = False    # stream a raw (pre-postprocess) archive to the share
    #: Put the simulator's window on the serve host's X display. Honoured **only** by a
    #: local ``vast serve`` on its Docker lane, which is the only deployment whose
    #: ``docker`` process sits at a screen; every other lane refuses the request rather
    #: than running windowless (see :mod:`robovast.service.host_display`). Named for the
    #: effect rather than after the internal ``RunOptions.gui`` it sets, because a client
    #: reads this field without the run machinery in front of it.
    show_gui: bool = False
    #: Which project (registry/namespace) and tag this campaign's **RoboVAST family**
    #: images come from — ``""`` for the service's own default. Per campaign, and on the
    #: request rather than in the service's environment, because that is what makes
    #: "run this against my dev images" a launch decision instead of a redeployment: the
    #: images are resolved service-side, so a client that could only set an env var could
    #: not reach them at all without rolling the service.
    #:
    #: Affects **only** images RoboVAST publishes. A container whose ``image`` the
    #: ``.vast`` states is run exactly as written — that field is for the campaign's own
    #: images, and silently redirecting one would launch something nobody named.
    image_project: str = ""
    image_project_tag: str = ""


class CampaignRef(BaseModel):
    """Identifies a launched campaign. Self-contained and workspace-independent."""

    campaign_id: str
    #: Present when the launch was accepted but something about it will not do what the
    #: caller probably meant — currently only ``show_gui`` on a project that declares no
    #: ``execution.local.gui.parameter_overrides``, whose scenario then still runs
    #: headless. Not an error: a scenario may open its window unconditionally, so
    #: refusing would be wrong. But silence would leave "I asked for a window and got
    #: none" indistinguishable from a broken display.
    note: str = ""


class BuildImageRequest(BaseModel):
    """Build the derived images a workspace project's containers declare.

    Every entry in ``execution.containers`` that adds ``system_packages`` or
    ``python_packages`` is built on top of its ``image``; one that adds nothing is
    pulled as-is and never built. So this is zero or more images, tagged by container
    name — not one "experiment image", and not tied to any particular role.

    The declarative content lives in the ``.vast``; this request only names the
    project, so the client stays free of any registry knowledge.
    Idempotent/content-addressed: if an image for the same inputs already exists it is
    reused (no build runs).
    """

    workspace_id: str
    config_path: str = ""            # which .vast (workspace-relative); "" = the sole .vast
    #: Which container's image to build, when more than one adds packages
    #: (``scenario`` / ``simulation`` / ``sut``, or an ad-hoc container's name).
    #: Omit to build every one of them.
    container: Optional[str] = None


class ImageBuildRef(BaseModel):
    """Identifies a launched (or cache-hit) image build."""

    build_id: str
    tag: str = ""                    # the container whose image this is
    #: True only when EVERY build this request covers was a cache hit — see
    #: :attr:`cached_builds`. One container's hit says nothing about another's.
    cached: bool = False
    #: Every build this request started, as ``{container: build_id}``. A campaign may
    #: build several images, and :attr:`build_id` names only one of them — poll the
    #: rest through here rather than assuming "the image" is a single thing.
    builds: dict = {}
    #: Per-container cache verdict, ``{container: bool}``. :attr:`cached` is the
    #: conjunction of these; this is the field that answers "is the image I care about
    #: built?" for a request that started several builds. It was previously only the
    #: primary container's verdict, reported as the whole request's, which announced
    #: "nothing to wait for" while a sibling build was still running.
    cached_builds: dict = {}


class ImageBuildError(BaseModel):
    """Structured, LLM-actionable build failure.

    Registry-free by construction: never carries an endpoint, credential, or
    registry-qualified ref (see the zero-registry-knowledge invariant).
    """

    #: base-pull | base-image | apt | pip | source-build | push | resource | validate |
    #: builder-pod
    #:
    #: ``base-image`` is distinct from ``base-pull``: the image was fetched fine, it
    #: simply does not contain something the project's own packages depend on.
    #:
    #: ``builder-pod`` is not about the image being built at all: the *builder* could not
    #: start (its own image could not be pulled, or no node could take it), so there is no
    #: build output to classify. Distinct from every other value here because it is the one
    #: that must not send a reader to the ``build:`` section.
    phase: str = ""
    #: ``agent`` — the failure maps to a field in the ``.vast`` the agent can change;
    #: ``infra`` — server-side (base pull, registry push, a builder pod that cannot
    #: start), not agent-fixable.
    #:
    #: Which field is named by ``phase`` + ``message``, and it is not always under
    #: ``build:`` — a ``base-image`` failure is fixed by re-pinning
    #: ``execution.containers.<name>.image``.
    fixable_by: str = "agent"
    entry: str = ""                  # the offending build: entry, when identifiable
    message: str = ""                # one-line summary
    log_tail: str = ""               # last lines of builder output for context


class ImageBuildStatus(BaseModel):
    """Live status of an image build (poll like a campaign's :class:`Status`)."""

    build_id: str
    tag: str = ""
    #: pending | validating | building | pushing | blocked | succeeded | failed | cached
    #:
    #: ``blocked`` is in-flight, not terminal: the build's pod cannot start (image pull,
    #: unschedulable) and Kubernetes may still resolve it. It is reported rather than waited
    #: out silently so a caller learns the reason -- carried in ``error`` -- on its first
    #: poll; it becomes ``failed`` if it has not cleared shortly after the pod appeared. The
    #: same vocabulary a job uses, for the same situation (see :class:`JobSummary`).
    phase: str = "pending"
    done: bool = False
    cached: bool = False             # early-exit: image for these inputs already existed
    #: SYMBOLIC ``build:<tag>`` only — never a registry-qualified ref.
    image_ref: str = ""
    digest: str = ""                 # optional short digest for provenance (not a ref)
    #: The current diagnosis, which a non-terminal ``blocked`` phase also carries -- a
    #: status saying only "blocked" would repeat the complaint this field exists to answer.
    error: Optional[ImageBuildError] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    #: What this build carried and where it looked for layers. Both are fixed costs that
    #: are invisible in BuildKit's own output: the context is copied, uploaded and
    #: mirrored back down once per container per build, and the cache ref decides whether
    #: any layer is reused at all. A build that is slow for either reason used to look
    #: exactly like one that was slow for no reason. Zero/empty means "not reported".
    context_bytes: int = 0
    cache_ref: str = ""


class ExecRequest(BaseModel):
    """Run one command in the experiment image — a diagnostic, never a campaign.

    Names exactly one source of the image (and, with ``config_name``, of a staged
    configuration): a workspace project, or an existing campaign whose ``_config/`` is
    itself a project. A running campaign's container is deliberately not addressable;
    see :meth:`RobovastInterface.exec_in_container`.

    Carries **no timeout field**, so no client can set one: the limit is derived from
    what is being run (``execution.timeout`` for a scenario, a fixed cap for a command)
    and reported back in :class:`ExecResult`.
    """

    #: Shell command. Empty means "run the staged config's scenario as the campaign
    #: would" and requires ``config_name``; empty with no config is an error.
    command: str = ""
    workspace_id: str = ""
    config_path: str = ""            # which .vast (workspace-relative); "" = the sole .vast
    campaign_id: str = ""            # a campaign as *config source* — never a container to attach to
    #: Omitted always means the bare image; a config is never inferred, not even when
    #: the project has exactly one.
    config_name: str = ""
    keep_alive: bool = False         # hold the one container open for follow-up calls
    #: Put the simulator's window on the serve host's X display — same restriction as
    #: :attr:`CreateCampaignRequest.show_gui`. Part of the held container's *identity*:
    #: the X11 mount can only be established when the container is created, so changing
    #: this replaces the container rather than exec'ing into a mount-less one.
    show_gui: bool = False
    #: Which container to run in: ``scenario`` (the default -- where the scenario runs,
    #: and the only container a campaign without a simulator has), ``simulation``,
    #: ``sut``, or an ad-hoc container's name. The names are the same ones a scenario's
    #: ``remote("ipc:///ipc/<name>")`` uses, so there is one vocabulary to learn.
    container: str = ""
    #: No ``tail`` here: like the three log operations, this returns the captured text
    #: and the *reading* surface trims it (the MCP tool via ``log_view.view_log``), so a
    #: CLI caller still gets everything.


class ExecContainerState(BaseModel):
    """State of the single exec container, as of one call.

    Also embedded in :class:`ResourceUsage`, so a caller that finds the lane full can
    attribute the shortfall to its own held container instead of guessing.
    """

    kept: bool = False               # container left running for follow-up calls
    #: False on a ``keep_alive`` call means a *fresh* container — the previous one, and
    #: anything running in it, is gone. Load-bearing: an agent that assumed its stack
    #: survived would misread every later observation.
    reused: bool = False
    image: str = ""
    config: str = ""                 # staged config name; "" for a bare-image container
    #: Seconds until the idle reap. Counts only while no process this tool started is
    #: still alive; ``None`` while something is running.
    idle_expires_in_s: Optional[int] = None
    deadline_in_s: Optional[int] = None   # seconds until the hard stop


class ExecResult(BaseModel):
    """What one :class:`ExecRequest` produced. No campaign, no provenance, nothing durable."""

    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_s: float = 0.0
    limit_s: int = 0
    #: Which rule set ``limit_s`` — ``command`` (fixed cap), ``execution.timeout`` (the
    #: project's own value), or ``default`` (the project set none). Distinguishing the
    #: last two is what makes a truncation self-explaining.
    limit_source: str = ""
    #: Where a started scenario's output went *inside the container*. Set only for an
    #: empty command, whose output ``entrypoint.sh`` redirects to a file rather than
    #: stdout; read it with a follow-up ``tail`` command.
    log_path: str = ""
    container: ExecContainerState = Field(default_factory=ExecContainerState)


class ExecStopResult(BaseModel):
    """Outcome of :meth:`RobovastInterface.stop_exec_container`.

    Not :class:`ActionResult`: "there was nothing to stop" is an empty result, not a
    failure, and ``ok=False`` would conflate it with one.
    """

    stopped: bool = False
    target: Optional[str] = None     # what was stopped, when something was


class ImageResolution(BaseModel):
    """The concrete image a project's container would run — resolved, nothing started.

    Answers what :class:`ExecRequest`'s own image resolution would pick, without paying
    for a container: pure config resolution (``plan_containers`` / ``resolve_robovast_image``,
    or an image-store lookup for a ``build:`` container), never Docker or Kubernetes. Exists
    so a caller can key a cache by "this image" — e.g. a per-image catalog a container would
    report identically on every call — without running the container just to learn which one
    it is.

    For a built image this is the **registry-free identity**, ``build:<tag>@<hash>``, and not
    the concrete ref the container runs FROM: the concrete form is a local docker tag on one
    lane and a registry-qualified ref on the other, and the second must never reach a client
    (the zero-registry-knowledge invariant). The identity still changes exactly when the
    image changes, which is all a cache key needs, and it reads the same on every lane.
    """

    image: str = ""


class OriginKind(StrEnum):
    """The vocabulary carried by :attr:`CampaignOrigin.kind`.

    A ``StrEnum`` for the same reason as :class:`~robovast.client.status.Phase`: members
    *are* their string value, so the wire format is plain JSON and comparisons against raw
    strings keep working. The field stays typed ``str`` -- this is the *known* vocabulary,
    not a lock on it, so a reader must treat an unfamiliar value as "some other way" rather
    than as an error.
    """

    WORKSPACE = "workspace"   # launched from a workspace's .vast
    RETRIGGER = "retrigger"   # re-run of an earlier campaign, from its frozen _config/


class CampaignOrigin(BaseModel):
    """Where a campaign's configuration came from. **A record, never a link.**

    Campaigns stay workspace-independent: nothing in the execution path reads this back, a
    re-run still relaunches from the frozen ``_config/`` (see
    :mod:`robovast.service.retrigger`), and the workspace named here may have been edited,
    renamed or deleted since -- or, for an ingested campaign, may never have existed on this
    deployment at all. It answers "where did this come from?", which is a fact about the past,
    and not "where do I relaunch it from?", which is always the campaign's own snapshot.

    **The workspace fields name where the configuration ORIGINATES, not what the launch read.**
    For a re-run they are copied from the parent's own origin at launch, so a chain of re-runs
    keeps naming the workspace at its root. Denormalised rather than resolved by walking
    ``from_campaign``, for three reasons: the campaign listing is paginated, so a reader may not
    hold the parent at all; a parent is routinely deleted, and provenance that evaporates with it
    is provenance nobody can rely on; and a chain then costs no recursion.

    :attr:`kind` is the single authority on which shape this is. It is *currently* derivable
    (a non-empty ``from_campaign`` means a re-run), but a reader that derives it instead would
    have to be revisited the first time an origin appears that is neither -- so switch on
    ``kind`` and never on whether ``from_campaign`` is empty.
    """

    #: One of :class:`OriginKind`, as a plain string. Open vocabulary.
    kind: str = ""
    #: The workspace the configuration originates from. ``""`` when it came from none.
    workspace_id: str = ""
    #: That workspace's name when the campaign was launched -- a label from the past, which
    #: may since have been renamed or reused. Carried beside the id because an id is
    #: unreadable and a name alone is not an identity.
    workspace_name: str = ""
    #: The ``.vast`` as launched, relative to the workspace root (so a project holding several
    #: in subdirectories is unambiguous). The campaign's frozen ``_config/`` keeps only the
    #: basename, which is why this is worth recording.
    config_path: str = ""
    #: The campaign this one was re-run from -- the *immediate* parent, not the root of the
    #: chain. Only meaningful for ``kind == "retrigger"``.
    from_campaign: str = ""


class CampaignSummary(BaseModel):
    """One row of :meth:`RobovastInterface.list_campaigns`.

    Campaigns are workspace-independent: this carries no binding to a workspace, and nothing
    here is read back to run anything. It does carry an :class:`CampaignOrigin` *record* of
    where the configuration came from -- a fact about the past, which is a different thing
    from a link (see that class).
    """

    campaign_id: str
    phase: str = Phase.UNKNOWN       # open vocabulary; see the Phase enum
    description: str = ""            # the launcher's free text; "" when none was given
    #: Who *says* they started it. With one shared secret nobody can prove who they are,
    #: so this is a label rather than an identity, and a consumer must not treat it as one.
    #: ``""`` means nobody gave a name -- a different fact from an anonymous someone, which
    #: is why it is not filled in with a placeholder, and why the web UI shows no chip at
    #: all for it rather than one reading "unattributed".
    created_by: str = ""
    #: Where the configuration came from. ``None`` means **not recorded** -- a campaign that
    #: ran before this was kept -- which is a different fact from an origin that is recorded
    #: and empty, and the reason it is never backfilled with a guess. A record, not a link:
    #: see :class:`CampaignOrigin`.
    origin: Optional[CampaignOrigin] = None
    postprocessed: bool = False      # configured postprocessing pipelines have run
    #: How the campaign was run: ``'search'`` (a closed ask/tell loop, one batch per round)
    #: or ``'batch'`` (one batch of enumerated configurations). ``""`` when unrecorded,
    #: which a reader must treat as "not a search" rather than guessing -- an old store may
    #: predate the field. Carried on the listing because it decides how results are *read*:
    #: only for a search is a configuration's batch a meaningful grouping.
    mode: str = ""
    num_runs: int = 0
    num_passed: int = 0
    num_failed: int = 0
    #: Search parameter sets whose configuration could not be built at all (an
    #: unrealizable draw). They never ran, so they are counted apart from the run
    #: tallies above rather than inside them -- but they are counted, because a
    #: campaign that could not compose half of what it proposed must not read as a
    #: campaign that simply proposed less. Always 0 for a batch campaign.
    num_composition_failed: int = 0
    #: Search parameter sets that RAN but produced nothing measurable -- every run lost
    #: to infrastructure rather than to the system under test. Counted apart from
    #: ``num_failed`` for the same reason ``num_killed`` is: those runs say nothing about
    #: the SUT. Their runs ARE inside the run tallies above (they happened); what this
    #: number reports is lost search COVERAGE, which a campaign that scored a fabricated
    #: value instead would hide entirely. Always 0 for a batch campaign.
    num_no_sample: int = 0
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    # A campaign whose runs all passed still *finishes* when a post-run step fails -- the
    # runs are the deliverable -- so the failure has to travel with the listing or it is
    # invisible once the live Status is gone (a service restart drops the tracked entry).
    # The web UI already tries to read these off a summary and falls back to nothing.
    postprocessing_error: str = ""
    share_error: str = ""


class ListCampaignsRequest(BaseModel):
    """Paginated global campaign listing (campaigns are not workspace-scoped)."""

    limit: int = 20
    offset: int = 0


class ListCampaignsResponse(BaseModel):
    campaigns: list[CampaignSummary] = Field(default_factory=list)
    total: int = 0


class JobState(BaseModel):
    """What one **running** job is doing right now, as opposed to what it has logged.

    The sibling of :meth:`RobovastInterface.get_job_log`: that answers "what did it say",
    this answers "where is it". A caller diagnosing a wedged campaign wants the second first
    -- a log tells you what is repeating, not what the simulator is doing while it repeats.

    Every field beyond ``job_name`` is optional and **absent rather than empty** when it could
    not be read, with :attr:`unavailable` saying which and why. That asymmetry is the point: an
    empty world and a world nobody could look at must not render the same, because the first
    reads as "nothing is happening" and would be believed.
    """

    job_name: str
    #: ``running`` -- the only state this is asked about -- or what was found instead.
    status: str = "running"
    #: Which run every section below describes, as ``<config>/<run>``.
    #:
    #: A job is one run on the local lane and may **pack** several on the cluster, run one after
    #: another -- so a packed job has exactly one live run at a time, and this names it. Present
    #: because without it a caller could not tell which of a job's runs it had been told about, and
    #: the sections were free to disagree with each other about that.
    #:
    #: ``None`` for a job whose run cannot be named, which is an unpacked cluster Job: its single
    #: run is what the readers find under ``/out``, and the Job's name is not a run key.
    run: Optional[str] = None
    #: Whatever the campaign's simulator reports about itself: findings, and the last poses
    #: and clock. Shape belongs to the simulator (see
    #: :meth:`~robovast.common.simulators.SimulatorBackend.health_command`), so RoboVAST
    #: passes it through rather than reshaping it into a vocabulary of its own.
    simulator: Optional[dict] = None
    #: Where the scenario has got to: the action executing and the state of every node, as
    #: scenario-execution reports it from the log its own behaviour tree wrote. Passed through
    #: for the same reason ``simulator`` is -- the shape belongs to whoever owns the record.
    #:
    #: The expensive half of this call, and deliberately so: the log holds one line per status
    #: change, so the current tree is a fold over the whole file rather than a tail read. That is
    #: why it is here, asked for when someone wants it, and not in whatever the service polls.
    scenario: Optional[dict] = None
    #: ``{container: {"at": <wall ts>, "processes": [{name, cpu_percent, memory_rss_bytes}]}}`` --
    #: the newest sample the run's own resource monitor has written, per container.
    #:
    #: Here because it answers what neither other section can: a run stuck at 0% CPU is
    #: deadlocked, one at 100% is spinning, and both look identical in a log and in a tree that
    #: still says RUNNING. Per **process** rather than per container, because that names the node
    #: doing it instead of only saying that something is.
    #:
    #: Numbers passed through and never scored, as with the other two: which of the two states is
    #: the wrong one depends on what the run is supposed to be doing, which RoboVAST does not know.
    resources: Optional[dict] = None
    #: One line per source that could not be read, saying which and why. Never a silent gap:
    #: a diagnostic that omits its own failures is one that reports a broken run as a fine one.
    unavailable: list = Field(default_factory=list)


class JobSummary(BaseModel):
    """One execution unit of a campaign's current batch.

    A "job" is whatever the backend fans a batch out into: a single **run** on the
    local Docker backend (sequential, so at most one is ``running``), or a
    **Kubernetes Job** on the cluster backend (which may pack several runs).
    ``job_name`` is the id :meth:`RobovastInterface.get_job_log` takes; ``display_name``
    is an optional human-friendly label (config/run locally, batch/job-index on the
    cluster).
    """

    job_name: str
    # running | pending | waiting | completed | failed | killed | blocked
    status: str = "pending"
    display_name: Optional[str] = None
    # Why a job is in its state, when there is something to say — the Kubernetes reason
    # + message for a ``blocked`` job (e.g. ``"ImagePullBackOff: Back-off pulling image
    # ..."``), or Kueue's own wait message for a ``waiting`` one. ``None`` otherwise.
    detail: Optional[str] = None


class JobCounts(BaseModel):
    """Aggregate job status counts for a campaign's current batch."""

    running: int = 0
    pending: int = 0
    # Jobs queued for cluster capacity (Kueue-suspended, no pod yet). Healthy and
    # expected — every cluster batch starts here — so it is counted apart from both
    # ``pending`` (pod exists, scheduling) and ``blocked`` (needs a human).
    waiting: int = 0
    completed: int = 0
    failed: int = 0
    # Jobs an operator stopped by hand (``stop_job``). Its own tally, and never part of
    # ``failed``: nothing was learned from the run about the system under test, so
    # counting it as a failed trial would put a human decision into the campaign's result.
    killed: int = 0
    # Jobs that cannot start and will not recover on their own (image pull / config
    # error). Distinct from ``failed``: Kubernetes still counts them active, but they
    # make no progress — see ``JobSummary.detail`` for the reason.
    blocked: int = 0
    total: int = 0


class ListJobsResponse(BaseModel):
    """Live per-job list + aggregate counts for one campaign's current batch."""

    jobs: list[JobSummary] = Field(default_factory=list)
    counts: JobCounts = Field(default_factory=JobCounts)


class BatchObjective(BaseModel):
    """One search batch's objective figures.

    ``min``/``max``/``mean`` are RAW and direction-free, so no reader has to know whether the
    campaign maximizes before it can read a field name; ``best_so_far`` is the one figure that
    applies the direction. ``None`` statistics with ``n_scored == 0`` mark a batch that measured
    nothing — a gap, never a zero — and ``best_so_far`` carries forward across it.
    """

    idx: int
    n_units: int = 0
    #: Units that produced the objective. Below ``n_units`` when a cell ran but nothing it
    #: produced was measurable, which is a coverage loss rather than a result.
    n_scored: int = 0
    min: Optional[float] = None
    max: Optional[float] = None
    mean: Optional[float] = None
    best_so_far: Optional[float] = None


class SearchHistory(BaseModel):
    """A search's per-batch objective trajectory: how the objective MOVED, not just its best.

    Answers "is this search still improving?", which the single ``Status.best_objective``
    cannot. Read from ``campaign.db`` through the record directory, so it is live during a run
    on both lanes and still there for a finished campaign after a service restart.

    ``unavailable`` is set instead of returning an empty ``batches`` list, because an empty list
    reads as "measured, and there was nothing": ``batch_mode`` (not a search), ``multi_objective``
    (more than one objective, so there is no scalar column to trend — the values are in
    ``objectives_json``), ``no_store`` (nothing readable).
    """

    objective_name: Optional[str] = None
    direction: Optional[str] = None
    batches: list[BatchObjective] = Field(default_factory=list)
    unavailable: Optional[str] = None


class ActionResult(BaseModel):
    """Generic ``ok``/``message`` result for state-changing actions."""

    ok: bool = True
    message: Optional[str] = None


class PostprocessingInfo(BaseModel):
    """The campaign's ``results_processing.postprocessing`` entries."""

    campaign_id: str
    entries: list = Field(default_factory=list)


class UpdatePostprocessingRequest(BaseModel):
    campaign_id: str
    entries: list


class PostprocessingRevision(BaseModel):
    campaign_id: str
    entries: list = Field(default_factory=list)


class PanelsSource(BaseModel):
    """The run-view ``visualization:`` block as editable YAML text."""

    campaign_id: str
    content: str = ""                # YAML text of the ``visualization:`` block


class UpdatePanelsSourceRequest(BaseModel):
    campaign_id: str
    content: str


class PostprocessingSource(BaseModel):
    """The ``results_processing.postprocessing`` block as editable YAML text.

    The webui rerun dialog edits this text and saves it straight back into the
    campaign's ``_config/<name>.vast`` — the text twin of the structured
    :class:`PostprocessingInfo`."""

    campaign_id: str
    content: str = ""                # YAML text of the ``results_processing:`` block


class UpdatePostprocessingSourceRequest(BaseModel):
    campaign_id: str
    content: str


class RunPostprocessingRequest(BaseModel):
    campaign_id: str
    force: bool = False
    skip: list[str] = Field(default_factory=list)


class RunShareRequest(BaseModel):
    campaign_id: str


class CleanupDataRequest(BaseModel):
    """Which campaign result buckets to delete from the object store.

    The service holds the cluster config (object-store credentials) and knows which
    campaigns are live, so bucket cleanup runs server-side — the CLI never needs
    cluster credentials. ``campaign_id`` None removes **all** finished campaigns
    (live ones are always skipped); a given id removes just that one, and ``force``
    removes it even if the service still considers it live.
    """
    campaign_id: Optional[str] = None
    force: bool = False


class LogChunk(BaseModel):
    """An incremental slice of a campaign's ``controller.log``.

    The controller runs in the driving process, so its log is a local file there
    (the CLI locally, the service for cluster campaigns). Clients poll from a byte
    *offset* and append — ``next_offset`` is where to resume; ``eof`` is True once
    the campaign has reached a terminal phase and no more will be written.
    """
    text: str = ""
    next_offset: int = 0
    eof: bool = False


class VersionInfo(BaseModel):
    """Server/client version for the compatibility handshake (see plan 0.7)."""

    robovast_version: str
    #: The revision the running code was built from, or ``""`` when this deployment cannot
    #: tell. Separate from :attr:`robovast_version` because the two answer different
    #: questions: that one is the semver the compatibility handshake compares, this one
    #: answers "is the change I just made loaded?" — which a service that loads its code
    #: once at startup makes a real question, and which a semver cannot answer because it
    #: stays the same across every edit.
    #:
    #: ``""`` is information, not a gap: it means the answer is unavailable here, so a
    #: caller must not read it as "a revision that does not match". Reporting the version
    #: in its place was the previous behaviour and is precisely what made the field unable
    #: to detect the staleness it exists for.
    code_revision: str = ""
    api_version: str = "0"
    backend: Optional[str] = None    # "docker" | "kubernetes" (informational)

    # -- cluster lane, when there is one ------------------------------------
    # Which cluster a campaign would land in. Reported because the defaults are
    # invisible and each is a different cluster: an unset context means "whatever
    # kubectl points at", which is a property of the host the service happens to run
    # on, not of the service. A campaign started against the wrong one is only
    # discovered by its absence.
    #: Kubeconfig context the cluster lane dispatches into; ``None`` = the active one.
    kube_context: Optional[str] = None
    #: Where ``kube_context`` came from: ``"--context"``, ``"ROBOVAST_KUBE_CONTEXT"``
    #: or ``"active kubeconfig context"``. The implicit case is the one that surprises.
    kube_context_source: Optional[str] = None
    #: Namespace the cluster lane submits Jobs into.
    namespace: Optional[str] = None
    #: True when the service runs *inside* the cluster (it reads the object store
    #: directly); false off-cluster, where campaigns are driven through a kubectl
    #: port-forward that is fragile under large result transfers. The two modes fail
    #: in different ways, so a client diagnosing a stalled transfer needs to know which.
    in_pod: Optional[bool] = None
    #: The API server URL the cluster lane resolves to, so "which cluster is this?"
    #: is answerable without reading the caller's kubeconfig.
    api_server: Optional[str] = None

    # -- can this deployment build an experiment image? ----------------------
    # A campaign whose container adds packages needs somewhere to push the derived image.
    # On the cluster lane that is the service's own in-pod registry, reached over its
    # Ingress — so a service that is unpublished, or whose registry prefix a `setup`
    # re-run dropped, cannot build at all. Nothing said so until a campaign was
    # submitted and refused, after a project push, a workspace create and a launch.
    #
    # **Capability, not liveness.** This answers "does this deployment have the
    # infrastructure a build needs", which is a property of how it was set up. Whether
    # Docker is running right now is a different question, answered by `resource_usage`
    # and by the run preflight; probing it here would make the cheapest call in the
    # interface the slowest.
    #: True when this deployment can build an experiment image; ``None`` when the service
    #: did not say. **A consumer must treat ``None`` as "no verdict" and print nothing**:
    #: a service older than this field leaves it absent, and reading that as ``False``
    #: would tell every healthy pre-field deployment to go and fix itself.
    can_build_images: Optional[bool] = None
    #: Why not, and the commands that fix it. Non-null only when ``can_build_images`` is
    #: False. Never carries a registry host, prefix or credential — registry details do
    #: not cross this interface (see :class:`RegistryConfig`), and this reaches a client.
    build_unavailable: Optional[str] = None

    # -- how to reach files -------------------------------------------------
    #: The address templates, so a caller learns the file address space from the
    #: service rather than from documentation it may not have.
    results_address: str = "/results/{campaign_id}/{path}"
    sources_address: str = "/sources/{workspace_id}/{path}"
    #: Filesystem roots behind those two namespaces — **non-null only when the caller
    #: can actually open them**: the service must be backed by a local filesystem *and*
    #: the request must come from loopback. Then a caller on the same machine reads
    #: files with its own tools instead of relaying every byte through this interface.
    #:
    #: A service with a cluster lane reports both as null. Its results live in the
    #: object store; the ``/tmp/robovast-campaigns`` fetch scratch looks eligible and is
    #: not — it is ephemeral and holds only already-fetched campaigns, so advertising it
    #: would name a path that is right for one campaign and absent for the next.
    #:
    #: ``/results/<campaign_id>/<path>`` is ``<results_root>/<campaign_id>/<path>``.
    #: ``/sources/<workspace_id>/<path>`` is
    #: ``<sources_root>/<workspace_id>/project/<path>`` — **except** for a workspace
    #: reporting ``read_only: true``, which is a directory pinned in place with
    #: ``--workspace-dir`` and therefore lives outside this root.
    results_root: Optional[str] = None
    sources_root: Optional[str] = None
    #: The origin to prefix a route or an address with, so a caller that cannot be handed
    #: bytes can be handed a link instead: ``"https://<ingress-host>"`` on a published
    #: deployment, the bound address for a local ``vast serve``.
    #:
    #: The **service** declares it because only the service knows it. A transport's
    #: ``base_url`` is where *it* dials, which is the same string only by accident, and the
    #: MCP mounted inside the service has no transport at all.
    #:
    #: ``""`` means this deployment has no origin to declare — unpublished, or bound to a
    #: wildcard where no single address is the one a caller used. Information, not a gap:
    #: a consumer omits the link rather than emitting one nobody can open.
    web_base: str = ""


class DiskSpace(BaseModel):
    """Capacity and current use of one filesystem, in bytes.

    One object rather than two optional fields on the caller, because the pair is only
    meaningful whole: a meter drawn from a half-populated reading shows a full or an
    empty disk, and nothing downstream could tell that from a real one.
    """

    capacity_bytes: int
    used_bytes: int


class ResourceUsage(BaseModel):
    """Live compute capacity and current usage of the service's execution backend.

    Backend-neutral by design: the local↔cluster difference is resolved inside the
    service (``LocalTransport`` reads the host via ``psutil``; ``ClusterService``
    reads the Kubernetes nodes), so a consumer — the UI chip or the MCP tool — reads
    the same fields regardless of where it runs and never branches on ``backend``.

    ``cpu_used`` / ``memory_used`` semantics differ by backend but answer the same
    question ("how much is currently claimed"): on the **cluster** they are the sum
    of resource *requests* of the non-terminal pods bound to a node (schedulability,
    matching how Kueue reasons about quota — pods still queued for a node are
    reported by ``jobs_pending``, not here, so ``used`` never exceeds ``capacity``);
    on **local** they are live host utilization. ``cpu_*`` are CPU cores;
    ``memory_*`` are bytes.

    ``disk`` and ``store`` are **actual filesystem bytes on both lanes** -- the one place
    this model does not follow the ``cpu_used``/``memory_used`` pattern. Requests cannot
    answer it: ``ephemeral-storage`` is almost never requested, so a request sum would
    report a few hundred MB used on a node that is 95% full. ``disk`` is the filesystem a
    run writes into (the sum of every node's kubelet-reported *nodefs* on the cluster; the
    campaign results root's filesystem locally); ``store`` is the results store, which on
    the cluster is a different thing from ``disk`` and only some providers can measure. A
    cluster total also cannot show that one node of many is nearly full -- a limitation of
    a summed meter, not of the reading.

    ``parallel_runs`` is a backend-intrinsic flag, **not** a count: ``False`` means
    scenario runs execute one at a time (local Docker is single-flight), ``True``
    means they run in parallel bounded only by free capacity (cluster). How many runs
    actually fit is left to the consumer, which knows each project's per-run
    reservation — the service does not.

    ``jobs_running`` / ``jobs_pending`` are scenario-run counts across every campaign
    this backend is driving, not one. One definition, both lanes: ``running`` is what is
    **executing right now**, ``pending`` is work the backend has **accepted but is not
    executing**. On the cluster that means Kueue-waiting + pod-pending + blocked Jobs;
    locally it is the remainder of the current batch, with ``running`` 0 or 1 because
    the Docker lane is single-flight. So the pair can be read — and summed into an
    "outstanding work" total — without branching on ``backend``.

    The two counts deliberately answer a different question from ``cpu_used`` above:
    they include work that has been accepted but has no compute granted yet, which is
    why counting it as *usage* reported more cores in use than the cluster had.
    ``completed`` and ``failed`` runs are past work and appear in neither; the
    per-campaign :class:`JobCounts` is the finer-grained view (it keeps ``waiting`` and
    ``blocked`` apart, which a capacity meter has no use for).
    """

    backend: str                     # "docker" | "kubernetes" (informational only)
    cpu_capacity: float              # total cores (cluster allocatable / host logical CPUs)
    cpu_used: float                  # cores claimed (cluster pod requests / host utilization)
    memory_capacity_bytes: int
    memory_used_bytes: int
    parallel_runs: bool              # runs execute in parallel? cluster=True, local=False
    jobs_running: int = 0            # scenario-run pods in phase Running, backend-wide
    jobs_pending: int = 0            # scenario-run pods admitted/queued but not yet Running
    #: The filesystem this backend's runs write into: nodefs on the cluster (container
    #: writable layers, emptyDir scratch, pulled image layers -- the disk kubelet's
    #: eviction thresholds watch), the campaign results root locally. **``None`` means "no
    #: verdict"** -- an older service, or a backend whose disk could not be read -- and a
    #: consumer must then show nothing rather than a zero.
    disk: Optional[DiskSpace] = None
    #: The campaign **results store**, when it is separately measurable -- the embedded
    #: object store's volume on a cluster that hosts one. ``None`` on a provider backed by
    #: a cloud bucket (object storage has no capacity to fill, so there is no meter to draw
    #: -- not a failure to draw one), and locally, where the store *is* the filesystem
    #: ``disk`` already reports.
    store: Optional[DiskSpace] = None
    #: Why there is no ``disk``, when the backend tried and failed. Non-null only when
    #: ``disk`` is None *and* the reason is known -- a service too old to have this field
    #: leaves both absent, which reads identically to "did not try". Names counts and the
    #: fixing command, **never a node name**: this string crosses the interface.
    disk_unavailable: Optional[str] = None
    #: The held container-exec container, when one exists. A diagnostic container can
    #: hold a ROS stack's worth of memory, and a caller told only "the lane is full"
    #: has no way to discover that its own container is the reason.
    exec_container: Optional[ExecContainerState] = None


# -- workspaces (editable project inputs; independent of campaigns) ---------


class CreateWorkspaceRequest(BaseModel):
    name: str = ""
    #: Seed the new workspace from this campaign's frozen ``_config/`` — the way from *reading*
    #: a campaign's configuration (``/results/<id>/_config/``, read-only) to *editing* it.
    #: A campaign id rather than a source address, because the copy is not a copy: ``_config/``
    #: stores the scenario at its basename while the ``.vast`` may declare a subdirectory path,
    #: so the tree has to be reconstructed the way a retrigger reconstructs it
    #: (:func:`robovast.service.retrigger.stage_project`). Empty for an empty workspace.
    from_campaign: str = ""


class WorkspaceInfo(BaseModel):
    workspace_id: str
    name: str = ""
    created_at: Optional[str] = None
    #: True for a directory pinned read-only with ``vast serve --workspace-dir``:
    #: used in place, so writes are refused — edit the files on disk instead.
    read_only: bool = False
    #: Campaigns running *right now* out of this workspace. A campaign reads its
    #: project from here for its whole life, so writing to a workspace that has any is
    #: changing a running experiment underneath itself. Live state, not a stored
    #: binding: a finished campaign is workspace-independent (which is why
    #: ``_execution/launch.yaml`` records no ``workspace_id``), and only the service
    #: driving the run can answer this at all. Empty on a service that tracks none.
    running_campaigns: list[str] = Field(default_factory=list)


class ListWorkspacesResponse(BaseModel):
    workspaces: list[WorkspaceInfo] = Field(default_factory=list)


class FileMeta(BaseModel):
    """Result of a write/upload — metadata only, never the content.

    Echoing content back would double its token cost for no benefit.
    """

    #: The address that was written (``/sources/<workspace_id>/<path>``). No separate
    #: ``path``: it is this string's tail, and two spellings of one location is how
    #: they come to disagree.
    address: str
    bytes: int = 0
    sha256: str = ""
    executable: bool = False


class WriteFileRequest(BaseModel):
    """Inline authoring — ``.vast``/``.osc`` only (enforced server-side)."""

    #: ``/sources/<workspace_id>/<path>``. ``/results`` is read-only and refused.
    address: str
    content: str


class EditFileRequest(BaseModel):
    """Old/new-string edit so the validate→fix loop sends a diff, not a file."""

    address: str
    old_string: str
    new_string: str


class FileEntry(BaseModel):
    """One directory entry, when a listing is asked for with ``detail=True``.

    Everything here comes from one ``stat()``. Deliberately no ``sha256``: hashing
    every file to list a campaign would read the whole tree to answer "what is in this
    directory".
    """

    name: str
    is_dir: bool = False
    #: ``None`` where the substrate does not report it (not zero — that is a real size).
    bytes: Optional[int] = None
    modified: Optional[float] = None
    #: Whether the file is executable — the run-script bit, carried end-to-end into the
    #: campaign. ``None`` where the substrate has no such concept (object storage).
    executable: Optional[bool] = None


class FileListing(BaseModel):
    """What is inside one directory of the address space.

    ``entries`` are **strings** by default, directory names suffixed with ``/`` and all
    of them relative to :attr:`address` — which is echoed once so the next address is a
    concatenation rather than a re-derivation. That is a deliberate cost decision: the
    same listing as objects is an order of magnitude more tokens spent describing sizes
    that are rarely the question. ``detail=True`` switches to :class:`FileEntry` objects
    for a caller (the web UI) that renders them.

    ``total`` counts the entries **before** ``offset``/``limit``, so a truncated listing
    still says how much it left out.
    """

    address: str
    entries: list[str] = Field(default_factory=list)
    detailed: list[FileEntry] = Field(default_factory=list)
    total: int = 0
    truncated: bool = False
    recursive: bool = False


class FileText(BaseModel):
    """A page of a text file. Binary files are refused, not mangled."""

    address: str
    total_lines: int = 0
    returned_lines: int = 0
    offset: int = 0
    content: str = ""


class CreateUploadRequest(BaseModel):
    """Grant an HTTP PUT for any file type (keeps bytes out of the token stream)."""

    #: ``/sources/<workspace_id>/<path>`` — the same address space as every other file op.
    address: str
    executable: bool = False


class UploadGrant(BaseModel):
    """A one-time, TTL-scoped upload grant; PUT the bytes to ``url``."""

    token: str
    path: str
    expires_in: int
    url: Optional[str] = None    # absolute when issued by an HTTP service


class StagedArchive(BaseModel):
    """Where an uploaded campaign archive landed on the service host.

    The PUT that streams the bytes deliberately stops here rather than importing: import is
    one op with one report, and making the byte channel a second entrance to it would give
    the same operation two implementations to keep in step.
    """

    path: str
    size: int = 0


class ImportCampaignRequest(BaseModel):
    """Which campaign archive to take in, and from where. Exactly one source.

    Two sources rather than one because the interesting one is the share: having the
    *service* fetch from it is what keeps a multi-gigabyte campaign off the path through
    somebody's laptop, and out of reach of a user whose share credentials are read-only or
    absent. ``archive_path`` is the other end of the same op — a file already on the service
    host, including one just uploaded through
    :meth:`~robovast.service.interface.RobovastInterface.create_archive_upload`.
    """

    #: A path on the **service host**: a staged upload, or a file put there by other means.
    archive_path: str = ""
    #: A campaign id or archive name on the configured share; the service downloads it.
    share_archive: str = ""
    #: Replace a campaign of the same id that is already here. Destructive by construction:
    #: the campaign it replaces is deleted, so it is off by default and asked for per call.
    force: bool = False
    #: Reconstruct ``campaign.db`` from the results tree — the recovery a corrupt store's
    #: stage names.
    rebuild_store: bool = False


class IngestStage(BaseModel):
    """One stage of an import, and what to do about it if it is not ``ok``.

    ``verdict`` is one of ``ok``/``migrated``/``absent``/``degraded``/``newer``/``failed``
    (:mod:`robovast.service.ingest`); only ``failed`` blocks. The remaining fields are
    per-stage and absent where they do not apply, which is why every one is optional.
    """

    verdict: str = ""
    detail: str = ""
    #: Config-version stages: the version found, and the migration steps applied to read it.
    version: Optional[int] = None
    steps: list[str] = Field(default_factory=list)
    #: Store stages: the schema version, how many runs it indexes, and whether the store was
    #: reconstructed from the results tree rather than recorded live by a controller.
    schema_version: Optional[int] = None
    runs: Optional[int] = None
    rebuilt: Optional[bool] = None
    #: What the caller can do about this stage, when there is something.
    recovery: str = ""


class IngestReport(BaseModel):
    """What happened to an imported campaign, per stage.

    ``ok`` is False only when a stage genuinely blocks. A *degraded* import is reported as
    usable-but-incomplete rather than discarded, because throwing away a campaign somebody
    already has in order to keep a boolean clean is the wrong trade.
    """

    campaign_id: str = ""
    ok: bool = False
    #: Names of the stages that block, so a caller can say *which* part failed.
    blocking: list[str] = Field(default_factory=list)
    stages: dict[str, IngestStage] = Field(default_factory=dict)
    #: Where the campaign was extracted, on the service host.
    path: str = ""


class ShareArchive(BaseModel):
    """One campaign archive on the configured share."""

    campaign_id: str = ""
    #: ``raw`` (before postprocessing -- what a campaign-end upload leaves) or
    #: ``postprocessed``. Read from the object's name; an archive written before the name
    #: carried it is raw, which is what it is.
    variant: str = ""
    #: The object's own name on the share, which is what an import names.
    object_name: str = ""
    #: ``-1`` where the provider cannot say.
    size: int = -1
    #: A link a person can open, where the provider has one. Absent for sftp, and for a
    #: webdav share whose URL needs credentials.
    url: Optional[str] = None


class ShareListing(BaseModel):
    """What the share holds, and which provider answered.

    ``configured`` is False when this service has no share at all — a real answer, and a
    different one from an empty share. A client that conflated them would offer to import
    from nowhere, or claim a configured share was empty when it simply could not be read.
    """

    configured: bool = False
    share_type: str = ""
    archives: list[ShareArchive] = Field(default_factory=list)


# -- validation / preview / authoring help (config editor) ------------------


class ValidationProblem(BaseModel):
    """One problem from :meth:`RobovastInterface.validate_project`.

    ``stage`` is the check that failed (``file``/``parse``/``schema``/
    ``scenario``/``generation``/plugin-ref/…); ``config``/``field`` locate it.
    """

    stage: str = ""
    config: Optional[str] = None
    field: Optional[str] = None
    message: str = ""


class ValidationReport(BaseModel):
    """Collect-all validation result (mirrors ``validate_project_file``)."""

    valid: bool = False
    problems: list[ValidationProblem] = Field(default_factory=list)
    configs: int = 0
    runs_per_config: int = 0
    total_trials: int = 0


class RetriggerAxis(BaseModel):
    """One axis of a retrigger pre-flight.

    ``verdict`` is ``ok`` / ``upgradable`` / ``unknown`` / ``blocked``. ``unknown`` is not a
    failure: a campaign recorded before a given field existed is exactly the kind this exists
    to rescue, so it is reported and does not block. ``detail`` must be actionable -- for a
    blocked axis it names the artifact and how to obtain it.

    ``data`` carries the axis's structured findings (the pinned images, the resolved plugins,
    the migration steps), which differ per axis and so are not modelled field by field here --
    a caller that wants them knows which axis it asked about.
    """

    verdict: str = ""
    detail: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class RetriggerReport(BaseModel):
    """Whether a campaign can be re-run, per axis (mirrors ``retrigger.check``).

    Five axes, reported together on purpose: they fail independently, so learning about one at
    a time means fixing the config only to discover the image is gone as well.
    """

    campaign_id: str = ""
    runnable: bool = False
    blocking: list[str] = Field(default_factory=list)
    axes: dict[str, RetriggerAxis] = Field(default_factory=dict)


class MigrationMarker(BaseModel):
    """One decision a migration could not make, and where it is."""

    path: str = ""
    reason: str = ""


class WorkOrder(BaseModel):
    """A campaign materialised as a workspace to finish migrating by hand.

    Returned instead of a campaign id, because nothing was launched: what the caller gets is a file
    with work left in it. ``markers`` is what remains to decide -- and while any of them stands the
    config deliberately will not validate, since a partly-migrated one that loaded would run a
    different experiment than the campaign it came from.
    """

    workspace_id: str = ""
    config_path: str = ""
    reached: int = 0
    capability: str = ""
    markers: list[MigrationMarker] = Field(default_factory=list)


class VariationRemote(BaseModel):
    """Where a variation type's Module-Federation preview bundle is served from.

    Field-for-field what ``local_transport._plugin_remotes`` builds — the container name
    (``REMOTE_NAME``, defaulting to the entry-point name), the ``remoteEntry.js`` URL, and
    the exposed module.
    """

    name: str = ""
    remote_entry_url: str = ""
    module: str = ""


class VariationPreview(BaseModel):
    """One variation's web preview: its type, its resolved params, and its renderer."""

    variation_type: str = ""
    params: dict = Field(default_factory=dict)
    remote: Optional[VariationRemote] = None


class ServedContribution(ConfigViewContribution):
    """A contribution as the service serves it: the geometry, plus what went wrong collecting it.

    ``errors`` is not part of what a *variation* returns -- it is what the collection observed, one
    entry per hook that raised. Reported rather than swallowed: a view missing one variation's
    markers is indistinguishable from a variation that placed nothing.
    """

    errors: list[str] = Field(default_factory=list)


class PreviewConfiguration(BaseModel):
    """One resolved configuration a ``.vast`` expands to."""

    name: str
    #: The scenario parameters, i.e. what the trial is given.
    parameters: dict = Field(default_factory=dict)
    #: The resolved ``sim`` block: the world this configuration runs in and the plugin
    #: overrides on it. A different level from ``parameters`` rather than a subset —
    #: one says what the trial does, the other what it runs in.
    sim: dict = Field(default_factory=dict)
    #: The ``_``-prefixed keys a variation wrote for other readers (``_map_file``,
    #: ``_path``, …). Shown behind a toggle, the way the desktop editor's ``--debug`` did.
    internals: dict = Field(default_factory=dict)
    #: Geometry and files the config view's panels draw. Typed rather than a bare dict so the
    #: generated web-UI types describe a marker; the models are the ones a variation returns.
    contribution: ServedContribution = Field(default_factory=ServedContribution)
    #: Per-variation preview descriptors (Phase 2b — Module Federation remotes /
    #: host-native built-ins); empty when a variation contributes no preview.
    previews: list[VariationPreview] = Field(default_factory=list)


class PreviewResponse(BaseModel):
    """Result of :meth:`RobovastInterface.preview_configurations`."""

    configs: int = 0
    runs_per_config: int = 0
    total_trials: int = 0
    configurations: list[PreviewConfiguration] = Field(default_factory=list)
    truncated: bool = False
    #: The Config tab's third column, from ``visualization.config.panels`` of the same file —
    #: flattened, with a Module-Federation ``remote`` attached to a package-provided panel.
    config_panels: list[dict] = Field(default_factory=list)


class WorldDescription(BaseModel):
    """What a campaign's world offers — :meth:`RobovastInterface.describe_world`.

    The vocabulary inside ``plugins`` and ``overridable`` is the **simulator's**, not
    RoboVAST's: a backend answers in its own terms (roqsim reports geoms and actuators; a
    different simulator would report its own objects) and RoboVAST only fixes the shape. Hence
    plain mappings rather than modeled fields — typing them here would make this the second
    place a simulator's schema is written down, and the two would disagree.

    ``image`` is not decoration. Which world a ref resolves to depends on what is *installed*,
    so this answer is only true for that image — a caller comparing two answers has to know
    whether it is comparing worlds or images.
    """

    #: The simulator backend that answered, the image it was asked in, and what it cost --
    #: seconds, because the first call against a cold image pulls it.
    backend: str = ""
    image: str = ""
    duration_s: float = 0.0
    #: The world as the simulator resolved it, and whether it came from an installed package.
    world: str = ""
    packaged: bool = False
    #: Everything the world is built from (a path world's YAML chain and its MJCF/meshes).
    inputs: list[str] = Field(default_factory=list)
    #: Each plugin under the key an override addresses it by, with the paths that exist.
    plugins: list[dict] = Field(default_factory=list)
    #: The entities the world compiles — ``None`` unless asked for, since it costs a build.
    entities: Optional[list[str]] = None
    #: ``{"fields": [...], "targets": {...}}``: the model values a run may change, and (when a
    #: target glob was given) the objects that can be named with their current values.
    overridable: dict = Field(default_factory=dict)
    #: Transport plugins the simulator left out of the build that answered this — a describe
    #: publishes nothing, so a bridge contributes nothing to it but a way to fail. Reported
    #: because ``entities`` was arrived at without them.
    dropped_transport: list[str] = Field(default_factory=list)
    #: Why a half of the answer is missing, when the simulator could produce only part of it
    #: (``{"build": "..."}`` with ``entities`` left ``None``). Empty when the reply is complete —
    #: and a caller must read it before concluding that a null ``entities`` means the world
    #: compiles none.
    errors: dict = Field(default_factory=dict)


class VariationTypeParam(BaseModel):
    """One parameter of a variation type (from its pydantic model)."""

    name: str
    type: str = ""
    required: bool = False
    default: Optional[object] = None
    description: Optional[str] = None


class VariationTypeInfo(BaseModel):
    """A registered ``robovast.variation_types`` entry point + its params."""

    name: str
    summary: str = ""
    params: list[VariationTypeParam] = Field(default_factory=list)


class VariationTypesResponse(BaseModel):
    types: list[VariationTypeInfo] = Field(default_factory=list)


# -- results data query (eval viewer) ---------------------------------------


class DataTable(BaseModel):
    """One queryable table, as :meth:`describe_campaign_data` reports it."""

    schema_: str = Field("", alias="schema")
    table: str = ""
    columns: list[str] = Field(default_factory=list)
    rows: Optional[int] = None
    description: str = ""
    column_notes: dict = Field(default_factory=dict)

    model_config = {"populate_by_name": True}


class DataDescribe(BaseModel):
    """Schema of a campaign's ``data.db`` (+ attached ``campaign.db``).

    Each ``tables`` entry is ``{schema, table, columns, rows}`` (passed through from
    the query helper verbatim — kept as a dict so ``schema`` stays that key across
    every client path).
    """

    campaign_id: str
    tables: list["DataTable"] = Field(default_factory=list)
    note: str = ""


class DataQueryResult(BaseModel):
    """Rows from a read-only ``query_campaign_data_sql``."""

    campaign_id: str
    columns: list[str] = Field(default_factory=list)
    rows: list[dict] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    # Present when 0 rows matched: distinguishes "genuinely empty" from a likely
    # filter/JOIN-key mismatch (see ``data_query._empty_result_note``). Carried on
    # the model so the hint survives the HTTP path, not just the in-process one.
    note: Optional[str] = None


class WorkProgress(BaseModel):
    """How far along the blocking work behind a campaign request currently is.

    Exists because the two waits a caller actually sits through — pulling the campaign out of
    the object store, then executing the notebook — are both minutes long and were both
    reported as nothing at all. The counts live only in memory (see
    :attr:`CampaignDataStatus.progress`), so reading them costs no round-trip and a client can
    poll once a second without competing with the transfer it is describing.

    One shape for both phases rather than one model each: a caller renders ``done``/``total``
    the same way regardless, and ``unit`` is what makes the sentence read right.
    """

    #: Which blocking step is running. ``listing`` is the metadata pass that establishes
    #: ``total`` — brief, but on a large campaign not instant, so it is named rather than
    #: spent looking idle.
    phase: Literal["listing", "downloading", "executing"]
    #: What ``done`` and ``total`` count, so a client need not branch on ``phase`` to word it.
    unit: Literal["files", "cells"]
    done: int = 0
    #: ``None`` while genuinely unknown (during ``listing``, or from a backend that cannot
    #: count cheaply). A client shows an indeterminate bar rather than inventing a denominator.
    total: Optional[int] = None
    #: Bytes are tracked only for transfers; ``0``/``None`` for cell execution.
    bytes_done: int = 0
    bytes_total: Optional[int] = None
    #: Free text naming the concrete thing in flight, e.g. the notebook being executed.
    detail: str = ""


class CampaignDataStatus(BaseModel):
    """Whether querying this campaign has to transfer anything first, and what it costs.

    Exists so a caller can say *why* it is about to wait, **before** it waits. As with
    :class:`ResourceUsage`, the local↔cluster difference is resolved inside the service, so
    a consumer reads the same fields either way and never branches on the backend:
    ``fetch_required`` false means the question does not apply.

    Deliberately cheap — two metadata lookups, never an enumeration of the campaign prefix
    — because the point is to answer *before* the expensive thing, and a probe that itself
    cost a listing would only move the cost.
    """

    campaign_id: str
    #: ``"local-disk"`` (files on the service's own disk) or ``"object-store"``.
    source: Literal["local-disk", "object-store"]
    #: Can a query have to transfer data before it can answer? False on local, where there
    #: is nothing to fetch and so nothing to warn about.
    fetch_required: bool
    #: The query databases are already cached at their current size, so the next query
    #: reads them directly. Always True when ``fetch_required`` is False.
    cached: bool
    #: How a transfer reaches the store: ``"none"``, ``"cluster-network"`` (in-pod, LAN
    #: speed) or ``"port-forward"`` (off-cluster driver — the slow one). The two cluster
    #: modes differ by orders of magnitude, so "object store" alone would not tell a caller
    #: whether to expect seconds or minutes.
    transfer: Literal["none", "cluster-network", "port-forward"]
    #: Size of what a *query* needs (``data.db`` + ``campaign.db``) — not of the campaign,
    #: which is typically orders of magnitude larger and irrelevant here.
    db_bytes: int = 0
    #: Another request is fetching this campaign right now; a query queues behind it rather
    #: than starting a second transfer.
    fetch_in_progress: bool = False
    #: What this service's last completed transfer of this campaign actually cost. ``None``
    #: before the first one — process-local, so a restart forgets it.
    last_fetch_seconds: Optional[float] = None
    last_fetch_bytes: Optional[int] = None
    #: Live counts for the transfer or render running *right now*, or ``None`` when this
    #: service is not busy with this campaign. Read from memory, so asking is free even while
    #: the transfer it describes is saturating the link.
    progress: Optional[WorkProgress] = None
    #: One human sentence naming the reason, for a client to show or an agent to repeat.
    note: str = ""


class SceneStatus(BaseModel):
    """Whether this run's 3D geometry is ready, and if not, what is happening about it.

    The same job as :class:`CampaignDataStatus`, for the same reason — *say why you are about to wait,
    before you wait* — so the fields deliberately reuse its names rather than inventing synonyms. A
    scene descriptor is compiled on demand, in the campaign's own image, and cached by world identity;
    the first viewer of a given world pays for it and everyone after reads it from disk.

    Reading it never starts anything: that is ``POST .../scene/run``. A ``GET`` that launched a 2 GB
    image pull would fire on a browser prefetch or a strict-mode double render.
    """

    campaign_id: str
    config_name: str = ""
    run_id: str = ""
    #: Ready to fetch: the descriptor is in the cache and ``url`` points at it.
    cached: bool = False
    #: Nothing cached yet, so a viewer must POST to have it built. False when cached.
    generation_required: bool = False
    #: Someone is building this exact world right now; a second request joins rather than duplicating.
    in_progress: bool = False
    #: Which step the wait is on, so a panel can name it instead of spinning: ``queued`` /
    #: ``pulling`` (a 2 GB image onto a fresh node — the dominant cold cost) / ``compiling`` /
    #: ``transferring`` / ``""`` when nothing is running.
    stage: str = ""
    #: Size of the cached descriptor, 0 when there is none. A browser fetches this much.
    bytes: int = 0
    #: Where the descriptor's ``scene.json`` is served from, once cached.
    url: str = ""
    #: The world identity this run needs, for display and for diagnosis when geometry looks wrong.
    world: str = ""
    #: False when the run's capture predates override recording: geometry is compiled from the *bare*
    #: world, which is wrong for a run that varied it. Surfaced rather than silently assumed.
    overrides_known: bool = True
    #: Set when geometry cannot be produced at all, naming the reason.
    error: str = ""
    #: One human sentence, for a client to show or an agent to repeat.
    note: str = ""


class PlotSpec(BaseModel):
    """One declared plot: a runnable query plus the Vega-Lite spec that charts it."""

    title: str = ""
    query: str = ""
    vega_lite: dict = Field(default_factory=dict)


class CampaignPlotsResponse(BaseModel):
    """User-declared plots for a campaign (from its snapshot ``.vast``
    ``evaluation.plots``). Each entry is ``{title, query, vega_lite}``."""

    campaign_id: str
    plots: list["PlotSpec"] = Field(default_factory=list)


class PlaybackTimeline(BaseModel):
    """The table and column defining a non-ROS run's playback range.

    Typed rather than a bare dict because the run view feeds ``table`` straight into a
    query: as ``Optional[dict]`` the generated client saw ``{}``, and only a hand-written
    type that happened to say ``string`` kept that call type-checking.
    """

    table: str = ""
    time_column: str = "timestamp"


class CampaignPanelsResponse(BaseModel):
    """The run-view panels for a campaign: the ones its snapshot ``.vast``
    declares under ``visualization.results.run_view.panels``, plus the
    contributed ones no ``.vast`` has to write (the ``playback`` transport
    always, a ``scene3d`` for a simulator that records a capture). Each entry
    is the flattened panel dict (``type`` + ``position`` + panel-specific data
    bindings), rendered by the web run-view against the campaign's ``data.db``.
    ``timeline`` (optional, ``visualization.results.run_view.timeline``) names
    the table + column that defines the playback range for non-ROS runs.
    ``transport_only`` answers, for the run view, whether any of it is content."""

    campaign_id: str
    panels: list[dict] = Field(default_factory=list)
    timeline: Optional["PlaybackTimeline"] = None
    #: Whether the served list holds nothing but the always-on transport -- i.e. this run view has
    #: nothing to look at, and the web UI says so and points at **Edit visualization**. The served
    #: list is never empty, so "bare" cannot be its length; and which panels a campaign gets
    #: without asking is settled here, where they are merged in (a simulator that records a capture
    #: contributes its own ``scene3d``, which *is* content), rather than in the web UI, which would
    #: then have to spell the contributed types a second time.
    transport_only: bool = False


# NOTE: the costmap frame endpoint (``CostmapFrame`` model + ``get_costmap_frame`` +
# ``Routes.campaign_costmap``) moved out of core: it is now a package-provided service
# endpoint shipped by ``robovast_nav`` (``robovast.service_endpoints`` group). See
# ``robovast.service.endpoint_plugin`` for the generic mechanism.


class CampaignVisualization(BaseModel):
    """One ``evaluation.visualization`` notebook workload + the node levels it
    defines a notebook for (a subset of ``run``/``config``/``batch``/``campaign``).
    A ``batch`` notebook is only reachable on a search campaign, whose tree has the
    batch nodes to select."""

    name: str
    levels: list[str] = Field(default_factory=list)


class CampaignVisualizationsResponse(BaseModel):
    """The notebook workloads declared for a campaign (its snapshot ``.vast``
    ``evaluation.visualization``). The web Explorer renders one tab per workload and
    fetches the executed HTML per selected node via :meth:`render_campaign_notebook`."""

    campaign_id: str
    workloads: list[CampaignVisualization] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------


class ServiceError(OSError):
    """A refusal from the service, carrying what the service actually said.

    The HTTP transport used to call ``raise_for_status()``, which discards FastAPI's
    ``{"detail": ...}`` body. Every 4xx therefore reached an MCP tool or the CLI as
    ``"400 Client Error: Bad Request for url: http://…?as=text&lines=200"`` — the status
    line and the URL, with the actual message dropped. The service's refusals are written
    to say what to do next ("is a binary file — read it as bytes…", "stop it first"), and
    none of that survived the trip, while the web UI, which parses ``detail`` itself, saw
    the real text. Two client families, two answers about the same call.

    Subclasses ``OSError`` deliberately: ``requests.HTTPError`` already did (via
    ``IOError``), so every existing ``except OSError`` — notably
    :data:`robovast.mcp_server.data_access._REPORTED`, which relies on it to turn a
    service-side SQL rejection into a reported error rather than a traceback — keeps
    working unchanged.
    """

    def __init__(self, status: int, detail: str, url: str = ""):
        self.status = status
        self.detail = detail
        self.url = url
        super().__init__(detail)


API_VERSION = "0"

#: The port a robovast-service listens on unless told otherwise, and the one every
#: client probes to find a local one. Here rather than beside either the server or
#: the deployment because both need it and so does a client that has neither: it was
#: declared twice, in ``service/app.py`` and in the cluster deploy manifests, which
#: is one edit away from a client probing a port nothing listens on.
DEFAULT_PORT = 8800

#: The wall-clock cap on one ``exec_in_container`` command; anything needing longer wants
#: a campaign. Part of the wire contract, not an implementation detail of either side: the
#: server enforces it and the client sizes its read timeout by it, so a client install --
#: which has no server -- still has to know the number.
COMMAND_LIMIT_S = 300


class Routes:
    """Canonical HTTP paths — shared by the service app and the HTTP client so
    the two bindings cannot drift. Phase 0 (campaign lifecycle + version)."""

    VERSION = "/version"
    HEALTHZ = "/healthz"
    #: Exchange the shared secret for a session cookie. Served, so the dev proxy and the
    #: middleware's public-path list must both know it — hence a member here rather than
    #: a literal at the route.
    LOGIN = "/login"
    USAGE = "/usage"
    CAMPAIGNS = "/campaigns"
    #: SSE stream of the campaign list — a server-side loop over the same
    #: ``list_campaigns`` pull (``CAMPAIGNS`` above stays the authoritative read for
    #: MCP / the CLI), pushing the full list on connect and on every change.
    CAMPAIGNS_STREAM = "/campaigns/events"
    WORKSPACES = "/workspaces"
    #: The file side channel: POST an address for a grant, then PUT the bytes to the
    #: token URL. Not workspace-scoped — the request carries a ``/sources`` address.
    UPLOADS = "/uploads"
    UPLOAD = "/uploads/{token}"
    #: Taking a campaign in. ``CAMPAIGN_ARCHIVES`` is the archive side channel (POST for a
    #: grant, PUT the bytes to the token URL, exactly as ``UPLOADS`` above); it stores the
    #: archive and stops there. ``CAMPAIGN_IMPORT`` is the import itself, and is what every
    #: caller uses — an upload, a CLI, the MCP tool — so the operation has one entrance.
    #:
    #: Two distinct words rather than a singular/plural pair: ``/campaigns/import`` beside
    #: ``/campaigns/imports`` is a difference no reader spots. Neither can shadow a campaign
    #: id, since those must match ``<name>-<timestamp>`` (``is_campaign_dir``).
    CAMPAIGN_ARCHIVES = "/campaigns/archives"
    CAMPAIGN_ARCHIVE_UPLOAD = "/campaigns/archives/{token}"
    CAMPAIGN_IMPORT = "/campaigns/import"
    #: What is on the configured share, read by the service with the service's own
    #: credentials. Its own namespace rather than under ``/campaigns``: the share is a
    #: separate system, and what it holds is not a subset of what this service has --
    #: a campaign can be cleaned up here while its archive stays up there.
    SHARE_ARCHIVES = "/share/archives"
    #: Authoring help — static, no workspace (config editor).
    CONFIG_SCHEMA = "/config/schema"
    VARIATION_TYPES = "/variation_types"

    @staticmethod
    def workspace(workspace_id: str) -> str:
        return f"/workspaces/{workspace_id}"

    #: The two **content** namespaces. Everything else in this table is a *control*
    #: namespace: a fixed vocabulary of service-owned verbs. File content lives apart
    #: from it so that no user-chosen name can ever be shadowed by a route — including
    #: routes added later. See :mod:`robovast.client.file_address`.
    RESULTS = f"/{file_address.RESULTS}"
    SOURCES = f"/{file_address.SOURCES}"

    @staticmethod
    def file(address: str) -> str:
        """The URL for a file address — which *is* the address (see file_address)."""
        return address if address.startswith("/") else f"/{address}"

    @staticmethod
    def workspace_validate(workspace_id: str) -> str:
        return f"/workspaces/{workspace_id}/validate"

    @staticmethod
    def workspace_preview(workspace_id: str) -> str:
        return f"/workspaces/{workspace_id}/preview"

    @staticmethod
    def workspace_world(workspace_id: str) -> str:
        # What the campaign's simulator says its world provides -- answered by the simulator's
        # own image, so it is a workspace verb rather than static authoring help.
        return f"/workspaces/{workspace_id}/world"

    @staticmethod
    def variation_asset(name: str, path: str) -> str:
        # A variation plugin's web-preview asset (Module Federation remoteEntry + chunks).
        return f"/variation_types/{name}/assets/{path}"

    @staticmethod
    def panel_types_asset(name: str, path: str) -> str:
        # A package-provided run-view panel's web asset (Module Federation remoteEntry +
        # chunks), served from the providing plugin's ``WEB_PANEL`` dir.
        return f"/panel_types/{name}/assets/{path}"

    @staticmethod
    def campaign_panel_asset(campaign_id: str, path: str) -> str:
        # A user-authored ``custom`` panel's bundle, staged into the campaign's _config/.
        return f"/campaigns/{campaign_id}/panel_assets/{path}"

    @staticmethod
    def upload(token: str) -> str:
        return f"/uploads/{token}"

    @staticmethod
    def campaign_archive_upload(token: str) -> str:
        return f"/campaigns/archives/{token}"

    @staticmethod
    def campaign(campaign_id: str) -> str:
        # The campaign resource itself — DELETE removes it wholesale (local dir /
        # cluster object-store data). GET is not served; use the sub-resources below.
        return f"/campaigns/{campaign_id}"

    @staticmethod
    def campaign_status(campaign_id: str) -> str:
        return f"/campaigns/{campaign_id}/status"

    @staticmethod
    def campaign_search_history(campaign_id: str) -> str:
        # Namespaced under `search/` rather than a flat `search-history`: a quality-diversity
        # archive and a multi-objective Pareto front are already produced by shipped strategies
        # and have no live view, so `search/archive` and `search/front` are the obvious
        # neighbours. A namespace costs nothing now and saves renaming a published route later.
        return f"/campaigns/{campaign_id}/search/history"

    @staticmethod
    def campaign_stop(campaign_id: str) -> str:
        return f"/campaigns/{campaign_id}/stop"

    @staticmethod
    def campaign_retrigger(campaign_id: str) -> str:
        # Under the SOURCE campaign, because that is what the request identifies; the campaign
        # it creates is new and is named in the response.
        return f"/campaigns/{campaign_id}/retrigger"

    @staticmethod
    def campaign_retrigger_workspace(campaign_id: str) -> str:
        # Under the source campaign like the retrigger itself: the request identifies it, and what
        # it creates is named in the response.
        return f"/campaigns/{campaign_id}/retrigger/workspace"

    @staticmethod
    def campaign_retrigger_check(campaign_id: str) -> str:
        # GET, not POST: it changes nothing and costs nothing, which is the whole point of
        # having it beside the retrigger rather than inside it.
        return f"/campaigns/{campaign_id}/retrigger/check"

    @staticmethod
    def campaign_archive(campaign_id: str) -> str:
        # The postprocessed tar.gz, streamed from the object store. Named here like every
        # other path so the MCP's download link and the route serving it are one string.
        return f"/campaigns/{campaign_id}/archive"

    @staticmethod
    def campaign_logs(campaign_id: str) -> str:
        return f"/campaigns/{campaign_id}/logs"

    @staticmethod
    def campaign_logs_stream(campaign_id: str) -> str:
        # SSE transport over the same assembly seam as ``campaign_logs``: the browser
        # streams live, resuming from the byte offset it carries in ``Last-Event-ID``.
        # The pull endpoint above stays the authoritative read for MCP / the CLI.
        return f"/campaigns/{campaign_id}/logs/stream"

    @staticmethod
    def campaign_jobs(campaign_id: str) -> str:
        return f"/campaigns/{campaign_id}/jobs"

    @staticmethod
    def job_stop(campaign_id: str) -> str:
        # ``job_name`` is a query param for the same reason as ``job_log`` below: locally
        # it is a "<config>/<run>" id and contains a '/'.
        return f"/campaigns/{campaign_id}/job-stop"

    @staticmethod
    def job_log(campaign_id: str) -> str:
        # ``job_name`` is a query param (it may contain '/', e.g. a local
        # "<config>/<run>" id), so it never has to be path-encoded.
        return f"/campaigns/{campaign_id}/job-log"

    @staticmethod
    def job_state(campaign_id: str) -> str:
        # Same ``job_name``-as-query-param reason as ``job_log``: it may contain '/'.
        return f"/campaigns/{campaign_id}/job-state"

    @staticmethod
    def job_exec(campaign_id: str) -> str:
        # POST: it changes the run, unlike ``job_state``. Same ``job_name``-as-query reason.
        return f"/campaigns/{campaign_id}/job-exec"

    @staticmethod
    def job_log_stream(campaign_id: str) -> str:
        # SSE transport over ``job_log`` (same ``job_name`` query param + offset seam).
        return f"/campaigns/{campaign_id}/job-log/stream"

    #: Object-store bucket cleanup (server-side; not campaign-scoped in the path
    #: because it also serves the "all campaigns" case).
    CLEANUP_DATA = "/campaigns/cleanup-data"

    #: Experiment image builds (declared by a project's ``build:`` section).
    IMAGE_BUILDS = "/image-builds"

    @staticmethod
    def image_build_status(build_id: str) -> str:
        return f"/image-builds/{build_id}/status"

    @staticmethod
    def image_build_log(build_id: str) -> str:
        return f"/image-builds/{build_id}/log"

    #: Diagnostic command execution in an experiment image. POST runs a command,
    #: DELETE stops the held container. Produces no campaign, so it is not under
    #: ``/campaigns``.
    EXEC = "/exec"
    #: Resolves the image EXEC would use, without running anything.
    EXEC_RESOLVE_IMAGE = "/exec/resolve-image"

    @staticmethod
    def campaign_scene(campaign_id: str) -> str:
        # Control route: status only, and never starts work (see SceneStatus).
        return f"/campaigns/{campaign_id}/scene"

    @staticmethod
    def campaign_scene_run(campaign_id: str) -> str:
        # ``<noun>/run``, as postprocessing and share already are.
        return f"/campaigns/{campaign_id}/scene/run"

    @staticmethod
    def workspace_scene(workspace_id: str) -> str:
        # The config view's geometry: the world a .vast declares, before anything has run. Status
        # only, like its campaign sibling and for the same reason.
        return f"/workspaces/{workspace_id}/scene"

    @staticmethod
    def workspace_scene_run(workspace_id: str) -> str:
        return f"/workspaces/{workspace_id}/scene/run"

    @staticmethod
    def workspace_scene_asset(workspace_id: str, path: str) -> str:
        # A separate route from the campaign's, but the SAME cache behind it -- an entry is keyed
        # by world identity, so a project and a campaign built from it share one.
        return f"/workspaces/{workspace_id}/scene_assets/{path}"

    @staticmethod
    def campaign_screenshot(campaign_id: str) -> str:
        # A POST: it *runs* the simulator, in the campaign's own image. No status sibling —
        # the render is synchronous, so its result and its reason arrive in the response.
        return f"/campaigns/{campaign_id}/screenshot"

    @staticmethod
    def campaign_scene_asset(campaign_id: str, path: str) -> str:
        # The descriptor's bytes, served from the shared cache like a panel bundle. A separate first
        # segment from ``scene`` on purpose: ``scene/run`` would otherwise collide with a cached file
        # called ``run``.
        return f"/campaigns/{campaign_id}/scene_assets/{path}"

    @staticmethod
    def campaign_postprocessing(campaign_id: str) -> str:
        return f"/campaigns/{campaign_id}/postprocessing"

    @staticmethod
    def campaign_postprocessing_run(campaign_id: str) -> str:
        return f"/campaigns/{campaign_id}/postprocessing/run"

    @staticmethod
    def campaign_share_run(campaign_id: str) -> str:
        return f"/campaigns/{campaign_id}/share/run"

    @staticmethod
    def campaign_postprocessing_source(campaign_id: str) -> str:
        return f"/campaigns/{campaign_id}/postprocessing/source"

    @staticmethod
    def campaign_describe(campaign_id: str) -> str:
        return f"/campaigns/{campaign_id}/describe"

    @staticmethod
    def campaign_query(campaign_id: str) -> str:
        return f"/campaigns/{campaign_id}/query"

    @staticmethod
    def campaign_query_csv(campaign_id: str) -> str:
        # The uncapped, streamed twin of ``campaign_query``. A GET (not the POST the JSON
        # query uses) so the whole thing is one URL a browser or curl can follow.
        return f"/campaigns/{campaign_id}/query.csv"

    @staticmethod
    def campaign_data_status(campaign_id: str) -> str:
        # A **control** route, not a ``/results`` path: every segment under ``/results`` is
        # a user-chosen file name (see :mod:`robovast.client.file_address`), so a literal
        # ``status`` there would shadow a campaign file actually called that.
        return f"/campaigns/{campaign_id}/data-status"

    @staticmethod
    def campaign_plots(campaign_id: str) -> str:
        return f"/campaigns/{campaign_id}/plots"

    @staticmethod
    def campaign_panels(campaign_id: str) -> str:
        return f"/campaigns/{campaign_id}/panels"

    @staticmethod
    def campaign_panels_source(campaign_id: str) -> str:
        return f"/campaigns/{campaign_id}/panels/source"

    @staticmethod
    def campaign_visualizations(campaign_id: str) -> str:
        return f"/campaigns/{campaign_id}/visualizations"

    @staticmethod
    def campaign_notebook(campaign_id: str) -> str:
        return f"/campaigns/{campaign_id}/notebook"


class RobovastInterface(ABC):
    """The full RoboVAST operation surface (Phase 0 subset defined here).

    Implemented by the server core and by the client transports; both satisfy
    the identical contract so callers are transport-agnostic.
    """

    # -- version / health ---------------------------------------------------

    @abstractmethod
    def version(self) -> VersionInfo:
        """Report the implementation's RoboVAST + API version (handshake)."""

    @abstractmethod
    def resource_usage(self) -> ResourceUsage:
        """Report the execution backend's CPU/memory capacity + current usage.

        ``backend`` selects the lane on a multi-backend service ("local"/"cluster");
        single-backend services offer one lane and ignore it.
        """

    # -- workspaces (editable project inputs) -------------------------------

    @abstractmethod
    def create_workspace(self, request: CreateWorkspaceRequest) -> WorkspaceInfo:
        """Create a workspace to author a project in.

        Empty, or -- with ``from_campaign`` -- seeded from that campaign's frozen ``_config/``,
        reconstructed rather than copied (the scenario is placed where the ``.vast`` declares
        it). Refuses, leaving no workspace behind, when the snapshot cannot produce a project
        that would run the same configuration.
        """

    @abstractmethod
    def list_workspaces(self) -> ListWorkspacesResponse:
        """List workspaces, newest first."""

    @abstractmethod
    def get_workspace(self, workspace_id: str) -> WorkspaceInfo:
        """Return one workspace."""

    @abstractmethod
    def delete_workspace(self, workspace_id: str) -> ActionResult:
        """Delete a workspace and its inputs.

        Campaigns are self-contained, so this never affects existing results.
        """

    # -- files (one address space) ------------------------------------------
    # Every file operation takes an *address* — ``/results/<campaign_id>/<path>`` or
    # ``/sources/<workspace_id>/<path>`` — which is also the URL that serves it. The
    # namespace carries the permission: only ``/sources`` is writable, and the write
    # operations below refuse ``/results`` rather than each caller remembering to.
    # See :mod:`robovast.client.file_address`.

    @abstractmethod
    def list_files(self, address: str, recursive: bool = False, detail: bool = False,
                   offset: int = 0, limit: int = 100) -> FileListing:
        """List one directory of the address space.

        **Non-recursive by default**: a campaign holds one directory per configuration
        and one per run, so a recursive listing of its root is thousands of entries a
        caller did not ask for. ``total`` reports what was there before ``offset`` /
        ``limit``, so a truncated page still says how much it left out.

        Raises ``ValueError`` on a malformed address (→ 400) and ``KeyError`` when the
        campaign/workspace or the directory does not exist (→ 404).
        """

    @abstractmethod
    def read_file(self, address: str, lines: int = 200, offset: int = 0) -> FileText:
        """Read a page of a text file. Binary content is refused, never mangled.

        Line-based paging happens **server-side**, so a caller reading 100 lines of a
        log on the cluster transfers 100 lines, not the file.

        Raises ``ValueError`` on a malformed address or a binary file (→ 400) and
        ``KeyError`` when the file does not exist (→ 404).
        """

    @abstractmethod
    def read_file_bytes(self, address: str) -> bytes:
        """Return one file's raw bytes — the representation a browser or ``vast files
        get`` wants (e.g. the run view's ``scene/scene.json`` and its sibling
        ``scene.bin``). Same errors as :meth:`read_file`, minus the binary refusal."""

    def local_file(self, address: str) -> Path:
        """A real filesystem path for *address*, so the HTTP layer can stream the file.

        Part of the **serving** contract rather than the client one: a response built from a
        path streams, and carries ``Range`` and conditional requests with it — which is what
        keeps a rosbag out of the service's memory and lets a browser seek a ``.webm``
        instead of downloading it before playing.

        Concrete-with-a-refusal rather than ``@abstractmethod`` because an implementation
        that only *calls* a service (:class:`HTTPTransport`) has no local file to offer and
        should not be forced to write a stub claiming otherwise. It says so here instead of
        failing as a missing attribute in a route.

        Every implementation that is actually served from — ``LocalTransport`` and its
        subclasses — overrides this. Callers must therefore **not** probe for it with
        ``getattr``: such a check can only succeed, and the code that believed otherwise sent
        cluster campaigns down the local resolver for years.
        """
        raise NotImplementedError(
            f"{type(self).__name__} serves no local files; it cannot stream {address!r}")

    @abstractmethod
    def write_file(self, request: WriteFileRequest) -> FileMeta:
        """Write a ``.vast``/``.osc`` file inline (other types → :meth:`create_upload`).

        ``/sources`` only; a ``/results`` address raises ``ValueError``.
        """

    @abstractmethod
    def edit_file(self, request: EditFileRequest) -> FileMeta:
        """Replace a unique string in a ``.vast``/``.osc`` file (token-cheap fix loop)."""

    @abstractmethod
    def delete_file(self, address: str) -> ActionResult:
        """Delete a file. ``/sources`` only."""

    @abstractmethod
    def create_upload(self, request: CreateUploadRequest) -> UploadGrant:
        """Grant a one-time, TTL-scoped HTTP PUT for any file type.

        Takes a ``/sources`` address like every other file operation; the grant's
        ``url`` is a one-time capability, not an address.
        """

    # -- campaign lifecycle -------------------------------------------------

    @abstractmethod
    def create_campaign(self, request: CreateCampaignRequest) -> CampaignRef:
        """Validate the workspace's project and start a campaign; return its id.

        Returns immediately (fire-and-forget); poll :meth:`get_status`.
        """

    @abstractmethod
    def retrigger_campaign(self, campaign_id: str) -> CampaignRef:
        """Launch a **new** campaign from what an existing one recorded; return its id.

        Reads the source campaign's frozen ``_config/`` and its ``_execution/`` records
        instead of a workspace — campaigns are workspace-independent, and the workspace a
        campaign came from may be gone or may have moved on, while its ``_config/`` is
        already the single source of truth for its configuration. The source is never
        written to; this produces a separate campaign with its own timestamped id, so it
        works whatever state the source ended in.

        Reproduces the configuration and **pins the image the source recorded** — a
        campaign's build context is not archived in its results, so the image cannot be
        rebuilt from them and a campaign that never recorded one is refused rather than
        rebuilt from a guess. The recorded ``_execution/launch.yaml`` replays the
        ``config_filter`` and requested ``runs``, so re-running a one-config pilot stays a
        one-config pilot.

        Everything downstream of the configuration is **re-expanded**: ``execution.generate``
        generators re-run (their cache is not archived), so a stochastic generator draws new
        samples. This is a re-run, not a replay of the same trials.

        Returns immediately, exactly like :meth:`create_campaign`; poll :meth:`get_status`.
        """

    @abstractmethod
    def materialize_retrigger_workspace(self, campaign_id: str,
                                        workspace_name: str) -> WorkOrder:
        """Materialise *campaign_id* as a workspace with its config migrated as far as possible.

        For a config no ladder step can carry forward. Nothing is launched: the result is a
        workspace holding the campaign's project, the config upgraded to the point it stopped, and
        a marker at every decision left -- deliberately invalid until each is resolved, because a
        partly-migrated config that loaded would run a different experiment.

        A workspace rather than a bespoke editing mode, so the whole existing authoring toolchain
        (the web editor, read/write/edit_file, validate_project) applies unchanged.
        """

    @abstractmethod
    def check_retrigger(self, campaign_id: str) -> RetriggerReport:
        """Whether *campaign_id* can be re-run, and what is missing if not.

        Answers without staging anything, starting a container or spending compute, so it is
        the cheap thing to call before :meth:`retrigger_campaign` rather than launching to find
        out. Reports every axis at once -- config version, host/container protocol, images,
        third-party plugins, asset providers -- because they fail independently.

        Computed service-side, like :meth:`validate_project`: a client-only install has no
        access to the service's results directory, so a client that tried to work this out for
        itself could not answer at all.
        """

    @abstractmethod
    def get_status(self, campaign_id: str) -> Status:
        """Return the campaign's live :class:`Status` (phase, progress, history)."""

    @abstractmethod
    def get_search_history(self, campaign_id: str) -> SearchHistory:
        """Return a search's per-batch objective trajectory.

        Deliberately its own call rather than a field on :meth:`get_status`: the status is
        polled by every campaign card at 1.5s, and a series that grows with the batch count
        does not belong on a payload whose cost is multiplied by campaigns on screen. This one
        is read only when something is actually displaying it.
        """

    @abstractmethod
    def get_campaign_logs(self, campaign_id: str, offset: int = 0) -> LogChunk:
        """Return the campaign's ``controller.log`` from byte *offset* onward.

        For streaming: poll from ``0``, append :attr:`LogChunk.text`, then poll
        again from the returned :attr:`LogChunk.next_offset`. Serves the live file
        while the campaign runs and the durable copy afterwards.
        """

    @abstractmethod
    def list_jobs(self, campaign_id: str) -> ListJobsResponse:
        """List the campaign's current-batch jobs (live) plus aggregate counts.

        A "job" is one execution unit (a run locally, a Kubernetes Job on the
        cluster). Reports live status only; pair with :meth:`get_job_log` to read a
        running job's log.
        """

    @abstractmethod
    def get_job_log(self, campaign_id: str, job_name: str,
                    offset: int = 0) -> LogChunk:
        """Return a **running** job's live log from byte *offset* onward.

        Same streaming protocol as :meth:`get_campaign_logs` (poll, append
        :attr:`LogChunk.text`, resume from :attr:`LogChunk.next_offset`). Live source
        only — the running pod on the cluster, the job's ``logs/system*.log`` files
        locally. Raises if the job's log source is gone.

        **Every** container the job runs, merged into one stream: a job is not one
        container (the ROS shape gives the simulator and the system under test their
        own), and their output only explains a failure when read together. Each line is
        tagged ``[<container>]`` when there is more than one.
        """

    def get_job_state(self, campaign_id: str, job_name: str) -> "JobState":
        """What a **running** job is doing right now, from the run's own tools.

        The sibling of :meth:`get_job_log`, and the first thing to reach for on a wedged
        campaign: the log says what is repeating, this says what the simulator is doing while
        it repeats.

        Read by running the simulator's own **fixed** health command inside the job (see
        :meth:`~robovast.common.simulators.SimulatorBackend.health_command`) and passing its
        JSON through. Two consequences worth stating, because they are what make this safe to
        offer at all:

        * **RoboVAST parses no simulator's file format.** The tool that owns the records is the
          only thing that reads them, so a simulator is free to reshape its own recording
          without breaking this.
        * **The command is the service's, never the caller's.** That is the line between this
          and ``exec_in_container``: a fixed read cannot be turned into arbitrary execution by
          choosing its arguments, so it needs no provenance record. A caller-supplied command
          in a live job would be a different thing and would have to be recorded as one.

        Not abstract: a transport that cannot do this inherits a refusal rather than being
        forced to implement one, which is the same courtesy :meth:`share` gets.
        """
        del campaign_id, job_name
        raise NotImplementedError("this service cannot read a running job's state")

    def exec_in_job(self, campaign_id: str, job_name: str, command: str,
                    container: str = "scenario", source: str = "api") -> "ExecResult":
        """Run *command* inside a **running** job. Recorded against the run.

        The escape hatch for what a copy cannot answer. ``exec_in_container`` stages the same
        configuration and is the right first move precisely because it perturbs nothing -- but a
        fault that only appears under contention, a particular random draw, or a long warm-up does
        not reproduce there, and its *failure* to reproduce is itself the finding that sends you
        here.

        **This is a probe and it is written down.** The run is no longer untouched, so
        :func:`~robovast.common.campaign_data.record_intervention` records it *before* the command
        runs -- the same ordering :meth:`stop_job` uses, and for the same reason: a crash in
        between must not leave perturbed data with no explanation. Every run the job covers is
        marked, which surfaces as ``runs.probed`` in ``data.db``.

        That is the whole line between this and :meth:`get_job_state`: there the service chooses a
        fixed read, so nothing arbitrary can ride in and nothing needs recording. Here the *caller*
        chooses, which cannot be bounded -- so it is recorded instead of refused.

        Confirming a cause is the point; making a wedged run go green is not. The fix belongs in the
        ``.vast`` and the number belongs to a clean relaunch, with the probed run dropped.

        Args:
            container: A **role** (``scenario`` / ``simulation`` / ``sut``), not a container name
                -- the concrete name differs by lane, so naming one here would make the same call
                mean different things on the two. Spelled literally rather than imported from
                ``robovast.common.config``: this package deliberately does not depend on the core
                (see ``test_client_needs_no_core``).
            source: Which surface asked, for the ledger. Not an identity: the service is
                unauthenticated.

        Not abstract, for the reason :meth:`get_job_state` is not: a transport that cannot do this
        inherits a refusal rather than being made to write one.
        """
        del campaign_id, job_name, command, container, source
        raise NotImplementedError("this service cannot exec into a running job")

    @abstractmethod
    def stop(self, campaign_id: str) -> ActionResult:
        """Request a cooperative stop of a running campaign."""

    @abstractmethod
    def stop_job(self, campaign_id: str, job_name: str,
                 reason: Optional[str] = None,
                 source: str = "api") -> ActionResult:
        """Kill **one** running job; the rest of the campaign keeps going.

        The narrow intervention for a job that is visibly wedged and will not exit on its
        own. It is not how a campaign is ended — that is :meth:`stop`, which requests the
        cooperative stop this deliberately does *not*.

        Only a job whose status is ``running`` may be stopped. Anything else raises
        ``RuntimeError`` naming the phase it is actually in: a ``pending`` or ``waiting``
        job has not started (there is nothing wedged to kill), and a ``blocked`` one has a
        problem — no quota, an unpullable image — that deleting it does not fix.

        The kill is recorded durably (:func:`~robovast.common.campaign_data.record_intervention`),
        so the runs it cut short report ``status == "killed"`` rather than being
        indistinguishable from runs whose results went missing on their own. That record
        is permanent and is *not* a trial failure: a killed run is excluded from a
        campaign's pass/fail counts as a distinct category.

        Args:
            campaign_id: The campaign the job belongs to.
            job_name: The job as :meth:`list_jobs` reports it — ``<config>/<run>``
                locally, the Kubernetes Job name on the cluster.
            reason: The operator's optional explanation, stored with the record.
            source: Which surface asked — ``"webui"``, ``"mcp"``, ``"cli"``. Recorded for
                the audit trail; not a user identity, since the service is
                unauthenticated.

        Raises:
            KeyError: No such campaign, or no such job in it.
            RuntimeError: The job is not running.
        """

    @abstractmethod
    def list_campaigns(
        self, request: Optional[ListCampaignsRequest] = None
    ) -> ListCampaignsResponse:
        """List campaigns known to this service (global, newest first).

        Ordered by each campaign's recorded start time (``started_at``), and ordered
        *before* ``limit``/``offset`` are applied — so a page is the N newest, not an
        arbitrary window. Callers render this order as given; a start time is never
        derived from the campaign id, whose ``<name>-`` prefix is user-supplied. A
        campaign with no recorded start time comes last.
        """

    @abstractmethod
    def cleanup_campaign_data(self, request: CleanupDataRequest) -> ActionResult:
        """Delete campaign result bucket(s) from the object store.

        Runs server-side because the service holds the cluster config (object-store
        credentials) and the authoritative live-campaign set — so the CLI needs no
        cluster credentials. Live campaigns are skipped unless ``force`` names one.
        """

    @abstractmethod
    def delete_campaign(self, campaign_id: str) -> ActionResult:
        """Permanently delete **one** campaign wholesale — its durable home.

        Locally that is the campaign's directory under the results root; on a
        cluster it is the campaign's object-store data (plus any leftover Jobs and
        the service's local cache). Distinct from :meth:`cleanup_campaign_data`,
        which is a bulk object-store bucket sweep: this removes a single named
        campaign in full, whatever backend holds it.

        Refuses a campaign that is still running (raises so it surfaces as a 409);
        stop it first. A campaign that is already gone deletes idempotently. The
        external share copy (if any) is never touched — it is a separate system.
        """

    @abstractmethod
    def campaign_tar_stream(self, campaign_id: str):
        """Yield the campaign as a ``tar.gz``, in chunks, for ``GET /campaigns/{id}/archive``.

        What comes out is the campaign as this service holds it -- postprocessed, if it
        has been -- minus the internal ``_postproc/`` staging, so what lands is the clean
        campaign layout. Streamed on both lanes and buffered by neither: the cluster tars
        objects as it fetches them, the local lane tars its own directory into the
        response.

        On the interface rather than only on the lane that first needed it: this was a
        cluster-only method the HTTP route probed for with ``hasattr``, so a local service
        answered 409 and the web UI hid its download button there. Which lane a service
        runs is not what decides whether a caller can be handed a file.
        """

    @abstractmethod
    def list_share_archives(self) -> ShareListing:
        """What the configured share holds, read with **this service's** credentials.

        For the callers that cannot hold share credentials of their own: a browser has
        none at all, which is why the web UI's campaign rows and its Share view both come
        from here. The ``vast share`` verbs you perform yourself use *your* credentials
        instead, which is what lets a read-only one work — the provider layer underneath is
        the same one implementation either way.

        Nothing caches this. A "does this campaign have a share copy" field on a campaign
        was tried and is deliberately absent: it would be a second copy of another system's
        state, and the first out-of-band delete makes it a lie.

        Reports ``configured=False`` rather than an empty list when there is no share, since
        "nothing there" and "nowhere to look" are different answers.
        """

    # -- taking a campaign in (the opposite direction to the archive download) --

    @abstractmethod
    def create_archive_upload(self) -> UploadGrant:
        """Grant a one-time, TTL-scoped PUT for a campaign archive.

        The campaign-archive counterpart of :meth:`create_upload`, and separate from it
        because that one addresses ``/sources`` — it needs workspaces configured, and an
        archive is not project input. The PUT stores the bytes and answers where they
        landed; :meth:`import_campaign` is what then imports them.
        """

    @abstractmethod
    def import_campaign(self, request: ImportCampaignRequest) -> CampaignRef:
        """Take a campaign in, as a tracked background operation.

        The one import op, whatever the source: an upload redeemed through
        :meth:`create_archive_upload`, a file already on the service host, or an archive on
        the configured share, which the *service* fetches so a campaign never has to travel
        through somebody's laptop to get between two servers. After it the campaign is
        indistinguishable from one that ran here — which takes registration, not just
        extraction, since listings and the web UI answer from ``campaign.db`` rather than
        from the results tree. When what arrived was **raw** (no ``_execution/data.db``, which
        is what the share holds) postprocessing is chained, because a campaign without its
        metric tables is not one anybody can ask anything.

        Returns a :class:`CampaignRef`, not a report: the work is a download, a
        multi-gigabyte extraction and then postprocessing, and an inline wait would turn a
        succeeding import into a client timeout. A ref for the same reason
        :meth:`create_campaign` and :meth:`retrigger_campaign` return one — all three mean
        "a campaign now exists, here is its id, go watch it", and an id a caller has to read
        out of a prose message is an id that breaks the first time the message is reworded.
        Per-stage detail lands in the campaign's ``_execution/import.json`` and its
        narrative in ``_execution/import.log``.

        The campaign is registered at phase ``importing`` before any bytes move — its id is
        read from the archive's member list, or from the share object's name — so it appears
        in the campaign view while it is still arriving, and ``vast wait`` follows it like
        any other work.

        A failed import is **kept**, as a failed campaign: registering it is what made it
        visible while it was arriving, so deleting the tree afterwards would leave it listed
        with nothing behind it to explain why. Its ``import.log`` and ``import.json`` stay
        where they are, and it is removed like any other campaign. The archive is untouched,
        so a retry with ``force`` costs only the transfer.

        Reads an arbitrary path on the service host by design: that is what lets a caller
        import a dataset it put there by other means, and it is the same privilege
        ``get_service_info``'s roots and :meth:`exec_in_container` already assume of an
        authenticated caller. An archive the *service* staged is deleted once imported; a
        path the caller named is left alone.

        Raises ``ValueError`` for an archive that is not one campaign or a request that does
        not name exactly one source, ``KeyError`` when the named archive is not there, and
        ``RuntimeError`` when a campaign of the same id is already here and ``force`` was not
        set, or when that campaign is busy with another operation.
        """

    # -- image builds (experiment image, from the project's build: section) --

    @abstractmethod
    def build_image(self, request: BuildImageRequest) -> ImageBuildRef:
        """Build the derived images the project's containers declare.

        A container is built when it adds ``system_packages`` or ``python_packages``,
        whatever its role; one that adds nothing is pulled as-is.

        Idempotent/content-addressed: returns immediately with ``cached=True`` when
        an image for the same inputs already exists; otherwise starts a build and
        returns its id (poll :meth:`get_image_build_status`). The image is referenced
        from ``execution.image`` as ``build:<tag>``; the concrete registry-qualified
        ref is resolved server-side and never crosses this interface. Raises
        ``ValueError`` if the project declares no derived image or it fails
        validation (fail-fast at submit).
        """

    @abstractmethod
    def get_image_build_status(self, build_id: str) -> ImageBuildStatus:
        """Return an image build's live status (phase, done, structured error)."""

    @abstractmethod
    def get_image_build_log(self, build_id: str, offset: int = 0) -> LogChunk:
        """Return the builder's raw log from byte *offset* onward.

        Same streaming protocol as :meth:`get_campaign_logs` (poll, append
        :attr:`LogChunk.text`, resume from :attr:`LogChunk.next_offset`). The raw
        builder output for deep dives when :class:`ImageBuildError` is not enough.
        """

    # -- container exec (diagnostic; produces no campaign) ------------------

    @abstractmethod
    def exec_in_container(self, request: ExecRequest) -> ExecResult:
        """Run one command in the experiment image and return what it produced.

        This is for **testing a container and its setup**, not for running experiments:
        nothing it does is durable — no campaign directory, no ``/out`` mount, no
        provenance, no repetitions — so its output cannot be compared with a campaign's.
        Use :meth:`create_campaign` to run the experiment.

        Two shapes, distinguished by ``config_name``: omitted, the bare image (imports,
        ``ros2 pkg list``, file checks); named, that config staged as a campaign would
        stage it, where an empty ``command`` starts its scenario.

        **A running campaign is never a target *of this call*.** There is no path from here to
        a job's container or pod, and the reason has not changed: a campaign in flight is
        provenance-recorded, reproducible compute, and attaching to it perturbs the thing it
        exists to produce. What changed is that the perturbation can now be *recorded* instead
        of forbidden -- :meth:`exec_in_job` enters the live job and marks every run it covers as
        probed, and :meth:`get_job_state` reads one without perturbing it at all.

        So this stays the right first move rather than the only one: it answers the same
        question against a copy, at no cost to the campaign. Reach for the live job when a fault
        does not reproduce here, which is itself the finding.

        At most one exec container exists at a time (see :class:`ExecContainerState`);
        ``keep_alive=False`` also stops a held one. Raises ``ValueError`` when the
        request names no source, names both, names an unknown ``config_name``, or asks
        for an empty command with no config staged.
        """

    @abstractmethod
    def stop_exec_container(self) -> ExecStopResult:
        """Stop the held exec container, if there is one.

        Idempotent: with nothing held this reports ``stopped=False`` rather than
        failing. Never touches a campaign's container.
        """

    @abstractmethod
    def resolve_image(self, request: ExecRequest) -> ImageResolution:
        """Resolve the image :meth:`exec_in_container` would use for *request*, without
        running anything. See :class:`ImageResolution`. ``request.command``/``keep_alive``/
        ``show_gui`` are unused — only the addressing fields matter.
        """

    # -- postprocessing (editable, re-runnable; never mutates _config) ------

    @abstractmethod
    def get_postprocessing(self, campaign_id: str) -> PostprocessingInfo:
        """Return the campaign's effective postprocessing entries + revisions."""

    @abstractmethod
    def update_postprocessing(
        self, request: UpdatePostprocessingRequest
    ) -> PostprocessingRevision:
        """Write a new versioned postprocessing override (validated)."""

    @abstractmethod
    def get_postprocessing_source(self, campaign_id: str) -> PostprocessingSource:
        """Return the effective ``results_processing.postprocessing`` block as
        editable YAML text (drives the campaigns-view rerun dialog)."""

    @abstractmethod
    def update_postprocessing_source(
        self, request: UpdatePostprocessingSourceRequest
    ) -> PostprocessingSource:
        """Persist an edited postprocessing block as a new `.vast` override
        revision (validated; never mutates ``_config/``)."""

    @abstractmethod
    def run_postprocessing(self, request: RunPostprocessingRequest) -> ActionResult:
        """(Re)run analysis postprocessing for one campaign with the effective config."""

    @abstractmethod
    def run_share(self, request: RunShareRequest) -> ActionResult:
        """(Re)trigger the upload-to-share of one finished campaign's raw archive.

        Works from disk with no live in-memory entry (usable after a service restart);
        the target provider comes from the current environment (``ROBOVAST_SHARE_TYPE``
        + credentials), so adjusting it and re-triggering re-uploads to a new provider.
        """

    # -- validation / preview / authoring help (config editor) --------------

    @abstractmethod
    def validate_project(self, workspace_id: str, path: str = "") -> ValidationReport:
        """Collect-all validation of a workspace ``.vast`` project.

        Wraps ``config_validation.validate_project_file``. ``path`` selects which
        ``.vast`` (workspace-relative); empty picks the sole ``.vast`` (error if
        there are several — pass ``path``). Empty ``workspace_id`` → the CWD project.
        Returns every problem at once (schema, scenario file, plugin refs) + counts.
        """

    @abstractmethod
    def preview_configurations(
        self, workspace_id: str, max_configs: int = 0, path: str = ""
    ) -> PreviewResponse:
        """Expand a workspace ``.vast`` into resolved configurations (no run).

        Wraps ``config_generation.generate_scenario_variations(output_dir=None)``;
        nothing is executed or written. ``path`` selects which ``.vast`` (empty =
        the sole one); ``max_configs`` caps the returned list.
        """

    @abstractmethod
    def describe_world(self, workspace_id: str, path: str = "", targets: str = "",
                       entities: bool = False, backend: str = "") -> WorldDescription:
        """Describe the world this campaign's simulator will load.

        The other half of authoring the ``sim`` channel. ``preview_configurations`` says what
        the campaign expands to; this says what the *world* offers it — which plugins an
        override can address, and with *targets* which model values a run may change at all,
        with the objects that can be named and their current values. Written against a guess,
        both are refused inside the container after an image pull; the whole point of asking
        here is that it costs one container run and no compute.

        Only the simulator can answer: resolving a world's ``extends`` chain, and which world a
        ref even names, needs the simulator installed. So the answer comes from **the image the
        campaign runs**, and carries which image that was.

        *targets* is a glob over object names and *entities* asks for the compiled entity list;
        both cost a model build, which is why neither is implied. *backend* names the lane the
        query runs on (``"local"``/``"cluster"``) — a service offering both must not accept a lane
        and then answer from the other one. Raises ``ValueError`` when no answer is possible — no
        backend, an image that must be built first, no container runner here — because
        "unverifiable" is not an empty result.
        """

    @abstractmethod
    def get_config_schema(self) -> dict:
        """Return the ``.vast`` JSON Schema (``ConfigV1.model_json_schema()``).

        Static — feeds the web editor's schema-driven completion/validation.
        """

    @abstractmethod
    def list_variation_types(self) -> VariationTypesResponse:
        """List the registered ``robovast.variation_types`` + their parameters."""

    # -- results data query (eval viewer) -----------------------------------

    @abstractmethod
    def describe_campaign_data(self, campaign_id: str) -> DataDescribe:
        """Describe a campaign's ``data.db`` schema (+ attached ``campaign.db``).

        The dir is resolved per transport (local disk / object-store fetch); the
        query logic is shared with the MCP ``run_data`` plugin
        (:mod:`robovast.results_processing.data_query`).
        """

    @abstractmethod
    def query_campaign_data_sql(
        self, campaign_id: str, sql: str, max_rows: int = 500,
        extra_campaign_ids: Optional[list[str]] = None,
        max_bytes: Optional[int] = None,
    ) -> DataQueryResult:
        """Run a read-only ``SELECT`` over a campaign's data (``campaign.db`` attached).

        The reply is bounded on two axes: ``max_rows`` (clamped at 5000) and its serialized
        size. The size ceiling defaults to a *context* budget, because the caller that
        cannot afford a large reply is the one reading it into a context window; a caller
        that renders the rows passes ``max_bytes`` to lift it.
        """

    @abstractmethod
    def stream_campaign_query_csv(
        self, campaign_id: str, sql: str,
        extra_campaign_ids: Optional[list[str]] = None,
    ):
        """The same ``SELECT``, yielded as CSV text with **no row cap**.

        :meth:`query_campaign_data_sql` clamps at 5000 rows and reports ``truncated``,
        which left a caller who wanted the whole result with nowhere to go. This is that
        somewhere: streamed, so neither end holds it, and cheap enough for an MCP tool to
        hand over the URL rather than spend context on rows.
        """

    @abstractmethod
    def campaign_data_status(self, campaign_id: str) -> CampaignDataStatus:
        """Report whether a query on this campaign must transfer data first, and its cost.

        A **cheap** pre-flight for :meth:`describe_campaign_data` /
        :meth:`query_campaign_data_sql`: it answers before the wait rather than explaining
        after it, so a client can say *fetching this campaign's databases* instead of
        appearing to hang. Must not itself enumerate the campaign.
        """

    @abstractmethod
    def campaign_scene_status(
        self, campaign_id: str, config_name: str, run_id: str
    ) -> SceneStatus:
        """Is this run's 3D geometry ready, and if not, what is happening about it.

        **Pure**: it reads the run's capture manifest and the campaign's image identity, and never
        starts a build. That is :meth:`run_campaign_scene`, because a ``GET`` that launches a 2 GB
        image pull would fire on a browser prefetch.
        """

    @abstractmethod
    def run_campaign_scene(
        self, campaign_id: str, config_name: str, run_id: str
    ) -> ActionResult:
        """Build this run's geometry if it is not cached, and return immediately.

        Joins an in-flight build of the same world rather than starting a second one; a build serves
        every campaign that used that world, so the work is shared even across campaigns. Poll
        :meth:`campaign_scene_status` for progress.
        """

    @abstractmethod
    def workspace_scene_status(self, workspace_id: str, path: str = "") -> SceneStatus:
        """Is this project's world compiled, and if not, what is happening about it.

        The config view's counterpart of :meth:`campaign_scene_status`, and pure for the same
        reason. The world comes from the ``.vast`` rather than from a run's capture, so this
        answers before the project has ever been run.
        """

    @abstractmethod
    def run_workspace_scene(self, workspace_id: str, path: str = "") -> ActionResult:
        """Compile this project's world if it is not cached, and return immediately.

        Shares the cache with :meth:`run_campaign_scene`: a campaign launched from this project
        finds the geometry already built, and the reverse.
        """

    @abstractmethod
    def resolve_workspace_scene_asset(self, workspace_id: str, path: str) -> str:
        """Filesystem path of one file of a cached descriptor, for the workspace route."""

    @abstractmethod
    def campaign_screenshot(self, campaign_id: str, config_name: str, run_id: str, *,
                            at: Optional[float] = None, view: Optional[dict] = None,
                            focus: Optional[list] = None, camera: Optional[str] = None,
                            size: str = "960x720") -> str:
        """Re-render one moment of a run from a chosen viewpoint; return the image's path.

        The counterpart of :meth:`campaign_scene_status` for *pixels* rather than geometry, and
        unlike it this one **does** work: it runs the simulator in the campaign's own pinned
        image. Synchronous, because the result is the point and nothing about it is cacheable —
        the key would be a camera pose and a moment.

        Needs a simulator that can re-render (``SimulatorBackend.simulation_screenshot``) and a
        run that recorded its state. Raises with the reason when either is missing.

        The caller owns the returned path and removes it with
        ``robovast.service.screenshot.discard``; the route does that once the response is sent.
        """

    @abstractmethod
    def resolve_campaign_scene_asset(self, campaign_id: str, path: str) -> str:
        """Absolute path of one file of this campaign's cached descriptor.

        Raises ``KeyError`` when it is not cached or the path escapes the entry -- the same contract
        :meth:`resolve_campaign_panel_asset` has, so the route serves both the same way.
        """

    @abstractmethod
    def list_campaign_plots(self, campaign_id: str) -> CampaignPlotsResponse:
        """Return the campaign's user-declared plots (``evaluation.plots`` in its
        snapshot ``.vast``): ``{title, query, vega_lite}`` each, rendered by the
        eval viewer against :meth:`query_campaign_data_sql`."""

    @abstractmethod
    def list_campaign_panels(self, campaign_id: str) -> CampaignPanelsResponse:
        """Return the campaign's run-view panels (top-level ``visualization.panels``
        in its snapshot ``.vast``): the raw panel dicts, rendered by the web run-view
        against the campaign's ``data.db``."""

    @abstractmethod
    def get_panels_source(self, campaign_id: str) -> PanelsSource:
        """Return the effective ``visualization:`` block as editable YAML text
        (drives the run-view 'edit visualization' dropdown)."""

    @abstractmethod
    def update_panels_source(
        self, request: UpdatePanelsSourceRequest
    ) -> PanelsSource:
        """Persist an edited ``visualization:`` block as a new `.vast` override
        revision (never mutates ``_config/``). The run view reloads its panels
        from the effective `.vast` afterwards."""


    @abstractmethod
    def list_campaign_visualizations(
        self, campaign_id: str
    ) -> CampaignVisualizationsResponse:
        """Return the campaign's ``evaluation.visualization`` notebook workloads
        (from its snapshot ``.vast``) + the node levels each defines a notebook for.
        Drives the Explorer's visualization tabs."""

    @abstractmethod
    def render_campaign_notebook(
        self, campaign_id: str, workload: str, level: str,
        config_name: str = "", run_id: Optional[int] = None, theme: str = "light",
        batch: Optional[int] = None,
    ) -> str:
        """Execute *workload*'s ``level`` notebook for the selected node and return the
        exported HTML. ``DATA_DIR`` is the node's directory: the campaign root
        (``campaign``), ``<root>/<config_name>`` (``config``), or
        ``<root>/<config_name>/<run_id>`` (``run``). The dir is resolved per transport
        (local disk / object-store fetch), so the same code serves both backends.
        ``theme`` (``'light'``/``'dark'``) drives the exported HTML's nbconvert theme so
        the render can match the web UI's colour scheme.

        A ``batch`` node is the exception: it has no directory of its own (a search
        campaign's configs are flat under the campaign root), so it gets the campaign root
        as ``DATA_DIR`` and *batch* is injected into the notebook as ``BATCH``. Passing
        *batch* for any other level is harmless but pointless."""

    # -- lifecycle ----------------------------------------------------------

    def shutdown(self) -> None:
        """Release resources when the service is stopping (Ctrl+C on ``vast serve``).

        Called once from the app's lifespan teardown. The default is a no-op;
        implementations that drive campaigns in-process (the local transport)
        override it to stop still-running campaigns so their containers don't
        outlive the process. Must not raise.
        """
