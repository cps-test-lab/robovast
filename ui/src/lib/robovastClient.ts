// Typed REST client — the browser binding of the RobovastInterface contract. It mirrors
// src/robovast/service/interface.py (the `Routes` paths + request/response models) exactly, the same
// way the Python HTTPTransport does; the UI never talks to anything but the service. Keep this file the
// single seam: if interface.py changes, change it here.
//
// Base URL is "" by default — the service serves this SPA same-origin, so relative paths hit its API.
// In dev the Vite proxy forwards the API prefixes to a running `vast serve`, so "" works there too.
// Override with VITE_ROBOVAST_URL to point at an arbitrary service.

const BASE = (import.meta.env.VITE_ROBOVAST_URL ?? '').replace(/\/$/, '')

// -- interface models (mirror interface.py / control_server.Status) ---------

export interface VersionInfo {
  robovast_version: string
  api_version: string
  backend?: string | null
}

// Live capacity/usage of the service's execution backend (mirrors
// interface.py:ResourceUsage). Backend-neutral: `parallel_runs` says whether runs
// go one-at-a-time (local Docker) or in parallel (cluster); CPU in cores, memory
// in bytes. `used` is host utilization locally / summed pod requests on the cluster.
export interface ResourceUsage {
  backend: string
  cpu_capacity: number
  cpu_used: number
  memory_capacity_bytes: number
  memory_used_bytes: number
  parallel_runs: boolean
}

export interface CampaignSummary {
  campaign_id: string
  phase: string
  postprocessed: boolean
  num_runs: number
  num_passed: number
  num_failed: number
  started_at?: string | null
  finished_at?: string | null
}

export interface ListCampaignsResponse {
  campaigns: CampaignSummary[]
  total: number
}

// Newest first. Prefer started_at when the backend provides it; fall back to
// campaign_id, which encodes the timestamp (campaign-YYYY-MM-DD-HHMMSS) and so
// sorts lexicographically by time. Returns a new array (never mutates input).
const sortKey = (c: CampaignSummary) => c.started_at ?? c.campaign_id
export const campaignsNewestFirst = (campaigns: CampaignSummary[]) =>
  [...campaigns].sort((a, b) =>
    sortKey(a) < sortKey(b) ? 1 : sortKey(a) > sortKey(b) ? -1 : 0)

export interface CreateCampaignRequest {
  workspace_id: string
  config_path?: string
  config_filter?: string
  runs?: number
  postprocess?: boolean
  upload_to_share?: boolean
}

export interface CampaignRef {
  campaign_id: string
}

export interface ActionResult {
  ok: boolean
  message?: string | null
}

// control_server.Status (reused verbatim by the interface) — the live monitor model.
export interface RunProgress {
  completed: number
  total: number
}

export interface BudgetItem {
  label: string
  current?: number | null
  limit: number
  done: boolean
}

export interface Status {
  phase: string
  stage?: string | null
  mode?: string | null
  campaign_id?: string | null
  batch: number
  batches_done: number
  budget: BudgetItem[]
  runs: RunProgress
  best_objective?: number | null
  batch_history: Record<string, unknown>[]
  stop?: Record<string, unknown> | null
  error?: string | null
  share_provider?: string | null
  extra: Record<string, unknown>
  updated_at: number
}

// An incremental slice of a campaign's controller.log. Poll from `next_offset`
// and append `text`; stop once `eof` is set (mirrors service/interface.py:LogChunk).
export interface LogChunk {
  text: string
  next_offset: number
  eof: boolean
}

// One execution unit of a campaign's current batch (a run locally, a k8s Job on
// the cluster). Mirrors interface.py:JobSummary/JobCounts/ListJobsResponse.
export interface JobSummary {
  job_name: string
  status: string // running | pending | completed | failed
  display_name?: string | null
}

export interface JobCounts {
  running: number
  pending: number
  completed: number
  failed: number
  total: number
}

export interface ListJobsResponse {
  jobs: JobSummary[]
  counts: JobCounts
}

export interface WorkspaceInfo {
  workspace_id: string
  name: string
  created_at?: string | null
  /** A directory pinned read-only with `vast serve --workspace-dir`: used in
   *  place, so it cannot be edited through the service (edit files on disk). */
  read_only?: boolean
}

export interface ListWorkspacesResponse {
  workspaces: WorkspaceInfo[]
}

export interface FileMeta {
  path: string
  bytes: number
  sha256: string
  executable: boolean
}

export interface ListFilesResponse {
  files: FileMeta[]
}

export interface FileContent {
  path: string
  content: string
}

export interface UploadGrant {
  token: string
  path: string
  expires_in: number
  url?: string | null
}

// -- config-editor models (mirror interface.py) ----------------------------

export interface ValidationProblem {
  stage: string
  config?: string | null
  field?: string | null
  message: string
}

export interface ValidationReport {
  valid: boolean
  problems: ValidationProblem[]
  configs: number
  runs_per_config: number
  total_trials: number
}

export interface VariationRemote {
  name: string
  remote_entry_url: string
  module: string
}

export interface VariationPreview {
  variation_type: string
  params: Record<string, unknown>
  remote?: VariationRemote | null
}

