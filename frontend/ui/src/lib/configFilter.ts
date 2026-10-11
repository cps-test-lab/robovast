// A campaign's config filter as the service reads it (`filter_configs_by_name`): comma-separated
// globs, a configuration selected when any of them matches its name. Matching follows Python's
// `fnmatch.fnmatchcase` — case-sensitive, `*`, `?`, `[seq]` and `[!seq]` — so what the launcher
// says a filter selects is what the campaign runs.

export function filterPatterns(filter: string): string[] {
  return filter
    .split(',')
    .map((p) => p.trim())
    .filter(Boolean)
}

export function isGlob(pattern: string): boolean {
  return /[*?[]/.test(pattern)
}

const escape = (s: string) => s.replace(/[\\^$.*+?()[\]{}|/-]/g, '\\$&')

// `fnmatch.translate`, anchored: an unclosed `[` is a literal, as it is there.
export function globToRegExp(glob: string): RegExp {
  let out = ''
  for (let i = 0; i < glob.length; i++) {
    const c = glob[i]
    if (c === '*') out += '.*'
    else if (c === '?') out += '.'
    else if (c === '[') {
      let j = i + 1
      if (glob[j] === '!') j++
      if (glob[j] === ']') j++
      while (j < glob.length && glob[j] !== ']') j++
      if (j >= glob.length) {
        out += '\\['
        continue
      }
      let body = glob.slice(i + 1, j).replace(/\\/g, '\\\\')
      if (body.startsWith('!')) body = '^' + body.slice(1)
      else if (body.startsWith('^')) body = '\\' + body
      out += `[${body}]`
      i = j
    } else out += escape(c)
  }
  return new RegExp(`^${out}$`, 's')
}

export function matchesPattern(name: string, pattern: string): boolean {
  return globToRegExp(pattern).test(name)
}

/** The names `filter` selects; every name for an empty filter. */
export function matchConfigs(names: string[], filter: string): string[] {
  const patterns = filterPatterns(filter).map(globToRegExp)
  if (!patterns.length) return names
  return names.filter((n) => patterns.some((re) => re.test(n)))
}
