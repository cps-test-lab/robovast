// The install files: the manifest's fields, the icons' sizes, and which files are the app shell.
import { readFileSync } from 'node:fs'
import { fileURLToPath, URL } from 'node:url'
import { describe, expect, it } from 'vitest'
import {
  ALL_ICONS,
  ICONS,
  buildWorker,
  bundleBytes,
  markBody,
  readIcons,
  shellFiles,
  webManifest,
  type BundleLike,
  type PwaOptions,
} from './pwa'
import { ACTIVE } from './src/colors'

const here = (path: string) => fileURLToPath(new URL(path, import.meta.url))
const MARK = readFileSync(here('./public/favicon.svg'), 'utf8')

const OPTS: PwaOptions = {
  name: 'RoboVAST',
  shortName: 'RoboVAST',
  description: 'test',
  background: ACTIVE.surface.bg,
  theme: ACTIVE.surface.bg,
  markSvg: MARK,
  iconDir: here('./icons'),
  workerEntry: here('./src/sw/worker.ts'),
}

/** Width and height from a PNG's IHDR chunk. */
function pngSize(bytes: Uint8Array): { width: number; height: number } {
  const signature = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]
  expect([...bytes.slice(0, 8)]).toEqual(signature)
  const view = new DataView(bytes.buffer, bytes.byteOffset)
  return { width: view.getUint32(16), height: view.getUint32(20) }
}

describe('webManifest', () => {
  it('is a standalone app opening at the root, with an icon of each size and a maskable one', () => {
    const m = webManifest(OPTS, '/')
    expect(m).toMatchObject({
      name: 'RoboVAST',
      short_name: 'RoboVAST',
      start_url: '/',
      scope: '/',
      id: '/',
      display: 'standalone',
      background_color: ACTIVE.surface.bg,
      theme_color: ACTIVE.surface.bg,
    })
    const icons = m.icons as { src: string; sizes: string; purpose: string }[]
    expect(icons.map((i) => i.sizes)).toEqual(expect.arrayContaining(['192x192', '512x512']))
    expect(icons.some((i) => i.purpose === 'maskable' && i.sizes === '512x512')).toBe(true)
    expect(icons.every((i) => i.src.startsWith('/icons/'))).toBe(true)
  })

  it('is served under the base the app is', () => {
    const m = webManifest(OPTS, '/ui/')
    expect(m.start_url).toBe('/ui/')
    expect((m.icons as { src: string }[])[0].src.startsWith('/ui/icons/')).toBe(true)
  })
})

describe('icons', () => {
  it('draw the mark without its light-mode style sheet or its comments', () => {
    const body = markBody(MARK)
    expect(body).toContain('<rect')
    expect(body).not.toContain('<style')
    expect(body).not.toContain('<!--')
    expect(body).not.toContain('<svg')
  })

  it('are committed drawn from the current mark and palette, at the size each declares', () => {
    // `npm run icons` redraws them; this fails when the mark, the ground or a size changed since.
    const icons = readIcons(OPTS)
    for (const icon of ALL_ICONS) {
      expect(pngSize(icons.get(icon.file)!)).toEqual({ width: icon.size, height: icon.size })
    }
  })

  it('are refused, naming the fix, when drawn from another mark or ground', () => {
    const otherMark = MARK.replace('#a8ffcf', '#ff0000')
    expect(() => readIcons({ ...OPTS, markSvg: otherMark })).toThrow(/npm run icons/)
    expect(() => readIcons({ ...OPTS, background: '#ffffff' })).toThrow(/npm run icons/)
  })

  it('keep a maskable icon\'s mark inside the safe zone', () => {
    // The mark spans most of its 40-unit view box; scaled by `scale`, it must fit the centre 80 %.
    for (const icon of ICONS.filter((i) => i.purpose === 'maskable')) {
      expect(icon.scale).toBeLessThanOrEqual(0.8 * 0.85)
      expect(icon.ground).toBe('bleed')
    }
  })
})

