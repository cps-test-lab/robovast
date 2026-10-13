import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { brotliDecompressSync, gunzipSync } from 'node:zlib'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { MIN_BYTES, precompressDir } from './precompress'

describe('precompressDir', () => {
  let dir: string
  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), 'precompress-'))
    mkdirSync(join(dir, 'assets'))
  })
  afterEach(() => rmSync(dir, { recursive: true, force: true }))

  it('writes a .br and a .gz that decode to the original', () => {
    const js = Buffer.from('export const x = "a repetitive chunk";\n'.repeat(200))
    writeFileSync(join(dir, 'assets', 'index-AAAA.js'), js)

    precompressDir(dir)

    const base = join(dir, 'assets', 'index-AAAA.js')
    expect(brotliDecompressSync(readFileSync(`${base}.br`))).toEqual(js)
    expect(gunzipSync(readFileSync(`${base}.gz`))).toEqual(js)
  })

  it('leaves small files, images and incompressible bytes alone', () => {
    writeFileSync(join(dir, 'index.html'), 'x'.repeat(MIN_BYTES - 1))
    writeFileSync(join(dir, 'assets', 'tex.png'), Buffer.alloc(8192))
    // Random bytes do not shrink: neither coding may ship a sibling larger than the original.
    const noise = Buffer.from(Array.from({ length: 8192 }, () => Math.floor(Math.random() * 256)))
    writeFileSync(join(dir, 'assets', 'noise.wasm'), noise)

    expect(precompressDir(dir)).toEqual([])
    for (const name of ['index.html', 'assets/tex.png', 'assets/noise.wasm']) {
      expect(existsSync(join(dir, `${name}.gz`))).toBe(false)
      expect(existsSync(join(dir, `${name}.br`))).toBe(false)
    }
  })
})
