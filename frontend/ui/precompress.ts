// Build-time compression of the emitted bundle: a `.br` and a `.gz` beside every file worth
// compressing, so the service hands a browser compressed bytes without compressing anything
// per request (`_CachedUi` in src/robovast/service/app.py picks the sibling the request
// accepts). Done once here at the strongest settings, since the cost is paid per build
// rather than per download.
import { readdirSync, readFileSync, writeFileSync } from 'node:fs'
import { extname, join } from 'node:path'
import { brotliCompressSync, constants, gzipSync } from 'node:zlib'
import type { Plugin } from 'vite'

// Text formats, plus the fonts and wasm Monaco and the workers ship. Images and anything
// already compressed are left alone: a second pass only costs the browser a decode.
export const COMPRESSIBLE = new Set([
  '.js', '.mjs', '.css', '.html', '.svg', '.json', '.map', '.txt', '.ttf', '.wasm',
])

// Below this a compressed response saves less than its headers and a round of decoding.
export const MIN_BYTES = 1024

// Suffix and encoder per content coding; the service prefers them in this order.
export const ENCODINGS: ReadonlyArray<[string, (data: Buffer) => Buffer]> = [
  ['.br', (data) => brotliCompressSync(data, {
    params: {
      [constants.BROTLI_PARAM_QUALITY]: constants.BROTLI_MAX_QUALITY,
      [constants.BROTLI_PARAM_SIZE_HINT]: data.length,
    },
  })],
  ['.gz', (data) => gzipSync(data, { level: constants.Z_BEST_COMPRESSION })],
]

function* files(dir: string): Generator<string> {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name)
    if (entry.isDirectory()) yield* files(path)
    else if (entry.isFile()) yield path
  }
}

// Writes the siblings for every compressible file under `dir` and returns their paths. A
// sibling that would not be smaller than its original is not written, so a request that
// accepts the coding is then served the original.
export function precompressDir(dir: string): string[] {
  const written: string[] = []
  for (const path of [...files(dir)]) {
    if (!COMPRESSIBLE.has(extname(path))) continue
    const data = readFileSync(path)
    if (data.length < MIN_BYTES) continue
    for (const [suffix, encode] of ENCODINGS) {
      const encoded = encode(data)
      if (encoded.length >= data.length) continue
      writeFileSync(path + suffix, encoded)
      written.push(path + suffix)
    }
  }
  return written
}

// Runs after the bundle (and the copied public directory) is on disk.
export function precompress(): Plugin {
  return {
    name: 'robovast-precompress',
    apply: 'build',
    writeBundle: {
      order: 'post',
      sequential: true,
      handler(options) {
        if (!options.dir) throw new Error('precompress: the build has no output directory')
        precompressDir(options.dir)
      },
    },
  }
}
