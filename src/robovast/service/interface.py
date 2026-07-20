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

Campaign **status** reuses :class:`robovast.execution.control_server.Status`
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
from typing import Optional

from pydantic import BaseModel, Field

# Reused verbatim — the controller's live status model. (The old ``Command`` /
# ``CommandResult`` RPC envelopes are gone: the controller runs in-process now, so
# ``stop`` is a direct call rather than an HTTP command to a controller pod.)
from robovast.execution.control_server import Phase, Status  # noqa: F401

# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class CreateCampaignRequest(BaseModel):
    """Start a campaign from a workspace's current project.

    ``backend`` is intentionally **absent**: it is implicit in *which* service
    the client is talking to (an in-process/local ``vast serve`` uses Docker; an
    in-cluster service uses Kubernetes). Callers select the backend by choosing
    the deployment, not per call.
    """

    workspace_id: str
    config_path: str = ""            # which .vast to run (workspace-relative); "" = the one .vast
    config_filter: str = ""          # optional glob to run only matching configs
    runs: int = 1                    # runs per configuration
    postprocess: bool = True         # trigger analysis postprocessing once when done
    upload_to_share: bool = False    # stream a raw (pre-postprocess) archive to the share


class CampaignRef(BaseModel):
    """Identifies a launched campaign. Self-contained and workspace-independent."""

    campaign_id: str


class CampaignSummary(BaseModel):
    """One row of :meth:`RobovastInterface.list_campaigns`.

    Campaigns are workspace-independent, so this carries no ``workspace_id``.
    """

    campaign_id: str
    phase: str = Phase.UNKNOWN       # open vocabulary; see the Phase enum
    postprocessed: bool = False      # configured postprocessing pipelines have run
    num_runs: int = 0
    num_passed: int = 0
    num_failed: int = 0
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


class ListCampaignsRequest(BaseModel):
    """Paginated global campaign listing (campaigns are not workspace-scoped)."""

    limit: int = 20
    offset: int = 0


class ListCampaignsResponse(BaseModel):
    campaigns: list[CampaignSummary] = Field(default_factory=list)
    total: int = 0


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
    status: str = "pending"          # running | pending | completed | failed
    display_name: Optional[str] = None


class JobCounts(BaseModel):
    """Aggregate job status counts for a campaign's current batch."""

    running: int = 0
    pending: int = 0
    completed: int = 0
    failed: int = 0
    total: int = 0


class ListJobsResponse(BaseModel):
    """Live per-job list + aggregate counts for one campaign's current batch."""

    jobs: list[JobSummary] = Field(default_factory=list)
    counts: JobCounts = Field(default_factory=JobCounts)


class ActionResult(BaseModel):
    """Generic ``ok``/``message`` result for state-changing actions."""

    ok: bool = True
    message: Optional[str] = None


class PostprocessingInfo(BaseModel):
    """Effective postprocessing entries for a campaign + override history."""

    campaign_id: str
    source: str = ""                 # which .vast is effective (snapshot or rev-N)
    entries: list = Field(default_factory=list)
    revisions: list[int] = Field(default_factory=list)


class UpdatePostprocessingRequest(BaseModel):
    campaign_id: str
    entries: list


class PostprocessingRevision(BaseModel):
    campaign_id: str
    revision: int
    entries: list = Field(default_factory=list)


class PanelsSource(BaseModel):
    """The run-view ``visualization:`` block as editable YAML text."""

    campaign_id: str
    source: str = ""                 # which .vast is effective (snapshot or rev-N)
    content: str = ""                # YAML text of the ``visualization:`` block


class UpdatePanelsSourceRequest(BaseModel):
    campaign_id: str
    content: str


class RunPostprocessingRequest(BaseModel):
    campaign_id: str
    force: bool = False
    skip: list[str] = Field(default_factory=list)


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
    api_version: str = "0"
    backend: Optional[str] = None    # "docker" | "kubernetes" (informational)


