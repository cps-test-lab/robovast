// What makes the web UI installable as an app: a web app manifest, its icons, and a service worker
// that caches the app shell (src/sw/). All of it is generated here, at build time, from what the
// build itself knows -- the colours from src/colors.ts, the icons from the mark in public/favicon.svg,
// the shell from the bundle's own entry chunk -- so none of it is a second copy that can drift.
//
// Build only: under `npm run dev` there is no manifest and no worker, and nothing caches a module
// that is about to be replaced.

import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { fileURLToPath, URL } from 'node:url'
import { build as esbuild } from 'esbuild'
import { initWasm, Resvg } from '@resvg/resvg-wasm'
import type { Plugin } from 'vite'
import { ACTIVE } from './src/colors'
import { PWA_FILES } from './src/lib/pwaFiles'

export { PWA_FILES }

export interface IconSpec {
  file: string
  size: number
  purpose: 'any' | 'maskable'
  /** The mark's size as a share of the icon, and whether the ground is a rounded tile or full bleed. */
  scale: number
  ground: 'tile' | 'bleed'
}

/** The sizes Android and iOS ask for. A maskable icon is cropped to the platform's shape, so its
 *  mark sits well inside the safe zone (the centre 80 % circle); iOS rounds its own corners, so its
 *  icon is full bleed. */
export const ICONS: IconSpec[] = [
  { file: 'icons/icon-192.png', size: 192, purpose: 'any', scale: 0.8, ground: 'tile' },
  { file: 'icons/icon-512.png', size: 512, purpose: 'any', scale: 0.8, ground: 'tile' },
  { file: 'icons/icon-maskable-512.png', size: 512, purpose: 'maskable', scale: 0.6, ground: 'bleed' },
]
export const APPLE_TOUCH_ICON: IconSpec = {
  file: 'icons/apple-touch-icon.png', size: 180, purpose: 'any', scale: 0.75, ground: 'bleed',
}

export interface PwaOptions {
  name: string
  shortName: string
  description: string
  /** The page's ground: the splash screen and the icons' ground. */
  background: string
  /** The browser chrome around the app. */
  theme: string
  /** The mark's SVG source, drawn on a 40 x 40 view box. */
  markSvg: string
  /** The service worker's entry module. */
  workerEntry: string
}

/** The web app manifest. `start_url` is the app's root, which opens on the campaign list. */
export function webManifest(opts: PwaOptions, base: string): Record<string, unknown> {
  return {
    id: base,
    name: opts.name,
    short_name: opts.shortName,
    description: opts.description,
    start_url: base,
    scope: base,
    display: 'standalone',
    background_color: opts.background,
    theme_color: opts.theme,
    icons: ICONS.map((icon) => ({
      src: `${base}${icon.file}`,
      sizes: `${icon.size}x${icon.size}`,
      type: 'image/png',
      purpose: icon.purpose,
    })),
  }
}

/** The mark's drawing: what is inside its `<svg>`, without comments and without the style sheet
 *  that recolours it for a light tab strip -- an icon always sits on its own dark ground. */
export function markBody(svg: string): string {
  const open = svg.search(/<svg[\s>]/)
  const inner = svg.slice(svg.indexOf('>', open) + 1, svg.lastIndexOf('</svg>'))
  return inner.replace(/<!--[\s\S]*?-->/g, '').replace(/<style[\s\S]*?<\/style>/g, '').trim()
}

/** One icon's SVG: the mark at `icon.scale` of the icon, centred, on the ground. */
export function iconSvg(mark: string, icon: IconSpec, ground: string): string {
  const s = icon.scale
  const tile = icon.ground === 'tile'
    ? `<rect width="40" height="40" rx="9" fill="${ground}"/>`
    : `<rect width="40" height="40" fill="${ground}"/>`
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40" width="${icon.size}" height="${icon.size}">`
    + tile
    + `<g transform="translate(20 20) scale(${s}) translate(-20 -20)">${markBody(mark)}</g></svg>`
}

// The renderer's WebAssembly, loaded once per build however many icons are drawn.
let wasmReady: Promise<void> | null = null
function resvgWasm(): Promise<void> {
  const require = createRequire(import.meta.url)
  wasmReady ??= initWasm(readFileSync(require.resolve('@resvg/resvg-wasm/index_bg.wasm')))
  return wasmReady
}

/** Rasterise an icon's SVG to PNG bytes. */
export async function renderIcon(svg: string, size: number): Promise<Uint8Array> {
  await resvgWasm()
  return new Resvg(svg, { fitTo: { mode: 'width', value: size } }).render().asPng()
}

/** A bundle as far as the shell is concerned: Rollup's output entries, reduced to what is read. */
export type BundleLike = Record<string, {
  type: 'chunk' | 'asset'
  fileName: string
  isEntry?: boolean
  imports?: string[]
  viteMetadata?: { importedCss?: Set<string> }
}>

