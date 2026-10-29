// The rule of this directory (README.md): its files import only `three` and each other, so the
// viewer core can be taken out of this app as it is. The palette, the service client and every
// other host concern are handed in by the host.
import { readdirSync, readFileSync } from 'node:fs'
import { fileURLToPath, URL } from 'node:url'
import { describe, expect, it } from 'vitest'

const DIR = fileURLToPath(new URL('.', import.meta.url))
const ALLOWED = /^(three|three\/addons\/.+|\.\/[\w.-]+)$/

describe('scene3d imports', () => {
  it('names only three and its siblings', () => {
    const offending: string[] = []
    for (const file of readdirSync(DIR).filter((f) => /\.ts$/.test(f) && !f.endsWith('.test.ts'))) {
      const source = readFileSync(`${DIR}/${file}`, 'utf8')
      for (const [, from] of source.matchAll(/^\s*(?:import|export)\b[^'"]*?from\s+['"]([^'"]+)['"]/gm)) {
        if (!ALLOWED.test(from)) offending.push(`${file}: ${from}`)
      }
    }
    expect(offending).toEqual([])
  })
})