class ResourceUsage(BaseModel):
    """Live compute capacity and current usage of the service's execution backend.

    Backend-neutral by design: the local↔cluster difference is resolved inside the
    service (``LocalTransport`` reads the host via ``psutil``; ``ClusterService``
    reads the Kubernetes nodes), so a consumer — the UI chip or the MCP tool — reads
    the same fields regardless of where it runs and never branches on ``backend``.

    ``cpu_used`` / ``memory_used`` semantics differ by backend but answer the same
    question ("how much is currently claimed"): on the **cluster** they are the sum
    of resource *requests* of non-terminal pods (schedulability, matching how Kueue
    reasons about quota); on **local** they are live host utilisation. ``cpu_*`` are
    CPU cores; ``memory_*`` are bytes.

    ``parallel_runs`` is a backend-intrinsic flag, **not** a count: ``False`` means
    scenario runs execute one at a time (local Docker is single-flight), ``True``
    means they run in parallel bounded only by free capacity (cluster). How many runs
    actually fit is left to the consumer, which knows each project's per-run
    reservation — the service does not.

    ``jobs_running`` / ``jobs_pending`` are backend-wide scenario-run counts (every
    campaign, not one), pod-accurate: ``running`` counts pods actually in phase
    ``Running`` and ``pending`` the rest still waiting — the same distinction the
    per-campaign :class:`JobCounts` draws. Both are ``0`` on backends that don't run
    Jobs (local Docker).
    """

    backend: str                     # "docker" | "kubernetes" (informational only)
    cpu_capacity: float              # total cores (cluster allocatable / host logical CPUs)
    cpu_used: float                  # cores claimed (cluster pod requests / host utilisation)
    memory_capacity_bytes: int
    memory_used_bytes: int
    parallel_runs: bool              # runs execute in parallel? cluster=True, local=False
    jobs_running: int = 0            # scenario-run pods in phase Running, backend-wide
    jobs_pending: int = 0            # scenario-run pods admitted/queued but not yet Running


# -- workspaces (editable project inputs; independent of campaigns) ---------


class CreateWorkspaceRequest(BaseModel):
    name: str = ""


class WorkspaceInfo(BaseModel):
    workspace_id: str
    name: str = ""
    created_at: Optional[str] = None
    #: True for a directory pinned read-only with ``vast serve --workspace-dir``:
    #: used in place, so writes are refused — edit the files on disk instead.
    read_only: bool = False


class ListWorkspacesResponse(BaseModel):
    workspaces: list[WorkspaceInfo] = Field(default_factory=list)


class FileMeta(BaseModel):
    """Result of a write/upload — metadata only, never the content.

    Echoing content back would double its token cost for no benefit.
    """

    path: str
    bytes: int = 0
    sha256: str = ""
    executable: bool = False


class WriteFileRequest(BaseModel):
    """Inline authoring — ``.vast``/``.osc`` only (enforced server-side)."""

    workspace_id: str
    path: str
    content: str


class EditFileRequest(BaseModel):
    """Old/new-string edit so the validate→fix loop sends a diff, not a file."""

    workspace_id: str
    path: str
    old_string: str
    new_string: str


class FileContent(BaseModel):
    path: str
    content: str


class ListFilesResponse(BaseModel):
    files: list[FileMeta] = Field(default_factory=list)


class CreateUploadRequest(BaseModel):
    """Grant an HTTP PUT for any file type (keeps bytes out of the token stream)."""

    workspace_id: str
    path: str
    executable: bool = False


class UploadGrant(BaseModel):
    """A one-time, TTL-scoped upload grant; PUT the bytes to ``url``."""

    token: str
    path: str
    expires_in: int
    url: Optional[str] = None    # absolute when issued by an HTTP service


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


class PreviewConfiguration(BaseModel):
    """One resolved configuration a ``.vast`` expands to."""

    name: str
    parameters: dict = Field(default_factory=dict)
    #: Per-variation preview descriptors (Phase 2b — Module Federation remotes /
    #: host-native built-ins); empty when a variation contributes no preview.
    previews: list = Field(default_factory=list)


class PreviewResponse(BaseModel):
    """Result of :meth:`RobovastInterface.preview_configurations`."""

    configs: int = 0
    runs_per_config: int = 0
    total_trials: int = 0
    configurations: list[PreviewConfiguration] = Field(default_factory=list)
    truncated: bool = False


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


class DataDescribe(BaseModel):
    """Schema of a campaign's ``data.db`` (+ attached ``campaign.db``).

    Each ``tables`` entry is ``{schema, table, columns, rows}`` (passed through from
    the query helper verbatim — kept as a dict so ``schema`` stays that key across
    every client path).
    """

    campaign_id: str
    tables: list[dict] = Field(default_factory=list)
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


class CampaignPlotsResponse(BaseModel):
    """User-declared plots for a campaign (from its snapshot ``.vast``
    ``evaluation.plots``). Each entry is ``{title, query, vega_lite}``."""

    campaign_id: str
    plots: list[dict] = Field(default_factory=list)