export interface PreviewConfiguration {
  name: string
  parameters: Record<string, unknown>
  previews: VariationPreview[]
}

export interface PreviewResponse {
  configs: number
  runs_per_config: number
  total_trials: number
  configurations: PreviewConfiguration[]
  truncated: boolean
}

export interface VariationTypeParam {
  name: string
  type: string
  required: boolean
  default?: unknown
  description?: string | null
}

export interface VariationTypeInfo {
  name: string
  summary: string
  params: VariationTypeParam[]
}

export interface VariationTypesResponse {
  types: VariationTypeInfo[]
}

// -- results data query (eval viewer) ---------------------------------------

export interface DataTable {
  schema: string
  table: string
  columns: string[]
  rows: number | null
}

export interface DataDescribe {
  campaign_id: string
  tables: DataTable[]
  note: string
}

export interface DataQueryResult {
  campaign_id: string
  columns: string[]
  rows: Record<string, unknown>[]
  row_count: number
  truncated: boolean
}

export interface PlotSpec {
  title: string
  query: string
  vega_lite: Record<string, unknown>
}

export interface CampaignPlotsResponse {
  campaign_id: string
  plots: PlotSpec[]
}

// The run-view panels declared for a campaign (its .vast top-level visualization.panels). Each entry
// is the raw panel dict (type + position + panel-specific data bindings); the UI normalizes it.
export interface CampaignPanelsResponse {
  campaign_id: string
  panels: Record<string, unknown>[]
}

// One evaluation.visualization notebook workload + the node levels it defines a notebook for
// (subset of run/config/campaign). The Explorer shows a tab per workload and renders the
// workload's notebook, executed against the selected node, as HTML.
export interface CampaignVisualization {
  name: string
  levels: string[]
}

export interface CampaignVisualizationsResponse {
  campaign_id: string
  workloads: CampaignVisualization[]
}

// One nav2 OccupancyGrid frame for the costmap panel (nearest a requested time), delivered
// untruncated. `data` is zlib-compressed, base64-encoded int8 cells (row-major, -1..100). The map
// spans width*resolution by height*resolution meters; origin_* is cell (0,0)'s corner pose in frame_id.
export interface CostmapFrame {
  t: number
  frame_id: string
  resolution: number
  width: number
  height: number
  origin_x: number
  origin_y: number
  origin_yaw: number
  data: string
}

// -- transport --------------------------------------------------------------

export class RobovastError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message)
    this.name = 'RobovastError'
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!res.ok) {
    // FastAPI errors carry {detail}; fall back to the status text.
    let detail = res.statusText
    try {
      const j = (await res.json()) as { detail?: string }
      if (j?.detail) detail = j.detail
    } catch {
      /* non-JSON body */
    }
    throw new RobovastError(res.status, detail)
  }
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

// -- the interface (Phase-0 subset the M1 UI needs) -------------------------

