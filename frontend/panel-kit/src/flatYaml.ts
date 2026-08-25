// A tiny YAML reader for the flat metadata sidecars panels have to read in the browser, and the one
// rule that makes reading them safe.
//
// Deliberately not a YAML parser. A remote panel bundles what it imports, and pulling a real parser in
// to read half a dozen keys costs more than the panel does. What it covers is the shape these sidecars
// actually have: a flat top-level mapping of scalars, plus sequences written either inline or as a
// block. Nothing here knows what the keys mean, so a consumer of any map format — nav's `map.yaml`, a
// simulator's own extent file, a hand-written one — reads it the same way and applies its own meaning
// on top (see mapYaml.ts for that layer).

/** What a flat sidecar's values can be, once read. */
export type YamlScalar = number | string

/** Read one scalar: a number where it is one, otherwise the string with any quotes removed. */
function scalar(text: string): YamlScalar {
  const n = Number(text)
  return text !== '' && !Number.isNaN(n) ? n : text.replace(/^["']|["']$/g, '')
}

/** Read a flat mapping: top-level `key: value` pairs, where a value is a scalar or a sequence.
 *
 *  Both sequence spellings are accepted, because both are in use and a reader that takes only one of
 *  them silently loses the other:
 *
 *      origin: [-7.2, -7.75, 0]        # inline — what a hand-written sidecar declares
 *
 *      origin:                         # block — what a generator writes
 *      - -7.2
 *      - -7.75
 *      - 0
 *
 *  Nested mappings are skipped rather than flattened: a generator's `metadata:` block is not the
 *  file's own vocabulary, and flattening it lets a nested `resolution:` shadow the real one. A key
 *  that opens a block yields an empty array when nothing follows it, so an absent key and an empty
 *  one stay distinguishable. */
export function parseFlatYaml(text: string): Record<string, YamlScalar | YamlScalar[]> {
  const out: Record<string, YamlScalar | YamlScalar[]> = {}
  // The top-level key a block sequence's items belong to, or null when no block is open.
  let block: string | null = null
  for (const rawLine of text.split('\n')) {
    const stripped = rawLine.split('#')[0]
    const line = stripped.trim()
    if (!line) continue
    const indent = stripped.length - stripped.trimStart().length

    if (line === '-' || line.startsWith('- ')) {
      if (block) (out[block] as YamlScalar[]).push(scalar(line.slice(1).trim()))
      continue
    }
    if (indent > 0 || !line.includes(':')) continue

    const [key, ...rest] = line.split(':')
    const name = key.trim()
    const value = rest.join(':').trim()
    if (!value) {
      // `key:` alone opens a block sequence or a nested mapping, and which one is only known from the
      // next line. Open an array either way: a nested mapping never pushes to it.
      out[name] = []
      block = name
      continue
    }
    block = null
    out[name] = value.startsWith('[')
      ? value
          .replace(/[[\]]/g, '')
          .split(',')
          .map((v) => scalar(v.trim()))
      : scalar(value)
  }
  return out
}

/** Read a declared sequence of numbers — an origin, an extent, a size — of one of `lengths`.
 *
 *  `fallback` is returned only when the key is ABSENT, and its absence is then the caller's documented
 *  default. A key that is PRESENT but unreadable throws instead, and that asymmetry is the point: once
 *  a caller holds a plain array of numbers it cannot tell a default apart from a value that was
 *  declared and misread. A map origin defaulted that way put a whole grid in the wrong place while
 *  every marker on it stayed right, which looked like a rotation rather than like a bug. */
export function numberSequence(
  raw: unknown,
  name: string,
  lengths: number[],
  fallback?: number[],
): number[] {
  if (raw === undefined && fallback) return fallback
  if (
    Array.isArray(raw) &&
    lengths.includes(raw.length) &&
    raw.every((v) => typeof v === 'number' && Number.isFinite(v))
  ) {
    return raw as number[]
  }
  const wanted = lengths.join(' or ')
  throw new Error(
    `\`${name}\` is not a sequence of ${wanted} numbers: ${JSON.stringify(raw)}. ` +
      'Write it inline as [a, b] or as a block sequence of `- ` items.',
  )
}
