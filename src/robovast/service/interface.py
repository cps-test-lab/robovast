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
from robovast.execution.control_server import Status  # noqa: F401

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


class CampaignRef(BaseModel):
    """Identifies a launched campaign. Self-contained and workspace-independent."""

    campaign_id: str


class CampaignSummary(BaseModel):
    """One row of :meth:`RobovastInterface.list_campaigns`.

    Campaigns are workspace-independent, so this carries no ``workspace_id``.
    """

    campaign_id: str
    phase: str = "unknown"           # matches Status.phase vocabulary
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


class RunPostprocessingRequest(BaseModel):
    campaign_id: str
    force: bool = False
    skip: list[str] = Field(default_factory=list)


class UploadToShareRequest(BaseModel):
    """Optional credential overrides for an upload-to-share attempt.

    Upload-to-share is a **stateless, repeatable** operation: the finished campaign
    lives in the object store, so a failed upload is retried by simply calling this
    again — optionally with corrected credentials — rather than by keeping a
    controller process parked and waiting for a retrigger (which is how it used to
    work, back when the campaign only existed inside that live pod).

    *overrides* are ``{ENV_VAR: value}`` share settings applied to the attempt
    (e.g. a fixed password, or a different ``ROBOVAST_SHARE_TYPE`` entirely). Empty
    means "use the service's configured share environment".
    """
    overrides: dict = Field(default_factory=dict)


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


# -- workspaces (editable project inputs; independent of campaigns) ---------


class CreateWorkspaceRequest(BaseModel):
    name: str = ""


class WorkspaceInfo(BaseModel):
    workspace_id: str
    name: str = ""
    created_at: Optional[str] = None


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


# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------


API_VERSION = "0"


class Routes:
    """Canonical HTTP paths — shared by the service app and the HTTP client so
    the two bindings cannot drift. Phase 0 (campaign lifecycle + version)."""

    VERSION = "/version"
    HEALTHZ = "/healthz"
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
    def campaign_upload_to_share(campaign_id: str) -> str:
        return f"/campaigns/{campaign_id}/upload-to-share"

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


class RobovastInterface(ABC):
    """The full RoboVAST operation surface (Phase 0 subset defined here).

    Implemented by the server core and by the client transports; both satisfy
    the identical contract so callers are transport-agnostic.
    """

    # -- version / health ---------------------------------------------------

    @abstractmethod
    def version(self) -> VersionInfo:
        """Report the implementation's RoboVAST + API version (handshake)."""

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
    def stop(self, campaign_id: str) -> ActionResult:
        """Request a cooperative stop of a running campaign."""

    @abstractmethod
    def list_campaigns(
        self, request: Optional[ListCampaignsRequest] = None
    ) -> ListCampaignsResponse:
        """List campaigns known to this service (global, newest first)."""

    @abstractmethod
    def upload_to_share(self, campaign_id: str,
                        overrides: Optional[dict] = None) -> ActionResult:
        """Upload the finished campaign's ``tar.gz`` to the external share.

        Stateless and repeatable: it reads the campaign from its durable home (the
        object store), so a failed upload is retried by calling this again — pass
        *overrides* (``{ENV_VAR: value}``) to correct or switch the share settings.
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
