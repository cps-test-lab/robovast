// The service the rig's browser talks to: every API request the UI makes, answered from the
// invented data below. Shapes follow the service's OpenAPI schema (openapi.json), and a response
// starts from that schema's defaults so a field the UI reads is never simply missing.
//
// Names are invented and neutral, and the only hosts named are `localhost` and RFC 2606's
// `example.com`.

import { readFileSync } from 'node:fs'
import { cameraFramePng, ROBOT, ROBOT_CAMERA, sceneGlb, SCENE_BODIES } from './images.mjs'
import { runSql, SqlError, table } from './sql.mjs'

const SCHEMAS = JSON.parse(readFileSync(new URL('../openapi.json', import.meta.url), 'utf8')).components.schemas

/** A value of `schemaName` with every required field at its type's empty value, then `over`. */
export function make(schemaName, over = {}) {
  const schema = SCHEMAS[schemaName]
  if (!schema) throw new Error(`openapi.json has no schema ${schemaName}`)
  const out = {}
  for (const key of schema.required ?? []) {
    if (key in over) continue
    out[key] = emptyOf(schema.properties[key])
  }
  return { ...out, ...over }
}

function emptyOf(prop) {
  if (!prop) return null
  if ('default' in prop) return prop.default
  if (prop.anyOf) return prop.anyOf.some((p) => p.type === 'null') ? null : emptyOf(prop.anyOf[0])
  if (prop.$ref) return make(prop.$ref.split('/').pop())
  switch (prop.type) {
    case 'string': return ''
    case 'integer':
    case 'number': return 0
    case 'boolean': return false
    case 'array': return []
    case 'object': return {}
    default: return null
  }
}

// -- the campaigns ---------------------------------------------------------------------------

export const RUNNING = 'demo-nav-sweep-7f3a21'
export const PASSED = 'demo-pick-place-2c9e41'
export const FAILED = 'demo-obstacle-check-b51d07'
/** The finished run every Results page shows. */
export const RUN = { campaign: PASSED, config: 'demo-arena', run: 0 }
export const WORKSPACE = 'ws-demo-0001'
export const VAST = 'demo_nav.vast'
export const NOTEBOOK = 'summary'
const CAMERA_TOPIC = '/demo_robot/front_camera/image'
const SCENE_URL = `/campaigns/${PASSED}/scene_assets/3e9b0c1d/scene.glb`

const NOW = Date.now() / 1000
const iso = (t) => new Date(t * 1000).toISOString()

const origin = make('CampaignOrigin', {
  kind: 'workspace', workspace_id: WORKSPACE, workspace_name: 'demo-navigation', config_path: VAST,
})

const CAMPAIGNS = [
  make('CampaignSummary', {
    campaign_id: RUNNING, created_by: 'alex', description: 'Arena sweep, two layouts, four runs each',
    phase: 'running', mode: 'batch', num_runs: 3, num_passed: 3, started_at: iso(NOW - 25 * 60),
    origin, results_bytes: 48_000_000,
  }),
  make('CampaignSummary', {
    campaign_id: PASSED, created_by: 'sam', description: 'Pick and place in the demo arena',
    phase: 'finished', mode: 'batch', num_runs: 2, num_passed: 2, postprocessed: true,
    started_at: iso(NOW - 3 * 3600), finished_at: iso(NOW - 3 * 3600 + 14 * 60), origin,
    results_bytes: 210_000_000,
  }),
  make('CampaignSummary', {
    campaign_id: FAILED, created_by: 'robin', description: 'Obstacle check with a narrow gap',
    phase: 'finished', mode: 'batch', num_runs: 3, num_passed: 2, num_failed: 1, postprocessed: true,
    started_at: iso(NOW - 26 * 3600), finished_at: iso(NOW - 26 * 3600 + 22 * 60), origin,
    results_bytes: 95_000_000,
  }),
]
const byId = Object.fromEntries(CAMPAIGNS.map((c) => [c.campaign_id, c]))

