// Typed REST client — the browser binding of the RobovastInterface contract, the same one
// the Python HTTPTransport binds. The UI never talks to anything but the service.
//
// The **types** are generated from the service's own OpenAPI schema
// (`npm run generate:api` → api.generated.ts) and re-exported below. They used to be
// hand-written: 41 interfaces mirroring pydantic models with nothing tying them together,
// so a field renamed in interface.py stayed "correct" here until something broke at
// runtime. The **functions** stay hand-written — they carry real behaviour (SSE URLs, the
// upload side channel, error mapping) that codegen has no opinion about.
//
// Base URL is "" by default — the service serves this SPA same-origin, so relative paths hit its API.
// In dev the Vite proxy forwards the API prefixes to a running `vast serve`, so "" works there too.
// Override with VITE_ROBOVAST_URL to point at an arbitrary service.

import type { components } from './api.generated'

type Schemas = components['schemas']

const BASE = (import.meta.env.VITE_ROBOVAST_URL ?? '').replace(/\/$/, '')

// -- interface models (mirror interface.py / control_server.Status) ---------

export type VersionInfo = Schemas['VersionInfo']

// Live capacity/usage of the service's execution backend (mirrors
// interface.py:ResourceUsage). Backend-neutral: `parallel_runs` says whether runs
// go one-at-a-time (local Docker) or in parallel (cluster); CPU in cores, memory
// in bytes. `used` is host utilization locally / summed pod requests on the cluster.
export type ResourceUsage = Schemas['ResourceUsage']

export type CampaignSummary = Schemas['CampaignSummary']

export type ListCampaignsResponse = Schemas['ListCampaignsResponse']

// Campaign lists arrive newest-first: the service orders them by recorded start time
// before it applies limit/offset, so the order and the page contents agree (see
// LocalTransport.list_campaigns). There is deliberately no client-side re-sort — a
// second key that had to agree with the backend's was itself the ordering bug, and
// `campaign_id` is not a usable key here because its `<name>-` prefix is user-supplied.

// The campaign lifecycle vocabulary carried by `phase` (mirrors the backend Phase enum). A string
// union rather than an enum so it stays a plain wire value; `CampaignSummary.phase` is still typed
// `string`, so an unexpected/future phase never fails to parse.
export type CampaignPhase =
  | 'initializing' | 'building' | 'starting' | 'plugin install' | 'variation' | 'running'
  | 'finishing' | 'postprocessing' | 'sharing'
  | 'finished' | 'failed' | 'stopped' | 'crashed' | 'unknown'

// Phases where the campaign is still working, so no final verdict exists yet.
// `initializing` is the first: the service has accepted the campaign and it is listed and
// addressable, but the lane's pre-flight (project push, image resolution) has not finished.
const RUNNING_PHASES: ReadonlySet<string> = new Set<CampaignPhase>([
  'initializing', 'building', 'starting', 'plugin install', 'variation', 'running',
  'finishing', 'postprocessing', 'sharing',
])

// The campaign is over, one way or another — the complement of RUNNING_PHASES, mirroring the
// backend's TERMINAL_PHASES. `crashed` and `unknown` are terminal too: `unknown` is what a campaign
// reconstructs to after a service restart that lost its live driver, so it must NOT be treated as
// still-running (that left a Stop button enabled on a campaign nothing was driving). Take a bare
// phase string, not a summary, so it also works on a live `Status.phase`.
export const isTerminalPhase = (phase: string | undefined): boolean =>
  !!phase && !RUNNING_PHASES.has(phase)