describe('shellFiles', () => {
  const css = (...files: string[]) => ({ importedCss: new Set(files) })
  const bundle: BundleLike = {
    'assets/index-a.js': {
      type: 'chunk', fileName: 'assets/index-a.js', isEntry: true,
      imports: ['assets/react-b.js', 'assets/mui-c.js'], viteMetadata: css('assets/index-a.css'),
    },
    'assets/react-b.js': { type: 'chunk', fileName: 'assets/react-b.js', imports: [] },
    'assets/mui-c.js': { type: 'chunk', fileName: 'assets/mui-c.js', imports: ['assets/react-b.js'] },
    // Reached only by a dynamic import: a page's chunk, not the shell.
    'assets/Monitor-d.js': { type: 'chunk', fileName: 'assets/Monitor-d.js', imports: ['assets/mui-c.js'] },
    'assets/monaco-e.js': { type: 'chunk', fileName: 'assets/monaco-e.js', imports: [] },
    'assets/index-a.css': { type: 'asset', fileName: 'assets/index-a.css' },
  }

  it('is the entry, what it imports statically, and their CSS -- never a lazily loaded chunk', () => {
    expect(shellFiles(bundle)).toEqual([
      'assets/index-a.css',
      'assets/index-a.js',
      'assets/mui-c.js',
      'assets/react-b.js',
    ])
  })

  it('refuses a bundle with no entry', () => {
    const { ['assets/index-a.js']: _entry, ...rest } = bundle
    expect(() => shellFiles(rest)).toThrow(/no entry chunk/)
  })
})

describe('buildWorker', () => {
  const shell = (entries: Record<string, string>) => new Map(Object.entries(entries))

  it('writes the list and a build id in, and a different list is a different worker', async () => {
    const a = await buildWorker(OPTS.workerEntry, shell({ '/assets/index-a.js': 'a' }))
    const b = await buildWorker(OPTS.workerEntry, shell({ '/assets/index-b.js': 'a' }))
    expect(a.code).toContain('/assets/index-a.js')
    expect(a.code).toContain(a.buildId)
    expect(a.code).not.toContain('__SHELL_BUILD_ID__')
    expect(a.buildId).not.toBe(b.buildId)
    const again = await buildWorker(OPTS.workerEntry, shell({ '/assets/index-a.js': 'a' }))
    expect(again.buildId).toBe(a.buildId)
  }, 30_000)

  it('is a new worker when only the bytes behind an unhashed URL change', async () => {
    // The manifest and the icons keep their URLs from build to build: a new palette or name must
    // still replace the cached copies.
    const files = { '/assets/index-a.js': 'a', '/manifest.webmanifest': '{"name":"RoboVAST"}' }
    const a = await buildWorker(OPTS.workerEntry, shell(files))
    const b = await buildWorker(OPTS.workerEntry,
      shell({ ...files, '/manifest.webmanifest': '{"name":"RoboVAST 2"}' }))
    expect(b.buildId).not.toBe(a.buildId)
    const icon = new Map<string, string | Uint8Array>([['/icons/icon-192.png', new Uint8Array([1, 2])]])
    const otherIcon = new Map<string, string | Uint8Array>([['/icons/icon-192.png', new Uint8Array([1, 3])]])
    expect((await buildWorker(OPTS.workerEntry, icon)).buildId)
      .not.toBe((await buildWorker(OPTS.workerEntry, otherIcon)).buildId)
  }, 30_000)
})

describe('bundleBytes', () => {
  const bundle: BundleLike = {
    'assets/index-a.js': { type: 'chunk', fileName: 'assets/index-a.js', code: 'js' },
    'assets/index-a.css': { type: 'asset', fileName: 'assets/index-a.css', source: 'css' },
  }

  it('reads a chunk\'s code and an asset\'s source, and refuses a file the bundle lacks', () => {
    expect(bundleBytes(bundle, 'assets/index-a.js')).toBe('js')
    expect(bundleBytes(bundle, 'assets/index-a.css')).toBe('css')
    expect(() => bundleBytes(bundle, 'assets/gone.js')).toThrow(/not in the bundle/)
  })
})