export const robovast = {
  version: () => request<VersionInfo>('GET', '/version'),

  resourceUsage: () => request<ResourceUsage>('GET', '/usage'),

  // Direct URL of a campaign's postprocessed tar.gz (a GET the browser downloads).
  // Cluster services stream it from the object store; a local service returns 409.
  archiveUrl: (campaignId: string) =>
    `${BASE}/campaigns/${encodeURIComponent(campaignId)}/archive`,

  listWorkspaces: () => request<ListWorkspacesResponse>('GET', '/workspaces'),

  listCampaigns: (limit = 20, offset = 0) =>
    request<ListCampaignsResponse>('GET', `/campaigns?limit=${limit}&offset=${offset}`),

  getStatus: (campaignId: string) =>
    request<Status>('GET', `/campaigns/${encodeURIComponent(campaignId)}/status`),

  getCampaignLogs: (campaignId: string, offset = 0) =>
    request<LogChunk>(
      'GET',
      `/campaigns/${encodeURIComponent(campaignId)}/logs?offset=${offset}`,
    ),

  createCampaign: (req: CreateCampaignRequest) =>
    request<CampaignRef>('POST', '/campaigns', {
      config_path: '',
      config_filter: '',
      runs: 1,
      postprocess: true,
      upload_to_share: false,
      ...req,
    }),

  listJobs: (campaignId: string) =>
    request<ListJobsResponse>('GET', `/campaigns/${encodeURIComponent(campaignId)}/jobs`),

  getJobLog: (campaignId: string, jobName: string, offset = 0) =>
    request<LogChunk>(
      'GET',
      `/campaigns/${encodeURIComponent(campaignId)}/job-log?job_name=${encodeURIComponent(
        jobName,
      )}&offset=${offset}`,
    ),

  stop: (campaignId: string) =>
    request<ActionResult>('POST', `/campaigns/${encodeURIComponent(campaignId)}/stop`),

  // -- workspaces & files ---------------------------------------------------

  createWorkspace: (name = '') => request<WorkspaceInfo>('POST', '/workspaces', { name }),

  deleteWorkspace: (id: string) =>
    request<ActionResult>('DELETE', `/workspaces/${encodeURIComponent(id)}`),

  listProjectFiles: (id: string) =>
    request<ListFilesResponse>('GET', `/workspaces/${encodeURIComponent(id)}/files`),

  readProjectFile: (id: string, path: string) =>
    request<FileContent>(
      'GET',
      `/workspaces/${encodeURIComponent(id)}/file?path=${encodeURIComponent(path)}`,
    ),

  writeProjectFile: (id: string, path: string, content: string) =>
    request<FileMeta>('POST', `/workspaces/${encodeURIComponent(id)}/file`, {
      workspace_id: id,
      path,
      content,
    }),

  deleteProjectFile: (id: string, path: string) =>
    request<ActionResult>(
      'DELETE',
      `/workspaces/${encodeURIComponent(id)}/file?path=${encodeURIComponent(path)}`,
    ),

  // Upload any (non-.vast/.osc) file via the TTL PUT side channel.
  uploadFile: async (id: string, path: string, data: Blob) => {
    const grant = await request<UploadGrant>(
      'POST',
      `/workspaces/${encodeURIComponent(id)}/uploads`,
      { workspace_id: id, path },
    )
    const res = await fetch(`${BASE}${grant.url ?? `/uploads/${grant.token}`}`, {
      method: 'PUT',
      body: data,
    })
    if (!res.ok) throw new RobovastError(res.status, `upload failed: ${res.statusText}`)
    return (await res.json()) as FileMeta
  },

  // -- config editor --------------------------------------------------------

  validateProject: (id: string, path = '') =>
    request<ValidationReport>('POST', `/workspaces/${encodeURIComponent(id)}/validate`, {
      path,
    }),

  previewConfigurations: (id: string, maxConfigs = 0, path = '') =>
    request<PreviewResponse>('POST', `/workspaces/${encodeURIComponent(id)}/preview`, {
      max_configs: maxConfigs,
      path,
    }),

  getConfigSchema: () => request<Record<string, unknown>>('GET', '/config/schema'),

  listVariationTypes: () => request<VariationTypesResponse>('GET', '/variation_types'),

  // -- eval / results data query --------------------------------------------

  describeCampaignData: (campaignId: string) =>
    request<DataDescribe>('GET', `/campaigns/${encodeURIComponent(campaignId)}/describe`),

  queryCampaignDataSql: (campaignId: string, sql: string, maxRows = 500) =>
    request<DataQueryResult>('POST', `/campaigns/${encodeURIComponent(campaignId)}/query`, {
      sql,
      max_rows: maxRows,
    }),

  listCampaignPlots: (campaignId: string) =>
    request<CampaignPlotsResponse>('GET', `/campaigns/${encodeURIComponent(campaignId)}/plots`),

  listCampaignPanels: (campaignId: string) =>
    request<CampaignPanelsResponse>('GET', `/campaigns/${encodeURIComponent(campaignId)}/panels`),

  // The evaluation.visualization notebook workloads a campaign declares — drives the Explorer tabs.
  listCampaignVisualizations: (campaignId: string) =>
    request<CampaignVisualizationsResponse>(
      'GET',
      `/campaigns/${encodeURIComponent(campaignId)}/visualizations`,
    ),

  // Execute a workload's notebook for the selected node and return its HTML (raw text, not JSON —
  // the Explorer feeds it to an iframe via a Blob URL). A cold render runs the notebook server-side
  // and can take seconds; FileCache makes repeat views instant.
  fetchNotebookHtml: async (
    campaignId: string,
    opts: {
      workload: string
      level: string
      configName?: string
      runId?: number | string
      theme?: 'light' | 'dark'
    },
  ): Promise<string> => {
    const params = new URLSearchParams({ workload: opts.workload, level: opts.level })
    if (opts.configName) params.set('config_name', opts.configName)
    if (opts.runId !== undefined) params.set('run_id', String(opts.runId))
    if (opts.theme) params.set('theme', opts.theme)
    const res = await fetch(
      `${BASE}/campaigns/${encodeURIComponent(campaignId)}/notebook?${params.toString()}`,
    )
    if (!res.ok) {
      let detail = res.statusText
      try {
        const j = (await res.json()) as { detail?: string }
        if (j?.detail) detail = j.detail
      } catch {
        /* non-JSON body */
      }
      throw new RobovastError(res.status, detail)
    }
    return res.text()
  },

  // Nearest costmap frame for one run's layer (topic) at time t. Returns null when the run/topic has
  // no frame (204). The panel decodes `data` (inflate → Int8Array) and draws it.
  costmapFrame: (
    campaignId: string,
    configName: string,
    runId: number | string,
    topic: string,
    t: number,
  ) =>
    request<CostmapFrame | null>(
      'GET',
      `/campaigns/${encodeURIComponent(campaignId)}/costmap?config_name=${encodeURIComponent(
        configName,
      )}&run_id=${encodeURIComponent(String(runId))}&topic=${encodeURIComponent(topic)}&t=${t}`,
    ),

  runPostprocessing: (campaignId: string, force = false) =>
    request<ActionResult>(
      'POST',
      `/campaigns/${encodeURIComponent(campaignId)}/postprocessing/run`,
      { campaign_id: campaignId, force, skip: [] },
    ),
}