function statusOf(c) {
  const running = c.phase === 'running'
  return make('StatusResponse', {
    campaign_id: c.campaign_id, phase: c.phase, mode: 'batch', batch: 1, batches_done: running ? 0 : 1,
    phase_since: NOW - (running ? 24 * 60 : 3 * 3600), progress_since: NOW - 40, updated_at: NOW - 2,
    postprocessed: c.postprocessed, results_bytes: c.results_bytes,
    runs: make('RunProgress', {
      total: running ? 8 : c.num_runs, completed: c.num_passed, failed: c.num_failed,
      outcomes_counted: true,
    }),
    stalled: false,
  })
}

const job = (name, status, startedAgo, run) => make('JobSummary', {
  job_name: name, display_name: name, kind: 'run', status,
  config_name: name.replace(/-\d+$/, ''), run_id: run,
  node: status === 'pending' ? null : 'worker-1.example.com',
  started_at: startedAgo == null ? null : NOW - startedAgo,
  usage: status === 'running'
    ? make('JobUsage', { cpu_cores: 1.6, cpu_request: 2, memory_bytes: 1_900_000_000, memory_request_bytes: 4_000_000_000 })
    : null,
})

const JOBS = {
  [RUNNING]: [
    job('demo-arena-small-3', 'running', 210, 3),
    job('demo-arena-large-0', 'running', 95, 0),
    job('demo-arena-large-1', 'pending', null, 1),
    job('demo-arena-large-2', 'pending', null, 2),
    job('demo-arena-large-3', 'pending', null, 3),
    job('demo-arena-small-0', 'completed', 1300, 0),
    job('demo-arena-small-1', 'completed', 1000, 1),
    job('demo-arena-small-2', 'completed', 600, 2),
  ],
}

function jobsOf(id) {
  const jobs = JOBS[id] ?? []
  const n = (s) => jobs.filter((j) => j.status === s).length
  return make('ListJobsResponse', {
    jobs,
    counts: make('JobCounts', {
      running: n('running'), pending: n('pending'), completed: n('completed'), failed: n('failed'),
      total: jobs.length,
    }),
  })
}

// -- logs --------------------------------------------------------------------------------------

const CAMPAIGN_LOG = [
  ['run', 'INFO', 'robovast.execution.controller', 'Campaign started: 2 configurations x 4 runs'],
  ['run', 'INFO', 'robovast.execution.controller', 'Image resolved: localhost:5000/demo/sim@sha256:4f1c…'],
  ['run', 'INFO', 'robovast.execution.scheduler', 'Submitted demo-arena-small-0'],
  ['run', 'INFO', 'robovast.execution.scheduler', 'Submitted demo-arena-small-1'],
  ['run', 'INFO', 'robovast.execution.scheduler', 'demo-arena-small-0 finished: passed (412 s)'],
  ['run', 'WARNING', 'robovast.execution.scheduler', 'demo-arena-large-1 waiting for capacity'],
  ['run', 'INFO', 'robovast.execution.scheduler', 'demo-arena-small-1 finished: passed (388 s)'],
  ['run', 'INFO', 'robovast.execution.scheduler', 'demo-arena-small-2 finished: passed (401 s)'],
  ['run', 'INFO', 'robovast.execution.scheduler', 'Submitted demo-arena-small-3'],
  ['run', 'INFO', 'robovast.execution.scheduler', 'Submitted demo-arena-large-0'],
].map(([phase, level, logger, message], seq) => make('CampaignLogRow', {
  phase, level, logger, message, seq, wall_ts: NOW - 1500 + seq * 140,
}))

const JOB_LOG = [
  ['simulator', 'INFO', 'Loading world demo_arena_small'],
  ['simulator', 'INFO', 'Simulation running at 1.0x real time'],
  ['scenario', 'INFO', 'Scenario demo_nav started'],
  ['nav', 'INFO', 'Goal accepted: (4.0, 1.5)'],
  ['nav', 'WARNING', 'Replanning: path blocked near (2.4, 1.1)'],
  ['nav', 'INFO', 'Goal reached'],
  ['scenario', 'INFO', 'Waypoint 2 of 3'],
].map(([container, level, message], i) => make('JobLogRow', {
  container, level, severity: level.toLowerCase(), message, node: container, time_source: 'wall',
  wall_ts: NOW - 200 + i * 25,
}))