export const isRunning = (c: CampaignSummary) => RUNNING_PHASES.has(c.phase)
export const isFinished = (c: CampaignSummary) => c.phase === 'finished'
export const isFailed = (c: CampaignSummary) => c.phase === 'failed'
// Results are ready to explore only once the run finished AND its configured postprocessing
// pipelines ran: "finished" alone is reached *before* postprocessing chains, and a campaign that
// defines no postprocessing never gets the derived data.db the Results views query. The single gate
// for what the Results topic (Explorer / Run / Data) shows.
export const hasResults = (c: CampaignSummary) => isFinished(c) && c.postprocessed
// Whether the campaign recorded anything at all. `num_runs` is tallied from its `campaign.db`, so
// zero means there is no store to read — the campaign never started, or ended before writing one.
// Nothing can be replayed or queried for such a campaign, so the Run view does not offer it.
// A search campaign whose every draw failed to compose also has zero runs, but it *did* record
// why: hiding it left the one campaign most in need of inspection missing from the picker with no
// explanation, so it counts as having something to show.
export const hasRecordedRuns = (c: CampaignSummary) =>
  c.num_runs > 0 || (c.num_composition_failed ?? 0) > 0

export type CreateCampaignRequest = Schemas['CreateCampaignRequest']

// Mirrors interface.py:DESCRIPTION_MAX_LEN. Enforced here only to keep the field from
// growing past what a campaign card can show — the service is the authority and rejects
// a longer one.
export const DESCRIPTION_MAX_LEN = 200

export type CampaignRef = Schemas['CampaignRef']

export type ActionResult = Schemas['ActionResult']

// Whether a run's 3D geometry is ready, and what the wait is on if not. Mirrors CampaignDataStatus'
// job: say why you are about to wait, before you wait.
export type SceneStatus = Schemas['SceneStatus']

// control_server.Status (reused verbatim by the interface) — the live monitor model.
export type RunProgress = Schemas['RunProgress']

export type BudgetItem = Schemas['BudgetItem']

export type Status = Schemas['Status']

// An incremental slice of a campaign's controller.log. Poll from `next_offset`
// and append `text`; stop once `eof` is set (mirrors service/interface.py:LogChunk).
export type LogChunk = Schemas['LogChunk']

// One execution unit of a campaign's current batch (a run locally, a k8s Job on
// the cluster). Mirrors interface.py:JobSummary/JobCounts/ListJobsResponse.
export type JobSummary = Schemas['JobSummary']

export type JobCounts = Schemas['JobCounts']

export type ListJobsResponse = Schemas['ListJobsResponse']

export type WorkspaceInfo = Schemas['WorkspaceInfo']

export type ListWorkspacesResponse = Schemas['ListWorkspacesResponse']

export type FileMeta = Schemas['FileMeta']

export interface FileListing {
  /** The directory that was listed; every entry is relative to it. */
  address: string
  /** Names, directories suffixed with `/`. The UI never asks for `detail=true`, so
   *  the server's `detailed` shape is deliberately not modelled here. */
  entries: string[]
  /** Entry count *before* offset/limit, so a truncated page says how much it omitted. */
  total: number
  truncated: boolean
  recursive: boolean
}

export interface FileText {
  address: string
  total_lines: number
  returned_lines: number
  offset: number
  content: string
}

export type UploadGrant = Schemas['UploadGrant']

// -- config-editor models (mirror interface.py) ----------------------------

export type ValidationProblem = Schemas['ValidationProblem']

export type ValidationReport = Schemas['ValidationReport']

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

export type PreviewConfiguration = Schemas['PreviewConfiguration']

export type PreviewResponse = Schemas['PreviewResponse']

export type VariationTypeParam = Schemas['VariationTypeParam']

export type VariationTypeInfo = Schemas['VariationTypeInfo']

export type VariationTypesResponse = Schemas['VariationTypesResponse']

// -- results data query (eval viewer) ---------------------------------------

export interface DataTable {
  schema: string
  table: string
  columns: string[]
  rows: number | null
}

export type DataDescribe = Schemas['DataDescribe']

export type DataQueryResult = Schemas['DataQueryResult']

// Whether querying a campaign has to transfer its databases from the object store first.
// `fetch_required: false` (a local service) means the question does not apply — the backend
// difference is resolved server-side, so a view reads the same fields either way.
export type CampaignDataStatus = Schemas['CampaignDataStatus']

export interface PlotSpec {
  title: string
  query: string
  vega_lite: Record<string, unknown>
}

export type CampaignPlotsResponse = Schemas['CampaignPlotsResponse']

