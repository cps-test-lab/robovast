// Resolving a panel field's binding: where a value comes from, separately from what it means.
//
// A `.vast` binds a panel's field to one of four sources — a literal, a scenario parameter, a
// variation's internal, or a contributed file role — and every field of every panel reads the same
// way, so a panel added later needs no parsing of its own and a field added later needs no syntax.
// The Python side of the same grammar is robovast.common.panel_bindings.Binding, which is what
// validates a declaration before it ever reaches here.

import type { ResolvedConfiguration } from './configPanel'

/** The explicit forms; anything else is the literal itself. */
export interface BindingSource {
  literal?: unknown
  /** A resolved scenario parameter — the value follows the selected configuration. */
  param?: string
  /** An `_`-prefixed key a variation left on the configuration (`_path`, `_map_file`). */
  internal?: string
  /** A named entry of the configuration's contributed `files` (`map`). */
  role?: string
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return !!v && typeof v === 'object' && !Array.isArray(v)
}

/**
 * The value a binding names, or `undefined` when the configuration does not have it.
 *
 * Absent rather than defaulted, always: a field silently filled with something plausible is a wrong
 * answer that looks right, where an empty one is a visible question. `map2d`'s "no map for this
 * configuration" note exists for exactly this case.
 */
export function resolveBinding(binding: unknown, config: ResolvedConfiguration): unknown {
  if (binding == null) return undefined
  if (!isRecord(binding)) return binding // a bare scalar or list is the literal
  const source = binding as BindingSource
  if (source.param != null) return config.parameters?.[source.param]
  if (source.internal != null) return config.internals?.[source.internal]
  if (source.role != null) return config.contribution?.files?.[source.role]
  if ('literal' in source) return source.literal
  return binding // a mapping that names no source is itself the literal (a pose, a spec)
}

/** `resolveBinding` narrowed to a string field — a path, a table, a topic. */
export function resolveStringBinding(
  binding: unknown,
  config: ResolvedConfiguration,
): string | undefined {
  const value = resolveBinding(binding, config)
  return typeof value === 'string' && value ? value : undefined
}