const SERVICE_LOG = [
  'INFO  robovast.service: serving on http://localhost:8800',
  'INFO  robovast.service: 3 campaigns indexed',
  `INFO  robovast.execution: ${RUNNING} running (3/8 done)`,
  'INFO  robovast.service: scene cache hit 3e9b0c1d',
].join('\n') + '\n'

// -- the finished run's tables -----------------------------------------------------------------

const RUN_SECONDS = 30
const VERDICT_AT = 28

/** The robot's path: forward along +x, weaving between the boxes. */
function robotPose(t) {
  const x = 0.4 * t
  const y = 1.0 * Math.sin(0.3 * t)
  const yaw = Math.atan2(0.3 * Math.cos(0.3 * t), 0.4)
  return { x, y, yaw }
}

function runTables() {
  const scopes = [
    { campaign: PASSED, config: 'demo-arena', run: 0 },
    { campaign: PASSED, config: 'demo-arena', run: 1 },
  ]
  const poses = []
  const velocity = []
  const log = []
  const verdicts = []
  const recording = []
  const clock = []
  for (const s of scopes) {
    const key = { config_name: s.config, run_id: s.run }
    for (let i = 0; i <= RUN_SECONDS * 10; i++) {
      const t = i / 10
      const p = robotPose(t + s.run)
      poses.push({
        ...key, timestamp: t, frame: ROBOT,
        'position.x': p.x, 'position.y': p.y, 'position.z': 0.15,
        'orientation.x': 0, 'orientation.y': 0, 'orientation.z': Math.sin(p.yaw / 2), 'orientation.w': Math.cos(p.yaw / 2),
      })
      if (i % 2 === 0) {
        velocity.push({
          ...key, timestamp: t,
          linear: 0.4 + 0.08 * Math.sin(t * 0.9) + (t > VERDICT_AT ? -0.35 : 0),
          angular: 0.3 * Math.cos(0.3 * t) * 0.9,
        })
      }
    }
    const lines = [
      [0.0, 'simulator', 'INFO', 'World demo_arena loaded: 4 bodies, 1 camera'],
      [0.2, 'scenario', 'INFO', 'Scenario demo_pick_place started'],
      [1.5, 'nav', 'INFO', 'Goal accepted: (11.0, 0.0)'],
      [4.8, 'nav', 'INFO', 'Passing demo_crate_red on the right'],
      [9.6, 'nav', 'WARNING', 'Local planner: oscillation detected, recovering'],
      [11.0, 'nav', 'INFO', 'Recovery succeeded'],
      [14.2, 'nav', 'INFO', 'Passing demo_crate_blue on the left'],
      [19.9, 'perception', 'INFO', 'demo_crate_green detected at 2.1 m'],
      [24.3, 'nav', 'INFO', 'Approaching demo_crate_yellow'],
      [27.6, 'scenario', 'INFO', 'Goal reached within tolerance'],
      [28.0, 'scenario', 'INFO', 'Scenario verdict: passed'],
      [29.5, 'simulator', 'INFO', 'Shutting down'],
    ]
    lines.forEach(([t, container, level, message], seq) => log.push({
      ...key, seq, sim_time: t, wall_ts: NOW - 3 * 3600 + t, time_source: 'sim', in_window: true,
      container, node: container, source: container, level, severity: level.toLowerCase(), message,
    }))
    verdicts.push({ ...key, timestamp: VERDICT_AT, wall_ts: NOW - 3 * 3600 + VERDICT_AT, status: 'passed' })
    recording.push({ ...key, world: 'demo_arena', seed: 7 + s.run, format_version: 1, overrides: '{}' })
    clock.push({ ...key, clock_map_source: 'sim_recording', clock_map_samples: 300, clock_map_wall_span_s: 30.1 })
  }
  const runView = [
    { config_name: 'demo-arena', run_id: 0, status: 'passed', passed: true, objective: null, batch: 0, live: false, duration: 28.0 },
    { config_name: 'demo-arena', run_id: 1, status: 'passed', passed: true, objective: null, batch: 0, live: false, duration: 29.1 },
  ]
  return {
    run_view: table(
      [['config_name', 'VARCHAR'], ['run_id', 'BIGINT'], ['status', 'VARCHAR'], ['passed', 'BOOLEAN'],
        ['objective', 'DOUBLE'], ['batch', 'BIGINT'], ['live', 'BOOLEAN'], ['duration', 'DOUBLE']],
      runView),
    // The Data browser's opening query reads it.
    runs: table(
      [['config_name', 'VARCHAR'], ['run_id', 'BIGINT'], ['status', 'VARCHAR'], ['duration', 'DOUBLE'],
        ['seed', 'BIGINT'], ['node', 'VARCHAR']],
      runView.map((r) => ({
        config_name: r.config_name, run_id: r.run_id, status: r.status, duration: r.duration,
        seed: 7 + r.run_id, node: 'worker-1.example.com',
      }))),
    sim_poses: table(
      [['config_name', 'VARCHAR'], ['run_id', 'BIGINT'], ['timestamp', 'DOUBLE'], ['frame', 'VARCHAR'],
        ['position.x', 'DOUBLE'], ['position.y', 'DOUBLE'], ['position.z', 'DOUBLE'],
        ['orientation.x', 'DOUBLE'], ['orientation.y', 'DOUBLE'], ['orientation.z', 'DOUBLE'], ['orientation.w', 'DOUBLE']],
      poses),
    base_velocity: table(
      [['config_name', 'VARCHAR'], ['run_id', 'BIGINT'], ['timestamp', 'DOUBLE'], ['linear', 'DOUBLE'], ['angular', 'DOUBLE']],
      velocity),
    run_log: table(
      [['config_name', 'VARCHAR'], ['run_id', 'BIGINT'], ['seq', 'BIGINT'], ['sim_time', 'DOUBLE'], ['wall_ts', 'DOUBLE'],
        ['time_source', 'VARCHAR'], ['in_window', 'BOOLEAN'], ['container', 'VARCHAR'], ['node', 'VARCHAR'],
        ['source', 'VARCHAR'], ['level', 'VARCHAR'], ['severity', 'VARCHAR'], ['message', 'VARCHAR']],
      log),
    scenario_timestamps: table(
      [['config_name', 'VARCHAR'], ['run_id', 'BIGINT'], ['timestamp', 'DOUBLE'], ['wall_ts', 'DOUBLE'], ['status', 'VARCHAR']],
      verdicts),
    sim_recording: table(
      [['config_name', 'VARCHAR'], ['run_id', 'BIGINT'], ['world', 'VARCHAR'], ['seed', 'BIGINT'],
        ['format_version', 'BIGINT'], ['overrides', 'VARCHAR']],
      recording),
    run_clock: table(
      [['config_name', 'VARCHAR'], ['run_id', 'BIGINT'], ['clock_map_source', 'VARCHAR'],
        ['clock_map_samples', 'BIGINT'], ['clock_map_wall_span_s', 'DOUBLE']],
      clock),
  }
}

