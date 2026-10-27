// The web UI on a phone: screenshots, layout probes and scripted touch, against the built UI.
//
// Serves the existing `dist/` with `vite preview` (it builds nothing: run `npm run build` first),
// answers every API request from the fixtures in `fixtures.mjs`, and drives Chromium through each
// page with the `Pixel 7` and `iPhone 13` profiles (portrait and landscape, touch on) and a 1440×900
// desktop with a mouse. Writes, under `--out` (default `e2e/out/`):
//
//   <page>-<profile>-<orientation>.png   one screenshot per page and profile
//   report.json                          per page and profile: does the page fit its viewport
//                                        (`scrollWidth` against `clientWidth`, and what reaches past
//                                        the right edge), and which touch targets a finger cannot
//                                        hit in a 44 px square
//   touch.json, scene-touch.webm         --touch: gestures on the run view's 3D scene (Pixel 7)
//   mouse.json                           --mouse: the same scene by mouse (desktop)
//   pwa.json                             --pwa: manifest, installability, service worker, caches
//
//   npm run e2e -- [--out <dir>] [--dist <dir>] [--pages campaigns,run] [--touch] [--mouse] [--pwa]
//                  [--strict]
//
// `--strict` exits non-zero when a page is wider than a phone's viewport, or when `campaigns`,
// `run` or `login` has a target under 44 px on a phone. Not part of `npm test`: it needs a browser
// (Playwright's Chromium) and a built UI.

import { spawn } from 'node:child_process'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs'
import { createServer } from 'node:net'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium, devices } from 'playwright'
import { centroidOf, changedFraction, decodePng, SCENE_BODIES } from './images.mjs'
import { fixtureService, NOTEBOOK, RUN, RUNNING } from './fixtures.mjs'

const UI = resolve(dirname(fileURLToPath(import.meta.url)), '..')

// -- arguments ---------------------------------------------------------------------------------

function parseArgs(argv) {
  const opts = {
    out: join(UI, 'e2e', 'out'), dist: join(UI, 'dist'), pages: null,
    touch: false, mouse: false, pwa: false, strict: false,
  }
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i]
    const value = () => {
      const v = argv[++i]
      if (v === undefined) throw new Error(`${a} needs a value`)
      return v
    }
    if (a === '--out') opts.out = resolve(value())
    else if (a === '--dist') opts.dist = resolve(value())
    else if (a === '--pages') opts.pages = value().split(',').filter(Boolean)
    else if (a === '--touch') opts.touch = true
    else if (a === '--mouse') opts.mouse = true
    else if (a === '--pwa') opts.pwa = true
    else if (a === '--strict') opts.strict = true
    else throw new Error(`unknown argument ${a}`)
  }
  return opts
}

// -- the server --------------------------------------------------------------------------------

function freePort() {
  return new Promise((res, rej) => {
    const s = createServer()
    s.once('error', rej)
    s.listen(0, '127.0.0.1', () => {
      const { port } = s.address()
      s.close(() => res(port))
    })
  })
}

async function startPreview(dist) {
  if (!existsSync(join(dist, 'index.html'))) {
    throw new Error(`${dist} holds no built UI (no index.html). Run \`npm run build\` first.`)
  }
  const port = await freePort()
  const child = spawn(
    'npx', ['vite', 'preview', '--config', join(UI, 'e2e', 'preview.config.mjs'), '--host', '127.0.0.1', '--port', String(port), '--strictPort', '--outDir', dist],
    { cwd: UI, stdio: ['ignore', 'pipe', 'pipe'], detached: true },
  )
  let output = ''
  child.stdout.on('data', (d) => { output += d })
  child.stderr.on('data', (d) => { output += d })
  const base = `http://127.0.0.1:${port}`
  const deadline = Date.now() + 30_000
  for (;;) {
    if (child.exitCode !== null) throw new Error(`vite preview exited (${child.exitCode}):\n${output}`)
    try {
      const res = await fetch(`${base}/`)
      if (res.ok) break
    } catch {
      // not listening yet
    }
    if (Date.now() > deadline) {
      stopPreview(child)
      throw new Error(`vite preview did not answer on ${base} within 30 s:\n${output}`)
    }
    await sleep(200)
  }
  return { base, child }
}