class CampaignPanelsResponse(BaseModel):
    """The run-view panels declared for a campaign (its snapshot ``.vast``
    top-level ``visualization.panels``). Each entry is the raw panel dict
    (``type`` + ``position`` + panel-specific data bindings), rendered by the
    web run-view against the campaign's ``data.db``. ``timeline`` (optional,
    ``visualization.timeline``) names the table + column that defines the
    playback range for non-ROS runs."""

    campaign_id: str
    panels: list[dict] = Field(default_factory=list)
    timeline: Optional[dict] = None


class CostmapFrame(BaseModel):
    """One nav2 OccupancyGrid frame for the run-view costmap panel: the frame from the
    ``costmaps`` table nearest a requested time, delivered untruncated. ``data`` is the
    zlib-compressed, base64-encoded int8 grid (row-major, -1=unknown/0=free/1..100=cost);
    the map spans ``width*resolution`` by ``height*resolution`` meters, and
    ``origin_*`` is the pose of cell (0,0)'s corner in ``frame_id``."""

    t: float
    frame_id: str
    resolution: float
    width: int
    height: int
    origin_x: float
    origin_y: float
    origin_yaw: float
    data: str


class CampaignVisualization(BaseModel):
    """One ``evaluation.visualization`` notebook workload + the node levels it
    defines a notebook for (a subset of ``run``/``config``/``campaign`` — ``batch``
    is omitted, the web tree has no batch node)."""

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


API_VERSION = "0"