/** The `run_view` of the other two campaigns, so the Results tree lists their runs too. */
function runViewOnly(runs) {
  return {
    run_view: table(
      [['config_name', 'VARCHAR'], ['run_id', 'BIGINT'], ['status', 'VARCHAR'], ['passed', 'BOOLEAN'],
        ['objective', 'DOUBLE'], ['batch', 'BIGINT'], ['live', 'BOOLEAN'], ['duration', 'DOUBLE']],
      runs.map(([config_name, run_id, status, live]) => ({
        config_name, run_id, status, passed: status === 'passed' ? true : status === 'failed' ? false : null,
        objective: null, batch: 0, live: !!live, duration: status === 'running' ? null : 30 + run_id,
      }))),
  }
}

const TABLES = {
  [PASSED]: runTables(),
  [RUNNING]: runViewOnly([
    ['demo-arena-small', 0, 'passed'], ['demo-arena-small', 1, 'passed'], ['demo-arena-small', 2, 'passed'],
    ['demo-arena-small', 3, 'running', true], ['demo-arena-large', 0, 'running', true],
  ]),
  [FAILED]: runViewOnly([
    ['demo-gap-wide', 0, 'passed'], ['demo-gap-narrow', 0, 'failed'], ['demo-gap-narrow', 1, 'passed'],
  ]),
}

