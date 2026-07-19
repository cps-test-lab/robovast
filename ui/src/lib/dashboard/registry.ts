// The panel registry: panel plugins self-register here (by importing the panels index), and the
// PanelHost looks them up by the `type` a vast spec names. Adding a panel is one registerPanel call --
// the layout engine, the clock, and the data provider need no change.

import type { PanelPlugin } from './types'

const registry = new Map<string, PanelPlugin>()

export function registerPanel(plugin: PanelPlugin): void {
  registry.set(plugin.manifest.type, plugin)
}

export function getPanel(type: string): PanelPlugin | undefined {
  return registry.get(type)
}