// The run-view panels declared for a campaign (its .vast top-level visualization.panels). Each entry
// is the raw panel dict (type + position + panel-specific data bindings); the UI normalizes it.
export type CampaignPanelsResponse = Schemas['CampaignPanelsResponse']

// The campaign's run-view `visualization:` block as editable YAML text. Saving overwrites that
// block in the campaign's own `_config/<name>.vast` in place.
export type PanelsSource = Schemas['PanelsSource']

// The campaign's `results_processing.postprocessing` block as editable YAML text (the same
// shape as PanelsSource — see the postprocessing rerun dialog). Saving overwrites that block in
// the campaign's own `_config/<name>.vast` in place; the raw rosbags are preserved, so
// postprocessing can be edited and re-run any number of times.
export type PostprocessingSource = Schemas['PostprocessingSource']

// One evaluation.visualization notebook workload + the node levels it defines a notebook for
// (subset of run/config/campaign). The Explorer shows a tab per workload and renders the
// workload's notebook, executed against the selected node, as HTML.
export type CampaignVisualization = Schemas['CampaignVisualization']

export type CampaignVisualizationsResponse = Schemas['CampaignVisualizationsResponse']

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

// -- the file address space -------------------------------------------------
// One address per file, and it is also the URL that serves it. Segments are encoded
// individually so the '/' separators survive: a resource that fetches siblings by
// *relative* URL (scene.json -> scene.bin) must stay inside its own directory.

const encodePath = (path: string) =>
  path.split('/').map(encodeURIComponent).join('/')

/** `/results/<campaign>/<path>` — a campaign's outputs, read-only. */
export const resultsUrl = (campaignId: string, path: string) =>
  `/results/${encodeURIComponent(campaignId)}/${encodePath(path)}`