function describe(id) {
  const tables = Object.entries(TABLES[id] ?? {}).map(([name, t]) => make('DataTable', {
    schema: 'main', table: name, kind: name === 'run_view' ? 'view' : 'table',
    columns: t.columns.map(([c, ty]) => `${c} ${ty}`), rows: t.rows.length, built: 2, runs: 2,
    description: '', column_notes: {}, failed: {},
  }))
  return make('DataDescribe', { campaign_id: id, tables, note: '' })
}

// -- the run view's panels, as the campaign's .vast declares them ----------------------------

const PANELS = [
  { type: 'scene3d', position: { fill: true } },
  {
    type: 'camera', title: 'Front camera', kind: 'topic', topic: CAMERA_TOPIC,
    position: { anchor: 'top-right', width: 340, height: 280 }, minimizable: true,
  },
  {
    type: 'timeseries', title: 'Base velocity',
    source: { table: 'base_velocity', time_column: 'timestamp' },
    series: [{ column: 'linear', label: 'linear (m/s)' }, { column: 'angular', label: 'angular (rad/s)' }],
    position: { anchor: 'bottom-right', width: 440, height: 240 }, minimizable: true,
  },
  {
    type: 'log', title: 'Run log',
    position: { anchor: 'bottom-left', width: 'calc(100% - 460px)', height: 240 }, minimizable: true,
  },
  { type: 'playback' },
]

const PANELS_YAML = `run_view:
  timeline: {table: sim_poses, time_column: timestamp}
  panels:
  - scene3d: {position: {fill: true}}
  - camera:
      title: Front camera
      kind: topic
      topic: ${CAMERA_TOPIC}
      position: {anchor: top-right, width: 340, height: 280}
      minimizable: true
  - timeseries:
      title: Base velocity
      source: {table: base_velocity, time_column: timestamp}
      series:
      - {column: linear, label: linear (m/s)}
      - {column: angular, label: angular (rad/s)}
      position: {anchor: bottom-right, width: 440, height: 240}
      minimizable: true
  - log:
      title: Run log
      position: {anchor: bottom-left, width: "calc(100% - 460px)", height: 240}
      minimizable: true
`

const VAST_TEXT = `version: 5
metadata:
  name: demo_nav
  description: Navigate the demo arena past four crates.
execution:
  simulator: roqsim
  image: localhost:5000/demo/sim:latest
  runs: 4
configuration:
- name: demo-arena-small
  parameters:
    world: demo_arena
    goal: {x: 11.0, y: 0.0}
- name: demo-arena-large
  parameters:
    world: demo_arena_large
    goal: {x: 18.0, y: 0.0}
visualization:
  results:
${PANELS_YAML.replace(/^/gm, '    ')}`

const NOTEBOOK_HTML = `<!doctype html><html><head><meta charset="utf-8"><style>
body{font:14px/1.5 system-ui,sans-serif;margin:16px;color:#1d1d1f}
h1{font-size:20px}table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:4px 8px}
</style></head><body>
<h1>Run summary: ${RUN.config} / ${RUN.run}</h1>
<p>The robot reached the goal at 28.0 s after one recovery near <code>demo_crate_red</code>.</p>
<table><tr><th>metric</th><th>value</th></tr>
<tr><td>path length</td><td>12.6 m</td></tr><tr><td>mean speed</td><td>0.43 m/s</td></tr>
<tr><td>min clearance</td><td>0.38 m</td></tr><tr><td>verdict</td><td>passed</td></tr></table>
<h2>Speed over time</h2>
<svg width="480" height="160" viewBox="0 0 480 160" style="border:1px solid #ddd">
<polyline fill="none" stroke="#3b82f6" stroke-width="2" points="${Array.from({ length: 61 }, (_, i) =>
  `${i * 8},${Math.round(90 - 40 * Math.sin(i * 0.25))}`).join(' ')}"/></svg>
</body></html>`

// -- the workspace -----------------------------------------------------------------------------

const WORKSPACE_FILES = {
  [VAST]: VAST_TEXT,
  'scenario.osc': 'import osc.standard\n\nscenario demo_nav:\n    do serial:\n        wait elapsed(1s)\n',
  'files/arena.yaml': 'image: arena.png\nresolution: 0.05\norigin: [-2.0, -6.0, 0.0]\n',
}

// -- the admin page ----------------------------------------------------------------------------

