// Normalize the raw panel dicts the service serves (from the vast's `visualization` block) into
// specs: lift the known fields, merge each panel type's manifest defaults, and keep every remaining
// key (the data bindings) verbatim in `config`. Unknown types are kept as-is so the host can render
// an explicit "unknown panel type" error rather than silently dropping the panel.
//
// Two surfaces with two layout grammars -- the run view's anchors and the config view's single
// column -- so there are two functions. What they share is what actually matters and is written
// once: which keys are plumbing, and that everything else is the panel's own binding.

import { getConfigPanel, getPanel } from './registry'
import type { ConfigPanelSpec, PanelSpec } from './types'
import type { RemoteDescriptor } from '@/lib/remote'

/** A well-formed service-attached MF descriptor (name + entry url + module). */
function asRemote(v: unknown): RemoteDescriptor | undefined {
  if (v && typeof v === 'object') {
    const r = v as Record<string, unknown>
    if (typeof r.name === 'string' && typeof r.remote_entry_url === 'string' && typeof r.module === 'string') {
      return { name: r.name, remote_entry_url: r.remote_entry_url, module: r.module }
    }
  }
  return undefined
}

/** Keys every surface treats as plumbing rather than as one panel's data binding. */
const COMMON_KEYS = ['type', 'title', 'hidden', 'remote', 'module'] as const

const KNOWN_KEYS = new Set([
  ...COMMON_KEYS,
  'position',
  'resizable',
  'minimizable',
  'minimized',
  'frameless',
  'fixed',
])

/** The config view is one column, so a panel's only layout field is its height. */
const CONFIG_KNOWN_KEYS = new Set([...COMMON_KEYS, 'height'])

/** Everything that is not plumbing, kept verbatim for the panel to interpret. */
function bindings(raw: Record<string, unknown>, known: Set<string>): Record<string, unknown> {
  const config: Record<string, unknown> = {}
  for (const [k, v] of Object.entries(raw)) {
    if (!known.has(k)) config[k] = v
  }
  return config
}

/** Config-view panels: type, title, a height, and the panel's own bindings. */
export function parseConfigPanels(raw: Record<string, unknown>[]): ConfigPanelSpec[] {
  return raw.map((r) => {
    const type = String(r.type ?? '')
    const manifest = getConfigPanel(type)?.manifest
    return {
      type,
      title: r.title != null ? String(r.title) : undefined,
      // No height means "take what the panels above left over"; only the last panel may omit
      // it, which the .vast schema is what enforces.
      height: (r.height as number | string | undefined) ?? manifest?.defaultHeight,
      hidden: (r.hidden as boolean | undefined) ?? false,
      remote: asRemote(r.remote),
      config: bindings(r, CONFIG_KNOWN_KEYS),
    }
  })
}

export function parsePanels(raw: Record<string, unknown>[]): PanelSpec[] {
  return raw.map((r) => {
    const type = String(r.type ?? '')
    const manifest = getPanel(type)?.manifest
    const pos = (r.position ?? {}) as Record<string, unknown>
    const config = bindings(r, KNOWN_KEYS)

    return {
      type,
      // Only an explicitly-declared title; a bare type shows no header (the header text itself
      // falls back to the manifest label in PanelHost when a header is shown).
      title: r.title != null ? String(r.title) : undefined,
      position: {
        anchor: (pos.anchor as PanelSpec['position']['anchor']) ?? manifest?.defaultPosition.anchor,
        width: (pos.width as number | string | undefined) ?? manifest?.defaultPosition.width,
        height: (pos.height as number | string | undefined) ?? manifest?.defaultPosition.height,
        fill: (pos.fill as boolean | undefined) ?? manifest?.defaultPosition.fill,
      },
      // Resizable unless the panel type or the author says otherwise. Opting in per panel made the
      // one panel that ships no host manifest -- every package-provided remote, the costmap among
      // them -- the only one in the view that could not be resized, for no reason its author could
      // see from the .vast.
      resizable: (r.resizable as boolean | undefined) ?? manifest?.resizable ?? true,
      minimizable: (r.minimizable as boolean | undefined) ?? manifest?.minimizable ?? false,
      minimized: (r.minimized as boolean | undefined) ?? false,
      frameless: (r.frameless as boolean | undefined) ?? manifest?.frameless ?? false,
      hidden: (r.hidden as boolean | undefined) ?? false,
      fixed: (r.fixed as boolean | undefined) ?? false,
      remote: asRemote(r.remote),
      config,
    }
  })
}