function stopPreview(child) {
  // npx starts vite as a child of its own: the whole group goes.
  try {
    process.kill(-child.pid, 'SIGTERM')
  } catch {
    // already gone
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

// -- routing -----------------------------------------------------------------------------------

/** The service's login page, read from the service at run time so it cannot drift from it. */
function loginHtml() {
  const app = readFileSync(resolve(UI, '../../src/robovast/service/app.py'), 'utf8')
  const m = app.match(/^_LOGIN_HTML = """([\s\S]*?)"""/m)
  if (!m) throw new Error('src/robovast/service/app.py: _LOGIN_HTML not found')
  return m[1]
}

/** Route every request of `context`: what `dist/` holds goes to the preview server, the rest is
 *  answered from the fixtures, and what they do not answer is a 404 noted in `unanswered`. */
async function routeContext(context, base, dist, service, unanswered) {
  await context.route('**/*', async (route) => {
    const req = route.request()
    const url = new URL(req.url())
    if (url.origin !== base) return route.continue()
    const path = decodeURIComponent(url.pathname)
    const inDist = path === '/' || (() => {
      const file = join(dist, path)
      return file.startsWith(dist) && existsSync(file) && statSync(file).isFile()
    })()
    if (inDist) return route.continue()
    let answer
    try {
      answer = service.handle(req.method(), req.url(), req.postData())
    } catch (e) {
      answer = { status: 500, contentType: 'application/json', body: JSON.stringify({ detail: `fixture error: ${e.message}` }) }
      unanswered.add(`${req.method()} ${url.pathname}${url.search} (fixture error: ${e.message})`)
    }
    if (!answer) {
      unanswered.add(`${req.method()} ${url.pathname}${url.search}`)
      answer = { status: 404, contentType: 'application/json', body: JSON.stringify({ detail: 'not in the e2e fixtures' }) }
    }
    return route.fulfill(answer)
  })
}

// A finished response is a finished EventSource to the browser, which then reports the stream as
// reconnecting. This stand-in fetches the routed body, dispatches its events, and then stays open,
// sending the heartbeat the service sends, so a live log reads as live.
const EVENT_SOURCE_STANDIN = `(() => {
  class FixtureEventSource extends EventTarget {
    constructor(url, init) {
      super()
      this.url = new URL(url, location.href).href
      this.withCredentials = !!(init && init.withCredentials)
      this.readyState = 0
      this.onopen = null
      this.onmessage = null
      this.onerror = null
      this._timer = null
      this._start()
    }
    _emit(type, data, id) {
      const e = data === undefined ? new Event(type)
        : new MessageEvent(type, { data, lastEventId: id || '', origin: location.origin })
      this.dispatchEvent(e)
      const handler = this['on' + type]
      if ((type === 'open' || type === 'message' || type === 'error') && typeof handler === 'function') handler.call(this, e)
    }
    async _start() {
      let text
      try {
        const res = await fetch(this.url, { headers: { Accept: 'text/event-stream' } })
        if (this.readyState === 2) return
        if (!res.ok) { this.readyState = 2; this._emit('error'); return }
        text = await res.text()
      } catch {
        this.readyState = 2
        this._emit('error')
        return
      }
      if (this.readyState === 2) return
      this.readyState = 1
      this._emit('open')
      for (const block of text.split(/\\n\\n/)) {
        if (this.readyState === 2) return
        let event = 'message'
        let id = ''
        const data = []
        for (const line of block.split('\\n')) {
          const i = line.indexOf(':')
          const field = i < 0 ? line : line.slice(0, i)
          const value = i < 0 ? '' : line.slice(i + 1).replace(/^ /, '')
          if (field === 'event') event = value
          else if (field === 'data') data.push(value)
          else if (field === 'id') id = value
        }
        if (data.length) this._emit(event, data.join('\\n'), id)
      }
      this._timer = setInterval(() => { if (this.readyState === 1) this._emit('heartbeat', '') }, 1000)
    }
    close() {
      this.readyState = 2
      clearInterval(this._timer)
    }
  }
  FixtureEventSource.CONNECTING = 0
  FixtureEventSource.OPEN = 1
  FixtureEventSource.CLOSED = 2
  Object.assign(FixtureEventSource.prototype, { CONNECTING: 0, OPEN: 1, CLOSED: 2 })
  window.EventSource = FixtureEventSource
})()`

// Dots where the fingers are, for the recording: CDP touches leave no trace on screen otherwise.
const TOUCH_DOTS = `(() => {
  const dots = new Map()
  const draw = (e) => {
    const seen = new Set()
    for (const t of e.touches) {
      seen.add(t.identifier)
      let d = dots.get(t.identifier)
      if (!d) {
        d = document.createElement('div')
        d.style.cssText = 'position:fixed;width:36px;height:36px;margin:-18px 0 0 -18px;border-radius:50%;' +
          'background:rgba(255,64,129,.45);border:2px solid #fff;pointer-events:none;z-index:2147483647'
        document.documentElement.appendChild(d)
        dots.set(t.identifier, d)
      }
      d.style.left = t.clientX + 'px'
      d.style.top = t.clientY + 'px'
    }
    for (const [id, d] of dots) if (!seen.has(id)) { d.remove(); dots.delete(id) }
  }
  for (const type of ['touchstart', 'touchmove', 'touchend', 'touchcancel'])
    window.addEventListener(type, draw, { capture: true, passive: true })
})()`

// -- profiles and pages ------------------------------------------------------------------------

const PHONES = [
  { name: 'pixel7', portrait: devices['Pixel 7'], landscape: devices['Pixel 7 landscape'] },
  { name: 'iphone13', portrait: devices['iPhone 13'], landscape: devices['iPhone 13 landscape'] },
]
const DESKTOP = {
  viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1, isMobile: false, hasTouch: false,
}

/** Every profile a page is shot in. Chromium for all of them: the iPhone profile is its viewport,
 *  scale and user agent, not WebKit. */
function profiles() {
  const out = []
  for (const p of PHONES) {
    for (const orientation of ['portrait', 'landscape']) {
      const { defaultBrowserType, ...device } = p[orientation]
      void defaultBrowserType
      out.push({ profile: p.name, orientation, phone: true, device })
    }
  }
  out.push({ profile: 'desktop', orientation: 'landscape', phone: false, device: DESKTOP })
  return out
}

const runHash = `#/results/run/${RUN.campaign}/${RUN.config}/${RUN.run}`

const PAGES = [
  { name: 'campaigns', hash: `#/execution?campaign=${RUNNING}`, ready: `text=${RUNNING}` },
  {
    name: 'explorer',
    hash: `#/results/explorer/${RUN.campaign}/${RUN.config}/${RUN.run}?tab=${NOTEBOOK}`,
    ready: 'iframe',
  },
  { name: 'run', hash: runHash, ready: 'canvas' },
  { name: 'data', hash: `#/results/data/${RUN.campaign}`, ready: '[role=grid]' },
  { name: 'config', hash: '#/config', ready: '.monaco-editor' },
  { name: 'admin', hash: '#/admin', ready: 'text=Admin' },
  { name: 'login', path: '/login?next=/', ready: 'input[name=token]' },
]

/** Requests in flight on `page`, so a page counts as loaded once the network has gone quiet. */
function trackRequests(page) {
  let inflight = 0
  let last = Date.now()
  const up = () => { inflight++; last = Date.now() }
  const down = () => { inflight = Math.max(0, inflight - 1); last = Date.now() }
  page.on('request', up)
  page.on('requestfinished', down)
  page.on('requestfailed', down)
  return async (quietMs = 700, maxMs = 15_000) => {
    const end = Date.now() + maxMs
    while (Date.now() < end) {
      if (inflight === 0 && Date.now() - last >= quietMs) return true
      await sleep(100)
    }
    return false
  }
}

async function openPage(context, base, def) {
  const page = await context.newPage()
  const errors = []
  page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`))
  page.on('console', (m) => { if (m.type() === 'error') errors.push(`console: ${m.text()}`) })
  const quiet = trackRequests(page)
  await page.goto(`${base}${def.path ?? `/${def.hash}`}`)
  await quiet()
  let ready = true
  try {
    await page.locator(def.ready).first().waitFor({ state: 'visible', timeout: 15_000 })
  } catch {
    ready = false
  }
  await quiet()
  await page.waitForTimeout(1200)
  // A selection the page scrolls into view can leave it scrolled sideways; the screenshot is of the
  // page from its origin.
  await page.evaluate(() => window.scrollTo(0, 0))
  await page.waitForTimeout(200)
  return { page, errors, ready, quiet }
}

// -- probes ------------------------------------------------------------------------------------

const TARGETS = 'button, a[href], input, select, textarea, [role=button], [role=tab], [role=menuitem], ' +
  '[role=treeitem], [role=option], [role=switch], [role=checkbox]'

/** Runs in the page: does it fit, what reaches past the right edge, which targets are small. */
function probe(targetsSelector) {
  const de = document.documentElement
  const clientWidth = de.clientWidth
  const describe = (el) => {
    const label = el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('placeholder') ||
      (el.innerText || el.value || '').trim().replace(/\s+/g, ' ').slice(0, 40) || el.getAttribute('name') || ''
    const cls = typeof el.className === 'string' ? el.className.split(/\s+/).find((c) => c && !c.startsWith('css-')) : ''
    return `${el.tagName.toLowerCase()}${el.id ? `#${el.id}` : ''}${cls ? `.${cls}` : ''}${label ? ` "${label}"` : ''}`
  }
  const clipsX = (el) => {
    for (let a = el.parentElement; a && a !== document.body && a !== de; a = a.parentElement) {
      const o = getComputedStyle(a).overflowX
      if (o !== 'visible') return true
    }
    return false
  }
  const past = (el) => {
    const r = el.getBoundingClientRect()
    return r.width > 0 && r.height > 0 && r.right + window.scrollX > clientWidth + 1
  }
  const overflow = []
  for (const el of document.body.querySelectorAll('*')) {
    if (!past(el) || clipsX(el)) continue
    if (el.parentElement && el.parentElement !== document.body && past(el.parentElement) && !clipsX(el.parentElement)) continue
    const r = el.getBoundingClientRect()
    overflow.push({ element: describe(el), right: Math.round(r.right + window.scrollX), width: Math.round(r.width) })
  }
  // What a finger can hit, not what is drawn: a target is big enough when every point of a 44 px
  // square on its centre lands on it -- which is how a hit area grown past a small drawing (a
  // chip's or a checkbox's) is honoured. Off screen, where nothing can be hit-tested, its box decides.
  const HALF = 21
  const hitArea = (el, r) => {
    const cx = r.left + r.width / 2
    const cy = r.top + r.height / 2
    if (cx - HALF < 0 || cy - HALF < 0 || cx + HALF > window.innerWidth || cy + HALF > window.innerHeight) {
      return r.width >= 44 && r.height >= 44
    }
    for (const dx of [-HALF, 0, HALF]) {
      for (const dy of [-HALF, 0, HALF]) {
        const hit = document.elementFromPoint(cx + dx, cy + dy)
        if (!hit || !(hit === el || el.contains(hit))) return false
      }
    }
    return true
  }
  const small = []
  let total = 0
  for (const el of document.querySelectorAll(targetsSelector)) {
    if (el.closest('.monaco-editor')) continue
    if (el instanceof HTMLInputElement && el.type === 'hidden') continue
    // Not a target anyone taps: an element hidden from assistive technology and drawn invisible
    // (the native input behind a styled select).
    if (el.closest('[aria-hidden="true"]') || getComputedStyle(el).opacity === '0') continue
    if (!el.checkVisibility({ visibilityProperty: true })) continue
    const r = el.getBoundingClientRect()
    if (r.width === 0 || r.height === 0) continue
    total++
    if (r.width >= 44 && r.height >= 44) continue
    if (!hitArea(el, r)) small.push({ element: describe(el), width: Math.round(r.width), height: Math.round(r.height) })
  }
  return {
    clientWidth, scrollWidth: de.scrollWidth, innerWidth: window.innerWidth,
    wide: de.scrollWidth > clientWidth, overflow: overflow.slice(0, 30), overflowCount: overflow.length,
    targets: { total, small: small.length, list: small },
  }
}

// -- the scene drivers -------------------------------------------------------------------------

/** The run view's scene canvas (the largest canvas on the page), and a point on it no panel covers
 *  with the room around that point: gestures are centred there and sized to fit. */
async function sceneSpot(page) {
  return page.evaluate(() => {
    const canvases = [...document.querySelectorAll('canvas')].filter((c) => c.checkVisibility())
    if (!canvases.length) return null
    const area = (c) => { const r = c.getBoundingClientRect(); return r.width * r.height }
    const canvas = canvases.sort((a, b) => area(b) - area(a))[0]
    canvas.setAttribute('data-e2e-scene', '')
    const r = canvas.getBoundingClientRect()
    const x0 = Math.max(r.left, 0)
    const y0 = Math.max(r.top, 0)
    const x1 = Math.min(r.right, window.innerWidth)
    const y1 = Math.min(r.bottom, window.innerHeight)
    const step = 8
    const hits = []
    for (let y = y0 + step / 2; y < y1; y += step) {
      for (let x = x0 + step / 2; x < x1; x += step) {
        if (document.elementFromPoint(x, y) === canvas) hits.push([x, y])
      }
    }
    if (!hits.length) return { rect: { x: r.x, y: r.y, width: r.width, height: r.height }, centre: null, clearance: 0 }
    const hitSet = new Set(hits.map(([x, y]) => `${x},${y}`))
    // The hit point farthest from any miss (or the viewport's edge): the open middle of the scene.
    let best = null
    for (const [x, y] of hits) {
      let d = Math.min(x - x0, x1 - x, y - y0, y1 - y)
      for (let k = step; k < d; k += step) {
        const ring = [[x + k, y], [x - k, y], [x, y + k], [x, y - k], [x + k, y + k], [x - k, y - k], [x + k, y - k], [x - k, y + k]]
        if (ring.some(([a, b]) => a >= x0 && a < x1 && b >= y0 && b < y1 && !hitSet.has(`${a},${b}`))) { d = k; break }
      }
      if (!best || d > best.clearance) best = { x, y, clearance: d }
    }
    return {
      rect: { x: r.x, y: r.y, width: r.width, height: r.height },
      centre: { x: best.x, y: best.y }, clearance: best.clearance,
      grid: { x0, y0, step, hits: [...hitSet] },
    }
  })
}

async function viewState(page) {
  return page.evaluate(() => ({ scrollY: window.scrollY, scrollX: window.scrollX, scale: window.visualViewport?.scale ?? 1 }))
}

async function canvasShot(page, rect, file) {
  const vp = page.viewportSize()
  const clip = {
    x: Math.max(0, rect.x), y: Math.max(0, rect.y),
    width: Math.min(rect.width, vp.width - Math.max(0, rect.x)),
    height: Math.min(rect.height, vp.height - Math.max(0, rect.y)),
  }
  const buf = await page.screenshot({ clip })
  if (file) writeFileSync(file, buf)
  return { img: decodePng(buf), clip }
}

/** Where on screen a box of the scene is drawn, found by its colour in a canvas screenshot, and
 *  only where nothing covers the canvas. */
async function boxOnScreen(page, spot) {
  const { img, clip } = await canvasShot(page, spot.rect)
  const scale = img.width / clip.width
  // Only pixels where the canvas itself is on top: the camera panel's picture shows the same colours.
  const { x0, y0, step, hits } = spot.grid
  const open = new Set(hits)
  const uncovered = (px, py) => {
    const x = clip.x + px / scale
    const y = clip.y + py / scale
    const gx = x0 + step / 2 + step * Math.floor((x - x0) / step)
    const gy = y0 + step / 2 + step * Math.floor((y - y0) / step)
    return open.has(`${gx},${gy}`)
  }
  // A pixel of each box's hue (the boxes are lit, so their shade varies but their hue does not).
  const hues = {
    demo_crate_red: (r, g, b) => r > 90 && r > 1.8 * g && r > 1.8 * b,
    demo_crate_blue: (r, g, b) => b > 90 && b > 1.8 * r && b > 1.3 * g,
    demo_crate_green: (r, g, b) => g > 80 && g > 1.6 * r && g > 1.6 * b,
    demo_crate_yellow: (r, g, b) => r > 120 && g > 100 && b < 0.5 * Math.min(r, g),
  }
  for (const body of SCENE_BODIES) {
    const hue = hues[body.name]
    const hit = centroidOf(img, (r, g, b, px, py) => hue(r, g, b) && uncovered(px, py))
    if (!hit) continue
    const x = clip.x + hit.x / scale
    const y = clip.y + hit.y / scale
    const onCanvas = await page.evaluate(([px, py]) =>
      document.elementFromPoint(px, py)?.hasAttribute('data-e2e-scene') ?? false, [x, y])
    if (onCanvas) return { x, y, body: body.name }
  }
  return null
}

/** Wait until the scene stops moving (orbit damping, a camera still flying), up to `maxMs`. */
async function waitStill(page, rect, maxMs = 5000) {
  const end = Date.now() + maxMs
  let prev = (await canvasShot(page, rect)).img
  while (Date.now() < end) {
    await page.waitForTimeout(250)
    const next = (await canvasShot(page, rect)).img
    if (changedFraction(prev, next) < 0.002) return true
    prev = next
  }
  return false
}

/** Put the run view back as it opened, so each gesture is judged from the same framing rather than
 *  from wherever the previous one left the camera. */
async function reopen(page) {
  await page.reload()
  await page.locator('canvas').first().waitFor({ state: 'visible', timeout: 15_000 })
  await page.waitForTimeout(1500)
  await page.evaluate(() => window.scrollTo(0, 0))
  const spot = await sceneSpot(page)
  await waitStill(page, spot.rect)
  return spot
}

/** Run one gesture and judge it: the page must not scroll or zoom, and the scene must change. */
async function judged(page, rect, name, out, prefix, act) {
  await waitStill(page, rect)
  const before = await viewState(page)
  const shotBefore = await canvasShot(page, rect, join(out, `${prefix}-${name}-before.png`))
  const detail = await act()
  await page.waitForTimeout(300)
  await waitStill(page, rect)
  const after = await viewState(page)
  const shotAfter = await canvasShot(page, rect, join(out, `${prefix}-${name}-after.png`))
  const changed = changedFraction(shotBefore.img, shotAfter.img)
  const scrolled = after.scrollY !== before.scrollY || after.scrollX !== before.scrollX
  const zoomed = Math.abs(after.scale - before.scale) > 1e-3
  const pass = !scrolled && !zoomed && changed > 0.01
  await page.waitForTimeout(600)
  return {
    gesture: name, pass, canvasChanged: Number(changed.toFixed(4)),
    scrollY: { before: before.scrollY, after: after.scrollY },
    scale: { before: before.scale, after: after.scale },
    ...(detail ? { detail } : {}),
  }
}

/** Touch points along a path: `at(f)` gives the fingers at fraction `f` of the gesture. */
async function touchPath(cdp, at, ms, steps = 24) {
  const pts = (f) => at(f).map(([x, y], id) => ({ x, y, id, radiusX: 6, radiusY: 6, force: 1 }))
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: pts(0) })
  for (let i = 1; i <= steps; i++) {
    await sleep(ms / steps)
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: pts(i / steps) })
  }
  await sleep(60)
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] })
}

async function touchDriver(browser, base, dist, service, unanswered, out) {
  const { defaultBrowserType, ...device } = devices['Pixel 7']
  void defaultBrowserType
  const videoDir = join(out, '.video')
  const context = await browser.newContext({
    ...device, serviceWorkers: 'block', recordVideo: { dir: videoDir, size: device.viewport },
  })
  await context.addInitScript(EVENT_SOURCE_STANDIN)
  await context.addInitScript(TOUCH_DOTS)
  await routeContext(context, base, dist, service, unanswered)
  const { page, ready } = await openPage(context, base, PAGES.find((p) => p.name === 'run'))
  await page.waitForTimeout(1500)
  const spot = await sceneSpot(page)
  const result = { profile: 'pixel7', orientation: 'portrait', ready, spot, gestures: [] }
  if (!spot?.centre) {
    result.error = 'no part of the scene canvas is uncovered'
  } else {
    const cdp = await context.newCDPSession(page)
    const { x: cx, y: cy } = spot.centre
    const reach = Math.max(30, Math.min(spot.clearance * 0.85, 140))
    let first = true
    const g = async (name, act) => {
      const at = first ? spot : await reopen(page)
      first = false
      return judged(page, at.rect, name, out, 'touch', act)
    }
    // First, on the opening framing, where every box is in view.
    await waitStill(page, spot.rect)
    const box = await boxOnScreen(page, spot)
    result.gestures.push(await g('double-tap', async () => {
      if (!box) return { error: 'no box of the scene is visible and uncovered' }
      for (let i = 0; i < 2; i++) {
        await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: box.x, y: box.y, id: 0 }] })
        await sleep(60)
        await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] })
        await sleep(120)
      }
      return { at: { x: Math.round(box.x), y: Math.round(box.y) } }
    }))
    result.gestures.push(await g('drag', () =>
      touchPath(cdp, (f) => [[cx - reach * 0.6 + reach * 1.2 * f, cy - reach * 0.2 + reach * 0.4 * f]], 800)))
    result.gestures.push(await g('pinch-out', () =>
      touchPath(cdp, (f) => {
        const d = reach * (0.25 + 0.7 * f)
        return [[cx - d, cy], [cx + d, cy]]
      }, 800)))
    result.gestures.push(await g('pinch-in', () =>
      touchPath(cdp, (f) => {
        const d = reach * (0.95 - 0.7 * f)
        return [[cx - d, cy], [cx + d, cy]]
      }, 800)))
    result.gestures.push(await g('two-finger-pan', () =>
      touchPath(cdp, (f) => {
        const dy = reach * (-0.5 + 1.0 * f)
        return [[cx - reach * 0.4, cy + dy], [cx + reach * 0.4, cy + dy]]
      }, 800)))
    result.gestures.push(await g('twist', () =>
      touchPath(cdp, (f) => {
        const a = (40 * Math.PI / 180) * f
        const r = reach * 0.6
        return [[cx - r * Math.cos(a), cy - r * Math.sin(a)], [cx + r * Math.cos(a), cy + r * Math.sin(a)]]
      }, 900)))
  }
  const video = page.video()
  await context.close()
  if (video) {
    await video.saveAs(join(out, 'scene-touch.webm'))
    await video.delete()
  }
  rmSync(videoDir, { recursive: true, force: true })
  writeFileSync(join(out, 'touch.json'), JSON.stringify(result, null, 2))
  return result
}

async function mouseDriver(browser, base, dist, service, unanswered, out) {
  const context = await browser.newContext({ ...DESKTOP, serviceWorkers: 'block' })
  await context.addInitScript(EVENT_SOURCE_STANDIN)
  await routeContext(context, base, dist, service, unanswered)
  const { page, ready } = await openPage(context, base, PAGES.find((p) => p.name === 'run'))
  await page.waitForTimeout(1500)
  const spot = await sceneSpot(page)
  const result = { profile: 'desktop', ready, spot, gestures: [] }
  if (!spot?.centre) {
    result.error = 'no part of the scene canvas is uncovered'
  } else {
    const { x: cx, y: cy } = spot.centre
    const reach = Math.max(30, Math.min(spot.clearance * 0.85, 200))
    let first = true
    const g = async (name, act) => {
      const at = first ? spot : await reopen(page)
      first = false
      return judged(page, at.rect, name, out, 'mouse', act)
    }
    const drag = async (button) => {
      await page.mouse.move(cx - reach * 0.5, cy)
      await page.mouse.down({ button })
      await page.mouse.move(cx + reach * 0.5, cy + reach * 0.2, { steps: 20 })
      await page.mouse.up({ button })
    }
    await waitStill(page, spot.rect)
    const box = await boxOnScreen(page, spot)
    result.gestures.push(await g('double-click', async () => {
      if (!box) return { error: 'no box of the scene is visible and uncovered' }
      await page.mouse.dblclick(box.x, box.y)
      return { at: { x: Math.round(box.x), y: Math.round(box.y) } }
    }))
    result.gestures.push(await g('wheel', async () => {
      await page.mouse.move(cx, cy)
      for (let i = 0; i < 5; i++) {
        await page.mouse.wheel(0, -120)
        await sleep(80)
      }
    }))
    result.gestures.push(await g('left-drag', () => drag('left')))
    result.gestures.push(await g('right-drag', () => drag('right')))
  }
  await context.close()
  writeFileSync(join(out, 'mouse.json'), JSON.stringify(result, null, 2))
  return result
}

// -- the PWA probe -----------------------------------------------------------------------------

async function pwaProbe(base, dist, service, unanswered, out) {
  const { defaultBrowserType, ...device } = devices['Pixel 7']
  void defaultBrowserType
  // The full Chromium (new headless) with a profile on disk: the headless shell the pages are shot
  // in has no installability checks, and an incognito context is itself an installability error.
  // Service workers are allowed here only: elsewhere a worker's cache could answer in the
  // fixtures' place.
  const profileDir = mkdtempSync(join(tmpdir(), 'robovast-e2e-pwa-'))
  const context = await chromium.launchPersistentContext(profileDir, {
    channel: 'chromium', ...device, serviceWorkers: 'allow',
  })
  await context.addInitScript(EVENT_SOURCE_STANDIN)
  await routeContext(context, base, dist, service, unanswered)
  const { page } = await openPage(context, base, PAGES.find((p) => p.name === 'campaigns'))
  await page.waitForTimeout(3000)
  const cdp = await context.newCDPSession(page)
  const manifest = await cdp.send('Page.getAppManifest').catch((e) => ({ error: e.message }))
  const installability = await cdp.send('Page.getInstallabilityErrors').catch((e) => ({ error: e.message }))
  const worker = await page.evaluate(async () => {
    const regs = navigator.serviceWorker ? await navigator.serviceWorker.getRegistrations() : []
    const caches_ = {}
    if (self.caches) {
      for (const key of await caches.keys()) {
        caches_[key] = (await (await caches.open(key)).keys()).map((r) => r.url)
      }
    }
    return {
      supported: !!navigator.serviceWorker,
      controller: navigator.serviceWorker?.controller?.scriptURL ?? null,
      registrations: regs.map((r) => ({
        scope: r.scope,
        active: r.active ? { scriptURL: r.active.scriptURL, state: r.active.state } : null,
        waiting: r.waiting ? r.waiting.state : null,
        installing: r.installing ? r.installing.state : null,
      })),
      caches: caches_,
    }
  })
  await context.close()
  const result = {
    manifest: manifest.error ? manifest : {
      url: manifest.url, errors: manifest.errors, parsed: manifest.data ? JSON.parse(manifest.data) : null,
    },
    installabilityErrors: installability.installabilityErrors ?? installability,
    serviceWorker: worker,
  }
  rmSync(profileDir, { recursive: true, force: true })
  writeFileSync(join(out, 'pwa.json'), JSON.stringify(result, null, 2))
  return result
}

// -- main --------------------------------------------------------------------------------------

async function main() {
  const opts = parseArgs(process.argv.slice(2))
  mkdirSync(opts.out, { recursive: true })
  const pages = opts.pages ? PAGES.filter((p) => opts.pages.includes(p.name)) : PAGES
  if (opts.pages && pages.length !== opts.pages.length) {
    throw new Error(`--pages: known pages are ${PAGES.map((p) => p.name).join(', ')}`)
  }
  const service = fixtureService({ loginHtml: loginHtml() })
  const unanswered = new Set()
  const { base, child } = await startPreview(opts.dist)
  const browser = await chromium.launch()
  const report = { base, dist: opts.dist, pages: [] }
  try {
    for (const prof of profiles()) {
      const context = await browser.newContext({ ...prof.device, serviceWorkers: 'block' })
      await context.addInitScript(EVENT_SOURCE_STANDIN)
      await routeContext(context, base, opts.dist, service, unanswered)
      for (const def of pages) {
        const { page, errors, ready } = await openPage(context, base, def)
        const file = `${def.name}-${prof.profile}-${prof.orientation}.png`
        await page.screenshot({ path: join(opts.out, file) })
        const probed = await page.evaluate(probe, TARGETS)
        report.pages.push({
          page: def.name, profile: prof.profile, orientation: prof.orientation, phone: prof.phone,
          viewport: prof.device.viewport, screenshot: file, ready, ...probed, errors,
        })
        await page.close()
      }
      await context.close()
    }
    if (opts.touch) report.touch = await touchDriver(browser, base, opts.dist, service, unanswered, opts.out)
    if (opts.mouse) report.mouse = await mouseDriver(browser, base, opts.dist, service, unanswered, opts.out)
    if (opts.pwa) report.pwa = await pwaProbe(base, opts.dist, service, unanswered, opts.out)
  } finally {
    await browser.close()
    stopPreview(child)
  }
  report.unanswered = [...unanswered].sort()
  report.sqlMissingTables = [...service.sqlMisses].sort()
  writeFileSync(join(opts.out, 'report.json'), JSON.stringify(
    { ...report, touch: undefined, mouse: undefined, pwa: undefined }, null, 2))

  // -- the summary
  const failures = []
  console.log(`\n${'page'.padEnd(10)} ${'profile'.padEnd(20)} ${'ready'.padEnd(6)} ${'width (scroll/client)'.padEnd(24)} targets <44px`)
  for (const p of report.pages) {
    const width = `${p.wide ? 'WIDE' : 'fits'} ${p.scrollWidth}/${p.clientWidth}`
    console.log(`${p.page.padEnd(10)} ${`${p.profile} ${p.orientation}`.padEnd(20)} ${String(p.ready).padEnd(6)} ` +
      `${width.padEnd(24)} ${p.targets.small}/${p.targets.total}${p.errors.length ? `  (${p.errors.length} errors)` : ''}`)
    if (p.phone && p.wide) failures.push(`${p.page} ${p.profile} ${p.orientation}: ${p.scrollWidth} px wide in ${p.clientWidth}`)
    if (p.phone && ['campaigns', 'run', 'login'].includes(p.page) && p.targets.small) {
      failures.push(`${p.page} ${p.profile} ${p.orientation}: ${p.targets.small} targets under 44 px`)
    }
  }
  for (const [name, r] of [['touch', report.touch], ['mouse', report.mouse]]) {
    if (!r) continue
    console.log(`\n${name} (${r.profile}${r.orientation ? ` ${r.orientation}` : ''})${r.error ? `: ${r.error}` : ''}`)
    for (const g of r.gestures) {
      console.log(`  ${g.gesture.padEnd(16)} ${g.pass ? 'pass' : 'FAIL'}  canvas ${(g.canvasChanged * 100).toFixed(1)} %  ` +
        `scrollY ${g.scrollY.before}→${g.scrollY.after}  scale ${g.scale.before}→${g.scale.after}` +
        `${g.detail?.error ? `  (${g.detail.error})` : ''}`)
    }
  }
  if (report.pwa) {
    const m = report.pwa.manifest
    console.log(`\npwa: manifest ${m.url ? m.url : 'none'}; installability errors: ` +
      `${JSON.stringify(report.pwa.installabilityErrors)}; service workers: ${report.pwa.serviceWorker.registrations.length}`)
  }
  console.log(`\nunanswered (${report.unanswered.length}):`)
  for (const u of report.unanswered) console.log(`  ${u}`)
  if (report.sqlMissingTables.length) {
    console.log(`\ntables queried that the fixtures lack (answered with the service's error):`)
    for (const t of report.sqlMissingTables) console.log(`  ${t}`)
  }
  console.log(`\nwritten to ${opts.out}`)
  if (opts.strict && failures.length) {
    console.log(`\n--strict: ${failures.length} failures`)
    for (const f of failures) console.log(`  ${f}`)
    process.exitCode = 1
  }
}

main().catch((e) => {
  console.error(e.stack || String(e))
  process.exit(2)
})
