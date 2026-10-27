// What makes the web UI installable as an app: a web app manifest, its icons, and a service worker
// that caches the app shell (src/sw/). The manifest and the worker are generated here, at build time,
// from what the build itself knows -- the colours from src/colors.ts, the shell from the bundle's own
// entry chunk -- so neither is a second copy that can drift.
//
// The icons are the one exception, because rasterising an SVG needs a renderer the build does not
// carry: `npm run icons` (icons/render.ts) draws them from the mark in public/favicon.svg, and they
// are committed in icons/ beside the fingerprint of what they were drawn from. The build recomputes
// that fingerprint and refuses to run when it differs, so a changed mark, palette or icon size cannot
// ship stale icons.
//
// Build only: under `npm run dev` there is no manifest and no worker, and nothing caches a module
// that is about to be replaced.

import { createHash } from 'node:crypto'
import { existsSync, readFileSync } from 'node:fs'
import { basename, join } from 'node:path'
import { fileURLToPath, URL } from 'node:url'
import { build as esbuild } from 'esbuild'
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
  /** Where the rendered icons and their fingerprint are kept (`npm run icons` writes them). */
  iconDir: string
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

/** Every icon the app ships: the manifest's and the apple-touch-icon. */
export const ALL_ICONS: IconSpec[] = [...ICONS, APPLE_TOUCH_ICON]

/** What the icons are drawn from -- each one's file, size, purpose and SVG, so the mark, the ground
 *  colour and the scale all count -- as one hash. */
export function iconsFingerprint(markSvg: string, ground: string): string {
  const drawn = ALL_ICONS.map((i) => [i.file, i.size, i.purpose, iconSvg(markSvg, i, ground)])
  return createHash('sha256').update(JSON.stringify(drawn)).digest('hex')
}

/** The fingerprint file beside the rendered icons. */
export const FINGERPRINT_FILE = 'fingerprint'

/** The rendered icons, by the file name each is emitted under -- refused, naming the fix, when they
 *  were drawn from something other than the current mark and palette, or one is missing. */
export function readIcons(opts: Pick<PwaOptions, 'markSvg' | 'background' | 'iconDir'>): Map<string, Uint8Array> {
  const fingerprintPath = join(opts.iconDir, FINGERPRINT_FILE)
  const recorded = existsSync(fingerprintPath) ? readFileSync(fingerprintPath, 'utf8').trim() : '(none)'
  const current = iconsFingerprint(opts.markSvg, opts.background)
  if (recorded !== current) {
    throw new Error(
      `pwa: the icons in ${opts.iconDir} were not drawn from the current mark, palette and sizes `
      + `(fingerprint ${recorded}, expected ${current}). Run \`npm run icons\` and commit the result.`,
    )
  }
  const out = new Map<string, Uint8Array>()
  for (const icon of ALL_ICONS) {
    const path = join(opts.iconDir, basename(icon.file))
    if (!existsSync(path)) throw new Error(`pwa: ${path} is missing. Run \`npm run icons\`.`)
    out.set(icon.file, readFileSync(path))
  }
  return out
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
      for (const [fileName, source] of readIcons(opts)) {
        this.emitFile({ type: 'asset', fileName, source })
      }
      this.emitFile({
        type: 'asset',
        fileName: PWA_FILES.manifest,
        source: `${JSON.stringify(webManifest(opts, base), null, 2)}\n`,
      })
      const precache = [
        ...shellFiles(bundle as unknown as BundleLike),
        PWA_FILES.manifest,
        ...ALL_ICONS.map((i) => i.file),
      ].map((f) => `${base}${f}`)
      const { code } = await buildWorker(opts.workerEntry, precache)
      this.emitFile({ type: 'asset', fileName: PWA_FILES.worker, source: code })
    },
  }
}

/** This app's own options, from the files they are kept in. */
export function robovastPwaOptions(): PwaOptions {
  const here = (path: string) => fileURLToPath(new URL(path, import.meta.url))
  return {
    name: 'RoboVAST',
    shortName: 'RoboVAST',
    description: 'Follow, read and replay RoboVAST campaigns.',
    background: ACTIVE.surface.bg,
    theme: ACTIVE.surface.bg,
    markSvg: readFileSync(here('./public/favicon.svg'), 'utf8'),
    iconDir: here('./icons'),
    workerEntry: here('./src/sw/worker.ts'),
  }
}

export function robovastPwa(): Plugin {
  return pwa(robovastPwaOptions())
}