const SERVICE_EVENTS = [
  ['campaign.started', 'info', 'alex', RUNNING, 'Campaign started'],
  ['campaign.finished', 'info', 'sam', PASSED, 'Campaign finished: 2 runs, 2 passed'],
  ['campaign.finished', 'warning', 'robin', FAILED, 'Campaign finished: 3 runs, 1 failed'],
  ['service.started', 'info', 'service', '', 'Service started'],
].map(([kind, severity, actor, subject, message], i) => make('ServiceEvent', {
  seq: i + 1, at: NOW - (4 - i) * 3600, kind, severity, actor, subject_id: subject,
  subject_type: subject ? 'campaign' : 'service', message, payload: {},
}))

function usageHistory() {
  const samples = []
  for (let i = 0; i < 60; i++) {
    samples.push(make('UsageSample', {
      at: NOW - (60 - i) * 60, cpu_capacity: 32, cpu_used: 8 + 6 * Math.sin(i / 8), cpu_measured: 6 + 5 * Math.sin(i / 8),
      memory_capacity_bytes: 128e9, memory_used_bytes: 40e9 + 10e9 * Math.sin(i / 10), memory_measured_bytes: 30e9,
    }))
  }
  return make('UsageHistory', { samples, sample_interval_s: 60, step_s: 60, service_started_at: NOW - 5 * 3600 })
}

// -- routing -----------------------------------------------------------------------------------

const json = (body, status = 200) => ({ status, contentType: 'application/json', body: JSON.stringify(body) })
const notFound = (detail) => json({ detail }, 404)

/** A text/event-stream body of `events` (`{event?, data, id?}`), `data` JSON-encoded. */
function sse(events) {
  return {
    status: 200,
    contentType: 'text/event-stream',
    body: events.map((e) =>
      `${e.id !== undefined ? `id: ${e.id}\n` : ''}${e.event ? `event: ${e.event}\n` : ''}data: ${JSON.stringify(e.data)}\n\n`).join(''),
  }
}

/** The fixture service: `handle(method, url, postBody)` → `{status, contentType, body}`, or null
 *  for a request nothing here answers. `sqlMisses` collects the tables a query named that the
 *  fixtures lack (answered with the service's error, and worth knowing about). */