class Routes:
    """Canonical HTTP paths — shared by the service app and the HTTP client so
    the two bindings cannot drift. Phase 0 (campaign lifecycle + version)."""

    VERSION = "/version"
    HEALTHZ = "/healthz"
    USAGE = "/usage"
    CAMPAIGNS = "/campaigns"
    WORKSPACES = "/workspaces"
    #: The file side channel: PUT bytes here with a create_upload token.
    UPLOAD = "/uploads/{token}"
    #: Authoring help — static, no workspace (config editor).
    CONFIG_SCHEMA = "/config/schema"
    VARIATION_TYPES = "/variation_types"

    @staticmethod
    def workspace(workspace_id: str) -> str:
        return f"/workspaces/{workspace_id}"

    @staticmethod
    def workspace_files(workspace_id: str) -> str:
        return f"/workspaces/{workspace_id}/files"

    @staticmethod
    def workspace_file(workspace_id: str) -> str:
        # path passed as a query param so nested paths need no encoding games
        return f"/workspaces/{workspace_id}/file"

    @staticmethod
    def workspace_edit(workspace_id: str) -> str:
        return f"/workspaces/{workspace_id}/edit"

    @staticmethod
    def workspace_upload(workspace_id: str) -> str:
        return f"/workspaces/{workspace_id}/uploads"

    @staticmethod
    def workspace_validate(workspace_id: str) -> str:
        return f"/workspaces/{workspace_id}/validate"

    @staticmethod
    def workspace_preview(workspace_id: str) -> str:
        return f"/workspaces/{workspace_id}/preview"

    @staticmethod
    def variation_asset(name: str, path: str) -> str:
        # A variation plugin's web-preview asset (Module Federation remoteEntry + chunks).
        return f"/variation_types/{name}/assets/{path}"

    @staticmethod
    def upload(token: str) -> str:
        return f"/uploads/{token}"

    @staticmethod
    def campaign_status(campaign_id: str) -> str:
        return f"/campaigns/{campaign_id}/status"

    @staticmethod
    def campaign_stop(campaign_id: str) -> str:
        return f"/campaigns/{campaign_id}/stop"

    @staticmethod
    def campaign_logs(campaign_id: str) -> str:
        return f"/campaigns/{campaign_id}/logs"

    @staticmethod
    def campaign_jobs(campaign_id: str) -> str:
        return f"/campaigns/{campaign_id}/jobs"

    @staticmethod
    def job_log(campaign_id: str) -> str:
        # ``job_name`` is a query param (it may contain '/', e.g. a local
        # "<config>/<run>" id), so it never has to be path-encoded.
        return f"/campaigns/{campaign_id}/job-log"

    #: Object-store bucket cleanup (server-side; not campaign-scoped in the path
    #: because it also serves the "all campaigns" case).
    CLEANUP_DATA = "/campaigns/cleanup-data"

    @staticmethod
    def campaign_postprocessing(campaign_id: str) -> str:
        return f"/campaigns/{campaign_id}/postprocessing"

    @staticmethod
    def campaign_postprocessing_run(campaign_id: str) -> str:
        return f"/campaigns/{campaign_id}/postprocessing/run"

    @staticmethod
    def campaign_describe(campaign_id: str) -> str:
        return f"/campaigns/{campaign_id}/describe"

    @staticmethod
    def campaign_query(campaign_id: str) -> str:
        return f"/campaigns/{campaign_id}/query"

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
    def campaign_costmap(campaign_id: str) -> str:
        return f"/campaigns/{campaign_id}/costmap"

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
        """Report the execution backend's CPU/memory capacity + current usage."""

    # -- workspaces (editable project inputs) -------------------------------

    @abstractmethod
    def create_workspace(self, request: CreateWorkspaceRequest) -> WorkspaceInfo:
        """Create an empty workspace to author a project in."""

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

    # -- workspace files ----------------------------------------------------

    @abstractmethod
    def write_project_file(self, request: WriteFileRequest) -> FileMeta:
        """Write a ``.vast``/``.osc`` file inline (other types → :meth:`create_upload`)."""

    @abstractmethod
    def edit_project_file(self, request: EditFileRequest) -> FileMeta:
        """Replace a unique string in a ``.vast``/``.osc`` file (token-cheap fix loop)."""

    @abstractmethod
    def read_project_file(self, workspace_id: str, path: str) -> FileContent:
        """Read a workspace file's text."""

    @abstractmethod
    def list_project_files(self, workspace_id: str) -> ListFilesResponse:
        """List the workspace's files with metadata."""

    @abstractmethod
    def delete_project_file(self, workspace_id: str, path: str) -> ActionResult:
        """Delete a workspace file."""

    @abstractmethod
    def create_upload(self, request: CreateUploadRequest) -> UploadGrant:
        """Grant a one-time, TTL-scoped HTTP PUT for any file type."""

    # -- campaign lifecycle -------------------------------------------------

    @abstractmethod
    def create_campaign(self, request: CreateCampaignRequest) -> CampaignRef:
        """Validate the workspace's project and start a campaign; return its id.

        Returns immediately (fire-and-forget); poll :meth:`get_status`.
        """

    @abstractmethod
    def get_status(self, campaign_id: str) -> Status:
        """Return the campaign's live :class:`Status` (phase, progress, history)."""

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
        only — the running pod's log on the cluster, the live ``logs/system.log`` file
        locally. Raises if the job's log source is gone.
        """

    @abstractmethod
    def stop(self, campaign_id: str) -> ActionResult:
        """Request a cooperative stop of a running campaign."""

    @abstractmethod
    def list_campaigns(
        self, request: Optional[ListCampaignsRequest] = None
    ) -> ListCampaignsResponse:
        """List campaigns known to this service (global, newest first)."""

    @abstractmethod
    def cleanup_campaign_data(self, request: CleanupDataRequest) -> ActionResult:
        """Delete campaign result bucket(s) from the object store.

        Runs server-side because the service holds the cluster config (object-store
        credentials) and the authoritative live-campaign set — so the CLI needs no
        cluster credentials. Live campaigns are skipped unless ``force`` names one.
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
    def run_postprocessing(self, request: RunPostprocessingRequest) -> ActionResult:
        """(Re)run analysis postprocessing for one campaign with the effective config."""

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
    ) -> DataQueryResult:
        """Run a read-only ``SELECT`` over a campaign's data (``campaign.db`` attached)."""

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
    def get_costmap_frame(
        self, campaign_id: str, config_name: str, run_id: int, topic: str, t: float,
    ) -> Optional[CostmapFrame]:
        """Return the ``costmaps`` frame nearest time ``t`` for one run's ``topic``
        (a nav2 OccupancyGrid layer), delivered untruncated for the run-view costmap
        panel. ``None`` when the run/topic has no frame."""

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
    ) -> str:
        """Execute *workload*'s ``level`` notebook for the selected node and return the
        exported HTML. ``DATA_DIR`` is the node's directory: the campaign root
        (``campaign``), ``<root>/<config_name>`` (``config``), or
        ``<root>/<config_name>/<run_id>`` (``run``). The dir is resolved per transport
        (local disk / object-store fetch), so the same code serves both backends.
        ``theme`` (``'light'``/``'dark'``) drives the exported HTML's nbconvert theme so
        the render can match the web UI's colour scheme."""

    # -- lifecycle ----------------------------------------------------------

    def shutdown(self) -> None:
        """Release resources when the service is stopping (Ctrl+C on ``vast serve``).

        Called once from the app's lifespan teardown. The default is a no-op;
        implementations that drive campaigns in-process (the local transport)
        override it to stop still-running campaigns so their containers don't
        outlive the process. Must not raise.
        """