/** The app shell: the entry chunk, every chunk it imports statically (transitively), and their CSS.
 *  Lazily loaded chunks -- the editor, the charts, three -- are not the shell: they load with the page
 *  that needs them, from the HTTP cache. */
export function shellFiles(bundle: BundleLike): string[] {
  const out = new Set<string>()
  const visit = (fileName: string) => {
    const chunk = bundle[fileName]
    if (!chunk || chunk.type !== 'chunk' || out.has(fileName)) return
    out.add(fileName)
    chunk.viteMetadata?.importedCss?.forEach((css) => out.add(css))
    chunk.imports?.forEach(visit)
  }
  const entries = Object.values(bundle).filter((c) => c.type === 'chunk' && c.isEntry)
  if (!entries.length) throw new Error('pwa: the bundle has no entry chunk, so there is no shell to cache')
  entries.forEach((c) => visit(c.fileName))
  return [...out].sort()
}

/** The service worker for a precache list: built from its entry module with the list and the build
 *  id written in. The id is a hash of the worker's own code and of the list, so a build that changes
 *  either is a new worker and anything else is the same one. */
export async function buildWorker(entry: string, precache: string[]): Promise<{ code: string; buildId: string }> {
  const PLACEHOLDER = '__SHELL_BUILD_ID__'
  const result = await esbuild({
    entryPoints: [entry],
    bundle: true,
    write: false,
    format: 'iife',
    target: 'es2020',
    minify: true,
    define: {
      SHELL_PRECACHE: JSON.stringify(precache),
      SHELL_BUILD_ID: JSON.stringify(PLACEHOLDER),
    },
  })
  const code = result.outputFiles[0].text
  if (!code.includes(PLACEHOLDER)) throw new Error('pwa: the worker does not read SHELL_BUILD_ID')
  const buildId = createHash('sha256').update(code).update(precache.join('\n')).digest('hex').slice(0, 16)
  return { code: code.split(PLACEHOLDER).join(buildId), buildId }
}

export function pwa(opts: PwaOptions): Plugin {
  let base = '/'
  return {
    name: 'robovast-pwa',
    apply: 'build',
    configResolved(config) {
      base = config.base
    },
    transformIndexHtml() {
      return [
        // With credentials: the service answers nothing without its session cookie, and a manifest
        // link is fetched without one unless it asks.
        { tag: 'link', attrs: { rel: 'manifest', href: `${base}${PWA_FILES.manifest}`, crossorigin: 'use-credentials' }, injectTo: 'head' },
        { tag: 'meta', attrs: { name: 'theme-color', content: opts.theme }, injectTo: 'head' },
        { tag: 'link', attrs: { rel: 'apple-touch-icon', href: `${base}${APPLE_TOUCH_ICON.file}` }, injectTo: 'head' },
        // iOS reads these, not the manifest, for a home-screen app: full screen, and a status bar
        // over the app's own dark ground.
        { tag: 'meta', attrs: { name: 'apple-mobile-web-app-capable', content: 'yes' }, injectTo: 'head' },
        { tag: 'meta', attrs: { name: 'apple-mobile-web-app-status-bar-style', content: 'black-translucent' }, injectTo: 'head' },
        { tag: 'meta', attrs: { name: 'apple-mobile-web-app-title', content: opts.shortName }, injectTo: 'head' },
      ]
    },
    async generateBundle(_options, bundle) {
      for (const icon of [...ICONS, APPLE_TOUCH_ICON]) {
        this.emitFile({
          type: 'asset',
          fileName: icon.file,
          source: await renderIcon(iconSvg(opts.markSvg, icon, opts.background), icon.size),
        })
      }
      this.emitFile({
        type: 'asset',
        fileName: PWA_FILES.manifest,
        source: `${JSON.stringify(webManifest(opts, base), null, 2)}\n`,
      })
      const precache = [
        ...shellFiles(bundle as unknown as BundleLike),
        PWA_FILES.manifest,
        ...[...ICONS, APPLE_TOUCH_ICON].map((i) => i.file),
      ].map((f) => `${base}${f}`)
      const { code } = await buildWorker(opts.workerEntry, precache)
      this.emitFile({ type: 'asset', fileName: PWA_FILES.worker, source: code })
    },
  }
}

/** This app's own options, from the files they are kept in. */
export function robovastPwa(): Plugin {
  const here = (path: string) => fileURLToPath(new URL(path, import.meta.url))
  return pwa({
    name: 'RoboVAST',
    shortName: 'RoboVAST',
    description: 'Follow, read and replay RoboVAST campaigns.',
    background: ACTIVE.surface.bg,
    theme: ACTIVE.surface.bg,
    markSvg: readFileSync(here('./public/favicon.svg'), 'utf8'),
    workerEntry: here('./src/sw/worker.ts'),
  })
}
