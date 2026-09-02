import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import {
  entriesDiffer,
  markReloading,
  parseServedEntries,
  reloadAllowed,
} from './servedBuild'

// The shape Vite actually emits, kept verbatim from a real `dist/index.html`: an entry
// `<script type="module">` plus one `<link rel="modulepreload">` per statically-imported
// vendor chunk, and a `<link rel="icon">` that must not be mistaken for either.
function servedHtml(entryHash: string, muiHash = 'DVrcIx1e'): string {
  return `<!doctype html>
<html lang="en">
  <head>
    <title>RoboVAST</title>
    <link rel="icon" type="image/svg+xml" href="/favicon.svg" />
    <script type="module" crossorigin src="/assets/index-${entryHash}.js"></script>
    <link rel="modulepreload" crossorigin href="/assets/react-BKWWG9Gw.js">
    <link rel="modulepreload" crossorigin href="/assets/mui-${muiHash}.js">
  </head>
  <body><div id="root"></div></body>
</html>`
}

describe('parseServedEntries', () => {
  it('collects the entry module and its preloads', () => {
    expect(parseServedEntries(servedHtml('DGxu44bO'))).toEqual([
      '/assets/index-DGxu44bO.js',
      '/assets/mui-DVrcIx1e.js',
      '/assets/react-BKWWG9Gw.js',
    ])
  })

  it('ignores non-script assets', () => {
    // The favicon is a `<link href=…>` like a modulepreload is. Were it counted, every
    // comparison would still work by luck — until the favicon changed and a tab reloaded
    // itself over an icon.
    expect(parseServedEntries(servedHtml('a')).some((u) => u.includes('favicon'))).toBe(false)
  })

  it('finds nothing in the dev server document', () => {
    // `npm run dev` serves an unhashed TSX entry, so there is nothing to fingerprint. The
    // empty result is what keeps `entriesDiffer` from calling dev an update.
    const dev = '<script type="module" src="/src/main.tsx"></script>'
    expect(parseServedEntries(dev)).toEqual([])
  })
})

describe('entriesDiffer', () => {
  const boot = parseServedEntries(servedHtml('DGxu44bO'))

  it('is false for the build this tab is running', () => {
    expect(entriesDiffer(boot, parseServedEntries(servedHtml('DGxu44bO')))).toBe(false)
  })

  it('is true when the entry hash moved', () => {
    expect(entriesDiffer(boot, parseServedEntries(servedHtml('Zq1111aa')))).toBe(true)
  })

  it('is true when only a vendor chunk moved', () => {
    // Rollup propagates a chunk hash up to its importers, so in practice the entry moves
    // too — but the fingerprint is the whole list precisely so it does not depend on that.
    expect(entriesDiffer(boot, parseServedEntries(servedHtml('DGxu44bO', 'ffff9999')))).toBe(true)
  })

  it('says "no" rather than "different" when either side is unknown', () => {
    // A tab must never reload itself over a question that could not be answered: an empty
    // side means dev mode, or a probe that came back without scripts.
    expect(entriesDiffer([], boot)).toBe(false)
    expect(entriesDiffer(boot, [])).toBe(false)
  })
})

describe('reload cooldown', () => {
  let store: Record<string, string>

  beforeEach(() => {
    store = {}
    // Node has no sessionStorage; only the two methods this module uses are needed.
    ;(globalThis as any).sessionStorage = {
      getItem: (k: string) => store[k] ?? null,
      setItem: (k: string, v: string) => { store[k] = v },
    }
  })

  afterEach(() => { delete (globalThis as any).sessionStorage })

  it('allows the first reload', () => {
    expect(reloadAllowed(1_000_000)).toBe(true)
  })

  it('refuses a second one straight after', () => {
    // The mid-roll loop: two replicas serving two builds keep answering "updated".
    markReloading(1_000_000)
    expect(reloadAllowed(1_005_000)).toBe(false)
  })

  it('allows one again once the cooldown has passed', () => {
    markReloading(1_000_000)
    expect(reloadAllowed(1_000_000 + 60_000)).toBe(true)
  })

  it('reloads anyway when storage is unavailable', () => {
    // Private mode throws on access. The guard is a nicety; the reload is the feature.
    ;(globalThis as any).sessionStorage = {
      getItem() { throw new Error('denied') },
      setItem() { throw new Error('denied') },
    }
    expect(() => markReloading()).not.toThrow()
    expect(reloadAllowed()).toBe(true)
  })
})
