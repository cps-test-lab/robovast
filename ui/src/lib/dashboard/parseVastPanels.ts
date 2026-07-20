// Normalize the raw panel dicts from GET /campaigns/{id}/panels (the vast's visualization.panels) into
// PanelSpecs: lift the known layout fields, merge each panel type's manifest defaults, and keep every
// remaining key (the data bindings) verbatim in `config`. Unknown types are kept as-is so the PanelHost
// can render an explicit "unknown panel type" error rather than silently dropping the panel.

import { getPanel } from './registry'
import type { PanelSpec } from './types'

const KNOWN_KEYS = new Set([
  'type',
  'title',
  'position',
  'resizable',
  'minimizable',
  'minimized',
  'frameless',
  'hidden',
  'fixed',
])

export function parseVastPanels(raw: Record<string, unknown>[]): PanelSpec[] {
  return raw.map((r) => {
    const type = String(r.type ?? '')
    const manifest = getPanel(type)?.manifest
    const pos = (r.position ?? {}) as Record<string, unknown>

    const config: Record<string, unknown> = {}
    for (const [k, v] of Object.entries(r)) {
      if (!KNOWN_KEYS.has(k)) config[k] = v
    }

    return {
      type,
      // Only an explicitly-declared title; a bare type shows no header (the header text itself
      // falls back to the manifest label in PanelHost when a header is shown).
      title: r.title != null ? String(r.title) : undefined,
      position: {
        anchor: (pos.anchor as PanelSpec['position']['anchor']) ?? manifest?.defaultPosition.anchor,
        width: (pos.width as number | string | undefined) ?? manifest?.defaultPosition.width,
        height: (pos.height as number | string | undefined) ?? manifest?.defaultPosition.height,
      },
      resizable: (r.resizable as boolean | undefined) ?? manifest?.resizable ?? false,
      minimizable: (r.minimizable as boolean | undefined) ?? manifest?.minimizable ?? false,
      minimized: (r.minimized as boolean | undefined) ?? false,
      frameless: (r.frameless as boolean | undefined) ?? manifest?.frameless ?? false,
      hidden: (r.hidden as boolean | undefined) ?? false,
      fixed: (r.fixed as boolean | undefined) ?? false,
      config,
    }
  })
}
