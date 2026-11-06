// How a compact screen reads a run view's declared panels.
import { describe, expect, it } from 'vitest'
import { showsHeader, stackPanels } from './stackedLayout'
import type { PanelSpec } from './types'

function panel(type: string, over: Partial<PanelSpec> = {}): PanelSpec {
  return {
    type,
    config: {},
    position: {},
    resizable: true,
    minimizable: true,
    minimized: false,
    frameless: false,
    hidden: false,
    fixed: false,
    ...over,
  }
}

const scene = panel('scene3d', { position: { fill: true }, frameless: true, minimizable: false })
const playback = panel('playback', { position: { anchor: 'bottom', height: 40 }, minimizable: false })
const camera = panel('camera', { position: { anchor: 'top-right' }, title: 'Front camera' })
const plot = panel('timeseries', { position: { anchor: 'bottom-left' }, title: 'Speed' })
const log = panel('log', { position: { anchor: 'bottom' }, title: 'Run log' })

describe('stackPanels', () => {
  it('puts the filling panel on the stage, header-less edge strips on their edge, the rest in tabs', () => {
    const out = stackPanels([scene, camera, plot, log, playback])
    expect(out.stage).toBe(scene)
    expect(out.bottom).toEqual([playback])
    expect(out.top).toEqual([])
    // Declaration order, and a docked panel WITH a header (the log) is a tab, not a bar.
    expect(out.tabs).toEqual([camera, plot, log])
  })

  it('leaves out hidden panels', () => {
    const out = stackPanels([scene, { ...camera, hidden: true }, plot])
    expect(out.tabs).toEqual([plot])
  })

  it('has no stage when nothing fills, and takes only the first filling panel as it', () => {
    expect(stackPanels([camera, plot]).stage).toBeNull()
    const second = panel('scene', { position: { fill: true }, title: 'Map' })
    const out = stackPanels([scene, second])
    expect(out.stage).toBe(scene)
    expect(out.tabs).toEqual([second])
  })
})

describe('showsHeader', () => {
  it('is a title or a minimize toggle, and never on a frameless panel', () => {
    expect(showsHeader(camera)).toBe(true)
    expect(showsHeader(playback)).toBe(false)
    expect(showsHeader(scene)).toBe(false)
    expect(showsHeader({ ...camera, frameless: true })).toBe(false)
  })
})
