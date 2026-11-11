// How a run view's panels are arranged on a compact screen, where the declared layout cannot be.
//
// The `.vast` lays panels out for a desktop: a scene filling the view, docks along its edges, panels
// in its corners. On a phone those all land on top of each other and over the scene. So a compact
// screen reads the same declarations differently, and the `.vast` does not change:
//
// - the **stage** is the panel that fills the view (the 3D scene, typically), shown on its own;
// - the **bars** are the strips docked along the top or bottom edge that carry no header (the
//   playback bar), kept on that edge at full width;
// - every other panel is a **tab**, one shown at a time beside or below the stage, in declaration
//   order.
//
// Pure, so the reading is tested on its own; PanelHost draws it.

import type { PanelSpec } from './types'

export interface StackedLayout {
  stage: PanelSpec | null
  top: PanelSpec[]
  bottom: PanelSpec[]
  tabs: PanelSpec[]
}

/** Whether a panel shows a header row: a title to show or a minimize toggle to carry. A frameless
 *  panel never does. */
export function showsHeader(p: PanelSpec): boolean {
  return !p.frameless && (p.minimizable || !!p.title)
}

export function stackPanels(panels: PanelSpec[]): StackedLayout {
  const out: StackedLayout = { stage: null, top: [], bottom: [], tabs: [] }
  for (const p of panels) {
    if (p.hidden) continue
    if (p.position.fill && !out.stage) {
      out.stage = p
    } else if (!showsHeader(p) && (p.position.anchor === 'top' || p.position.anchor === 'bottom')) {
      out[p.position.anchor].push(p)
    } else {
      out.tabs.push(p)
    }
  }
  return out
}
