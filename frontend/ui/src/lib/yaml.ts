// Rendering a resolved configuration back as YAML.
//
// YAML and not JSON because the .vast is YAML: a value read in the config view is usually on its
// way back into that file, and a reader should not have to translate braces and quotes into
// indentation to compare the two.

import { dump } from 'js-yaml'

/** A configuration fragment as YAML, or a readable note when it is empty or unserializable. */
export function toYaml(value: unknown): string {
  if (value == null || (typeof value === 'object' && !Object.keys(value as object).length)) {
    return '(none)'
  }
  try {
    // `noRefs` because a configuration that repeats a value -- the same pose as a start and as a
    // goal -- would otherwise render as a YAML anchor/alias pair, which is correct YAML and
    // useless to read. `lineWidth: -1` keeps a long path on one line rather than folding it.
    return dump(value, { noRefs: true, lineWidth: -1, sortKeys: false })
  } catch (err) {
    return `(could not render: ${err instanceof Error ? err.message : String(err)})`
  }
}