export function fixtureService({ loginHtml }) {
  const sqlMisses = new Set()
  const glb = sceneGlb()
  const png = cameraFramePng()

  function handle(method, url, postBody) {
    const u = new URL(url)
    const path = decodeURIComponent(u.pathname)
    const q = u.searchParams
    let m

    if (path === '/login') return { status: 200, contentType: 'text/html', body: loginHtml.replace('{{next}}', q.get('next') ?? '/') }
    if (path === '/version') {
      return json(make('VersionInfo', {
        robovast_version: '0.0.0-demo', package_version: '0.0.0-demo', api_version: '1', backend: 'cluster',
        code_revision: 'demo', built_at: iso(NOW - 86400), web_base: 'http://localhost:8800',
        results_address: '/results', sources_address: '/sources', can_schedule: true, can_build_images: true,
      }))
    }
    if (path === '/usage') {
      return json(make('ResourceUsage', {
        backend: 'cluster', parallel_runs: true, cpu_capacity: 32, cpu_used: 12, cpu_measured: 9.5,
        memory_capacity_bytes: 128e9, memory_used_bytes: 48e9, memory_measured_bytes: 35e9,
        jobs_running: 2, jobs_pending: 3,
        results: make('DiskSpace', { capacity_bytes: 2e12, used_bytes: 0.7e12 }),
        disk: make('DiskSpace', { capacity_bytes: 500e9, used_bytes: 210e9 }),
      }))
    }
    if (path === '/usage/history') return json(usageHistory())
    if (path === '/campaigns' && method === 'GET') return json(make('ListCampaignsResponse', { campaigns: CAMPAIGNS, total: CAMPAIGNS.length }))
    if (path === '/campaigns/events') {
      return sse([{ data: make('ListCampaignsResponse', { campaigns: CAMPAIGNS, total: CAMPAIGNS.length }) }])
    }
    // The .vast schema the config editor validates against; an open one is enough to show it.
    if (path === '/config/schema') return json({ $schema: 'http://json-schema.org/draft-07/schema#', type: 'object' })
    if (path === '/share/archives') return json({ configured: false, share_type: '', archives: [], workspaces: [] })
    if (path === '/workspaces' && method === 'GET') {
      return json(make('ListWorkspacesResponse', {
        workspaces: [make('WorkspaceInfo', { workspace_id: WORKSPACE, name: 'demo-navigation', created_at: iso(NOW - 7 * 86400) })],
      }))
    }

    if ((m = path.match(/^\/campaigns\/([^/]+)\/(.*)$/))) {
      const [, id, rest] = m
      const c = byId[id]
      if (!c) return notFound(`no campaign ${id}`)
      if (rest === 'status') return json(statusOf(c))
      if (rest === 'jobs') return json(jobsOf(id))
      if (rest === 'logs/stream') {
        return id === RUNNING
          ? sse([{ id: CAMPAIGN_LOG.length, data: CAMPAIGN_LOG }])
          : sse([{ id: 0, data: CAMPAIGN_LOG.slice(0, 3) }, { event: 'eof', data: null }])
      }
      if (rest === 'logs') return json(make('CampaignLogChunk', { rows: CAMPAIGN_LOG, cursor: String(CAMPAIGN_LOG.length), eof: id !== RUNNING, phases: ['run'] }))
      if (rest === 'job-log/stream') return sse([{ id: JOB_LOG.length, data: JOB_LOG }])
      if (rest === 'job-log') return json(make('JobLogChunk', { rows: JOB_LOG, cursor: String(JOB_LOG.length), eof: false }))
      if (rest === 'search/history') return json(make('SearchHistory', { unavailable: 'batch_mode' }))
      if (rest === 'retrigger/check') return json(make('RetriggerReport', { campaign_id: id, runnable: true, axes: {}, blocking: [] }))
      if (rest === 'panels') {
        return json(make('CampaignPanelsResponse', {
          campaign_id: id, panels: PANELS, timeline: { table: 'sim_poses', time_column: 'timestamp' }, transport_only: false,
        }))
      }
      if (rest === 'panels/source') return json(make('PanelsSource', { campaign_id: id, content: PANELS_YAML }))
      if (rest === 'describe') return json(describe(id))
      if (rest === 'query' && method === 'POST') {
        const { sql, max_rows: maxRows = 500 } = JSON.parse(postBody || '{}')
        try {
          const res = runSql(sql, TABLES[id] ?? {})
          const truncated = res.rows.length > maxRows
          const rows = res.rows.slice(0, maxRows)
          return json(make('DataQueryResult', { campaign_id: id, columns: res.columns, rows, row_count: rows.length, truncated, note: null }))
        } catch (e) {
          if (!(e instanceof SqlError)) throw e
          const missing = e.detail.match(/Table with name (\S+) does not exist/)
          if (missing) sqlMisses.add(`${id}: ${missing[1]}`)
          return json({ detail: e.detail }, 400)
        }
      }
      if (rest === 'plots') {
        return json(make('CampaignPlotsResponse', {
          campaign_id: id,
          plots: [{
            title: 'Run duration',
            query: 'SELECT config_name, run_id, duration FROM run_view',
            vega_lite: {
              mark: 'bar',
              encoding: {
                x: { field: 'run_id', type: 'ordinal', title: 'run' },
                y: { field: 'duration', type: 'quantitative', title: 'duration (s)' },
                color: { field: 'config_name', type: 'nominal' },
              },
            },
          }],
        }))
      }
      if (rest === 'visualizations') {
        return json(make('CampaignVisualizationsResponse', {
          campaign_id: id, workloads: [make('CampaignVisualization', { name: NOTEBOOK, levels: ['run', 'config', 'campaign'] })],
        }))
      }
      if (rest === 'notebook') return { status: 200, contentType: 'text/html', body: NOTEBOOK_HTML }
      if (rest === 'scene') {
        return json(make('SceneStatus', {
          campaign_id: id, config_name: q.get('config_name') ?? '', run_id: q.get('run_id') ?? '',
          cached: true, url: SCENE_URL, world: 'demo_arena', bytes: glb.length, overrides_known: true,
        }))
      }
      if (path === SCENE_URL) return { status: 200, contentType: 'model/gltf-binary', body: glb }
    }

    if ((m = path.match(/^\/data\/campaigns\/([^/]+)\/(frame-index|frame)$/))) {
      if (q.get('topic') !== CAMERA_TOPIC) return notFound(`no frames of ${q.get('topic')}`)
      if (m[2] === 'frame-index') {
        return json({ topic: CAMERA_TOPIC, times: Array.from({ length: RUN_SECONDS + 1 }, (_, i) => i) })
      }
      const t = Math.max(0, Math.min(RUN_SECONDS, Math.floor(Number(q.get('t') ?? RUN_SECONDS))))
      return { status: 200, contentType: 'image/png', body: png, headers: { 'X-Frame-Time': String(t) } }
    }

    if ((m = path.match(/^\/sources\/([^/]+)\/?(.*)$/))) {
      const [, ws, file] = m
      if (ws !== WORKSPACE) return notFound(`no workspace ${ws}`)
      if (!file) {
        const entries = Object.keys(WORKSPACE_FILES).sort()
        return json({ address: `/sources/${ws}/`, entries, total: entries.length, truncated: false, recursive: true })
      }
      const text = WORKSPACE_FILES[file]
      if (text === undefined) return notFound(`no file ${file}`)
      const lines = text.split('\n').length
      return json({ address: path, total_lines: lines, returned_lines: lines, offset: 0, content: text })
    }
    if ((m = path.match(/^\/workspaces\/([^/]+)\/(validate|preview|scene)$/))) {
      if (m[2] === 'validate') return json(make('ValidationReport', { valid: true, configs: 2, runs_per_config: 4, total_trials: 8 }))
      if (m[2] === 'preview') return json(make('PreviewResponse', { state: 'ready', configs: 2, runs_per_config: 4, total_trials: 8 }))
      return json(make('SceneStatus', { cached: false, generation_required: true, note: 'Not built for this workspace yet.' }))
    }

    if (path === '/admin/events') return json(make('ServiceEvents', { events: SERVICE_EVENTS, next_seq: SERVICE_EVENTS.length + 1 }))
    if (path === '/admin/mcp-tools') {
      return json(make('McpToolStats', {
        max_age_s: 30 * 86400, max_rows: 200000,
        tools: [
          make('McpToolStat', { tool: 'list_campaigns', calls: 42, errors: 0, mean_ms: 35, max_ms: 210, last_at: NOW - 600 }),
          make('McpToolStat', { tool: 'start_campaign', calls: 7, errors: 1, mean_ms: 820, max_ms: 2400, last_at: NOW - 1500 }),
          make('McpToolStat', { tool: 'query_campaign_data_sql', calls: 19, errors: 2, mean_ms: 140, max_ms: 900, last_at: NOW - 3000 }),
        ],
      }))
    }
    if (path === '/admin/mcp-calls') return json(make('McpCalls', { calls: [], total: 0, limit: 200, offset: 0 }))
    if (path === '/admin/upgrade') {
      return json(make('UpgradeInfo', {
        supported: true, image_ref: 'localhost:5000/demo/robovast-service:latest',
        running_digest: 'sha256:4f1c0e…', registry_digest: 'sha256:4f1c0e…', upgrade_available: false,
      }))
    }
    if (path === '/admin/config') {
      return json(make('ServiceConfig', {
        how_to_change: 'Set the variable in the service environment and restart it.',
        settings: [
          make('ServiceSetting', { key: 'ROBOVAST_BACKEND', group: 'execution', value: 'kubernetes', default: 'local', is_set: true, description: 'Where runs execute.' }),
          make('ServiceSetting', { key: 'ROBOVAST_NAMESPACE', group: 'execution', value: 'default', default: 'default', is_set: false, description: 'The namespace runs are scheduled in.' }),
          make('ServiceSetting', { key: 'ROBOVAST_RESULTS_DIR', group: 'storage', value: '/data/results', default: '/data/results', is_set: false, description: 'Where campaign results live.' }),
        ],
      }))
    }
    if (path === '/admin/cache') return json(make('ServiceCache', {}))
    if (path === '/admin/log/stream') return sse([{ id: SERVICE_LOG.length, data: SERVICE_LOG }])

    return null
  }

  return { handle, sqlMisses }
}

export { SCENE_BODIES }
