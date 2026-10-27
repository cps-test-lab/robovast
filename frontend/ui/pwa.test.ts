// The install files: the manifest's fields, the icons' sizes, and which files are the app shell.
import { readFileSync } from 'node:fs'
import { fileURLToPath, URL } from 'node:url'
import { describe, expect, it } from 'vitest'
import {
  APPLE_TOUCH_ICON,
  ICONS,
  buildWorker,
  iconSvg,
  markBody,
  renderIcon,
  shellFiles,
  webManifest,
  type BundleLike,
  type PwaOptions,
} from './pwa'

const here = (path: string) => fileURLToPath(new URL(path, import.meta.url))
const MARK = readFileSync(here('./public/favicon.svg'), 'utf8')

const OPTS: PwaOptions = {
  name: 'RoboVAST',
  shortName: 'RoboVAST',
  description: 'test',
  background: '#101316',
  theme: '#101316',
  markSvg: MARK,
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
      background_color: '#101316',
      theme_color: '#101316',
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

  it('render at the size each declares', async () => {
    for (const icon of [...ICONS, APPLE_TOUCH_ICON]) {
      const png = await renderIcon(iconSvg(MARK, icon, OPTS.background), icon.size)
      expect(pngSize(png)).toEqual({ width: icon.size, height: icon.size })
    }
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
  it('writes the list and a build id in, and a different list is a different worker', async () => {
    const a = await buildWorker(OPTS.workerEntry, ['/assets/index-a.js'])
    const b = await buildWorker(OPTS.workerEntry, ['/assets/index-b.js'])
    expect(a.code).toContain('/assets/index-a.js')
    expect(a.code).toContain(a.buildId)
    expect(a.code).not.toContain('__SHELL_BUILD_ID__')
    expect(a.buildId).not.toBe(b.buildId)
    const again = await buildWorker(OPTS.workerEntry, ['/assets/index-a.js'])
    expect(again.buildId).toBe(a.buildId)
  }, 30_000)
})
