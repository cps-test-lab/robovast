// Panel registries: panel plugins self-register here (by importing a panels index), and a host looks
// them up by the `type` a vast spec names. Adding a panel is one register call -- the layout engine,
// the clock, and the data provider need no change.
//
// Two surfaces, one implementation. A run-view panel and a config-view panel take different props (a
// playback clock and a run-scoped data provider versus a resolved configuration), so they cannot
// share a registry -- but nothing about *registration* differs, so the map is a factory used twice
// rather than a file copied twice.

import type { ComponentType } from 'react'
import type { ConfigPanelProps, PanelProps } from '@robovast/panel-kit'
import type { ConfigPanelPlugin, PanelPlugin } from './types'

export interface PanelRegistry<P> {
  register: (plugin: { manifest: { type: string }; component: ComponentType<P> }) => void
  get: (type: string) => { manifest: { type: string }; component: ComponentType<P> } | undefined
}

function createPanelRegistry<P>(): PanelRegistry<P> {
  const registry = new Map<string, { manifest: { type: string }; component: ComponentType<P> }>()
  return {
    register: (plugin) => {
      registry.set(plugin.manifest.type, plugin)
    },
    get: (type) => registry.get(type),
  }
}

const runPanels = createPanelRegistry<PanelProps>()
const configPanels = createPanelRegistry<ConfigPanelProps>()

/** Register a run-view panel (Results → Run view). */
export function registerPanel(plugin: PanelPlugin): void {
  runPanels.register(plugin)
}

export function getPanel(type: string): PanelPlugin | undefined {
  return runPanels.get(type) as PanelPlugin | undefined
}

/** Register a config-view panel (the Config tab's third column). */
export function registerConfigPanel(plugin: ConfigPanelPlugin): void {
  configPanels.register(plugin)
}

export function getConfigPanel(type: string): ConfigPanelPlugin | undefined {
  return configPanels.get(type) as ConfigPanelPlugin | undefined
}