/** `/sources/<workspace>/<path>` — a workspace's authored inputs, writable. */
export const sourcesUrl = (workspaceId: string, path: string) =>
  `/sources/${encodeURIComponent(workspaceId)}/${encodePath(path)}`

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

  createCampaign: (req: Partial<CreateCampaignRequest> & { workspace_id: string }) =>
    request<CampaignRef>('POST', '/campaigns', {
      config_path: '',
      config_filter: '',
      campaign_name: '',
      description: '',
      runs: 1,
      postprocess: true,
      upload_to_share: false,
      // Never from the web UI: it cannot know whether the browser is on the serve host,
      // and a window opening on someone else's screen is worse than no control at all.
      show_gui: false,
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

  // SSE stream URLs for live logs. `new EventSource(url)` streams deltas, auto-reconnects,
  // and resumes from the last byte offset via Last-Event-ID — see LogPanel. The pull methods
  // above stay for MCP/CLI parity; the browser prefers these.
  campaignLogStreamUrl: (campaignId: string) =>
    `${BASE}/campaigns/${encodeURIComponent(campaignId)}/logs/stream`,

  // SSE stream of the campaign list itself: the server pushes the full list on
  // connect and on every change (a server-side loop over listCampaigns). The
  // Monitor page consumes this instead of polling; EventSource reconnects natively.
  campaignsStreamUrl: () => `${BASE}/campaigns/events`,

  jobLogStreamUrl: (campaignId: string, jobName: string) =>
    `${BASE}/campaigns/${encodeURIComponent(campaignId)}/job-log/stream?job_name=${encodeURIComponent(
      jobName,
    )}`,

  stop: (campaignId: string) =>
    request<ActionResult>('POST', `/campaigns/${encodeURIComponent(campaignId)}/stop`),

  // Kill ONE running job; the campaign keeps going and that run is recorded as `killed`.
  // Refused (409) unless the job is running. `job_name` is a query param because locally it
  // is a "<config>/<run>" id and contains a slash.
  stopJob: (campaignId: string, jobName: string, reason?: string) =>
    request<ActionResult>(
      'POST',
      `/campaigns/${encodeURIComponent(campaignId)}/job-stop?job_name=${encodeURIComponent(
        jobName,
      )}&source=webui${reason ? `&reason=${encodeURIComponent(reason)}` : ''}`,
    ),

  // Launch a NEW campaign from this one's frozen config and pinned image — the source is
  // untouched, and the returned id is the new campaign's, not this one's. Refuses (400) when
  // the campaign never recorded an image its runs could start from.
  retriggerCampaign: (campaignId: string) =>
    request<CampaignRef>('POST', `/campaigns/${encodeURIComponent(campaignId)}/retrigger`),

  // Permanently delete one campaign wholesale (local dir / cluster object-store data +
  // leftover Jobs + cache). Refused by the service while the campaign is still running.
  deleteCampaign: (campaignId: string) =>
    request<ActionResult>('DELETE', `/campaigns/${encodeURIComponent(campaignId)}`),

  // -- workspaces & files ---------------------------------------------------

  createWorkspace: (name = '') => request<WorkspaceInfo>('POST', '/workspaces', { name }),

  deleteWorkspace: (id: string) =>
    request<ActionResult>('DELETE', `/workspaces/${encodeURIComponent(id)}`),

  // Files live in one address space that is also the URL space (see
  // robovast/common/file_address.py): /sources/<workspace>/<path> for the inputs a user
  // authors, /results/<campaign>/<path> for a campaign's outputs (read-only). The path
  // segments are encoded individually so the '/' separators survive.
  // Recursive and undetailed: every consumer builds a path tree, and none of them shows
  // a size — so ask for the names only.
  listProjectFiles: (id: string) =>
    request<FileListing>('GET', `${sourcesUrl(id, '')}?recursive=1&limit=0`),

  readProjectFile: (id: string, path: string) =>
    request<FileText>('GET', `${sourcesUrl(id, path)}?as=text&lines=0`),

  writeProjectFile: (id: string, path: string, content: string) =>
    request<FileMeta>('PUT', sourcesUrl(id, path), { content }),

  deleteProjectFile: (id: string, path: string) =>
    request<ActionResult>('DELETE', sourcesUrl(id, path)),

  // Upload any (non-.vast/.osc) file via the TTL PUT side channel.
  uploadFile: async (id: string, path: string, data: Blob) => {
    const grant = await request<UploadGrant>('POST', '/uploads', {
      address: sourcesUrl(id, path),
    })
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

  // On-demand 3D geometry. `sceneStatus` is pure -- it never starts a build, because a GET that
  // launched a 2 GB image pull would fire on a prefetch or a strict-mode double render. `runScene` is
  // the explicit trigger, and returns as soon as the build is dispatched.
  sceneStatus: (campaignId: string, configName: string, runId: number | string) =>
    request<SceneStatus>(
      'GET',
      `/campaigns/${encodeURIComponent(campaignId)}/scene` +
        `?config_name=${encodeURIComponent(configName)}&run_id=${encodeURIComponent(String(runId))}`,
    ),

  runScene: (campaignId: string, configName: string, runId: number | string) =>
    request<ActionResult>(
      'POST',
      `/campaigns/${encodeURIComponent(campaignId)}/scene/run` +
        `?config_name=${encodeURIComponent(configName)}&run_id=${encodeURIComponent(String(runId))}`,
    ),

  // A scene asset URL comes from SceneStatus.url and is used verbatim: it carries the cache key, so one
  // prefix addresses the whole entry and the descriptor loader's sibling fetches resolve.
  sceneAssetUrl: (url: string) => `${BASE}${url}`,

  describeCampaignData: (campaignId: string) =>
    request<DataDescribe>('GET', `/campaigns/${encodeURIComponent(campaignId)}/describe`),

  queryCampaignDataSql: (campaignId: string, sql: string, maxRows = 500) =>
    request<DataQueryResult>('POST', `/campaigns/${encodeURIComponent(campaignId)}/query`, {
      sql,
      max_rows: maxRows,
    }),

  // Cheap pre-flight for the two above: on a cluster campaign the first of them fetches the
  // databases from the object store inside the request, which without a word looks like a hang.
  campaignDataStatus: (campaignId: string) =>
    request<CampaignDataStatus>(
      'GET',
      `/campaigns/${encodeURIComponent(campaignId)}/data-status`,
    ),

  listCampaignPlots: (campaignId: string) =>
    request<CampaignPlotsResponse>('GET', `/campaigns/${encodeURIComponent(campaignId)}/plots`),

  listCampaignPanels: (campaignId: string) =>
    request<CampaignPanelsResponse>('GET', `/campaigns/${encodeURIComponent(campaignId)}/panels`),

  // The run-view `visualization:` block as editable YAML text (the 'edit visualization' dropdown).
  getPanelsSource: (campaignId: string) =>
    request<PanelsSource>(
      'GET',
      `/campaigns/${encodeURIComponent(campaignId)}/panels/source`,
    ),

  updatePanelsSource: (campaignId: string, content: string) =>
    request<PanelsSource>(
      'POST',
      `/campaigns/${encodeURIComponent(campaignId)}/panels/source`,
      { campaign_id: campaignId, content },
    ),

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
      // Which search round a `batch`-level notebook is for. A batch has no directory of its
      // own, so this is what identifies it — the service injects it as `BATCH`.
      batch?: number
      theme?: 'light' | 'dark'
    },
  ): Promise<string> => {
    const params = new URLSearchParams({ workload: opts.workload, level: opts.level })
    if (opts.configName) params.set('config_name', opts.configName)
    if (opts.runId !== undefined) params.set('run_id', String(opts.runId))
    if (opts.batch !== undefined) params.set('batch', String(opts.batch))
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

  // GET a run-scoped JSON endpoint under a campaign, with `config_name`+`run_id` and the given
  // params applied. Generic seam for panels (incl. external ones) that need a specialized endpoint
  // the generic DataProvider doesn't model -- e.g. robovast_nav's costmap panel hits `costmap`.
  runEndpoint: <T>(
    campaignId: string,
    configName: string,
    runId: number | string,
    endpoint: string,
    params: Record<string, string | number> = {},
  ) => {
    const qs = new URLSearchParams({ config_name: configName, run_id: String(runId) })
    for (const [k, v] of Object.entries(params)) qs.set(k, String(v))
    return request<T>(
      'GET',
      `/campaigns/${encodeURIComponent(campaignId)}/${endpoint}?${qs.toString()}`,
    )
  },

  // The campaign's `results_processing.postprocessing` block as editable YAML text (the rerun
  // dialog). `source` names the effective .vast; saving writes a new override revision.
  getPostprocessingSource: (campaignId: string) =>
    request<PostprocessingSource>(
      'GET',
      `/campaigns/${encodeURIComponent(campaignId)}/postprocessing/source`,
    ),

  updatePostprocessingSource: (campaignId: string, content: string) =>
    request<PostprocessingSource>(
      'POST',
      `/campaigns/${encodeURIComponent(campaignId)}/postprocessing/source`,
      { campaign_id: campaignId, content },
    ),

  runPostprocessing: (campaignId: string, force = false) =>
    request<ActionResult>(
      'POST',
      `/campaigns/${encodeURIComponent(campaignId)}/postprocessing/run`,
      { campaign_id: campaignId, force, skip: [] },
    ),

  // (Re)trigger upload-to-share for a finished campaign. Works from disk after a
  // restart; the target provider comes from the service environment.
  runShare: (campaignId: string) =>
    request<ActionResult>(
      'POST',
      `/campaigns/${encodeURIComponent(campaignId)}/share/run`,
      { campaign_id: campaignId },
    ),

  // URL of one run artifact file (fetched directly, e.g. by the scene3d loader). The
  // address is the run's real directory — <config>/<run>/<path> under the campaign —
  // so a relative sibling fetch (scene.json -> scene.bin) resolves within it.
  runFileUrl: (campaignId: string, configName: string, runId: number | string, path: string) =>
    `${BASE}${resultsUrl(campaignId, `${configName}/${runId}/${path}`)}`,

  // URL of a file the whole *campaign* shares rather than one run — a scene descriptor for a world
  // every run compiled identically, a frozen input under `_config/`. The path is campaign-relative,
  // so it addresses across runs where `runFileUrl` cannot; sibling fetches resolve the same way.
  campaignFileUrl: (campaignId: string, path: string) => `${BASE}${resultsUrl(campaignId, path)}`,
}
