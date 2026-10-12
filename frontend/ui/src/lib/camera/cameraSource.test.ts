import { describe, expect, it, vi } from 'vitest'

import type { DataProvider, PanelSpec } from '@robovast/panel-kit'
import { cameraKind, cameraTopic, resolveCameraSource, sceneLook } from './cameraSource'

const spec = (config: Record<string, unknown>): PanelSpec =>
  ({ type: 'camera', config }) as unknown as PanelSpec

const provider = (over: Partial<DataProvider> = {}): DataProvider =>
  ({
    scope: 'c:cfg:0',
    campaignId: 'c',
    configName: 'cfg',
    runId: '0',
    has: vi.fn(async () => true),
    series: vi.fn(async () => [{ file: 'cam.webm', t_start: '3.5', fps: 25, topic: '/cam' }]),
    runFileUrl: (p: string) => `/results/c/cfg/0/${p}`,
    ...over,
  }) as unknown as DataProvider

describe('cameraKind', () => {
  it('is recorded unless the config says', () => {
    expect(cameraKind({})).toBe('recorded')
    expect(cameraKind({ kind: 'topic' })).toBe('topic')
    expect(cameraKind({ kind: 'render' })).toBe('render')
    expect(cameraKind({ kind: 'scene' })).toBe('scene')
  })

  it('is scene for a camera or a view without a kind, and render only when asked', () => {
    expect(cameraKind({ camera: 'robot.cam' })).toBe('scene')
    expect(cameraKind({ view: { position: [0, 0, 1], look_at: [1, 0, 0] } })).toBe('scene')
    expect(cameraKind({ kind: 'render', camera: 'top' })).toBe('render')
    expect(cameraKind({ kind: 'render', view: { azimuth: 90 } })).toBe('render')
    expect(cameraKind({ source: { topic: '/cam' } })).toBe('recorded')
  })

  it('refuses a kind it does not have', () => {
    expect(() => cameraKind({ kind: 'webrtc' as never })).toThrow(/webrtc/)
  })
})

describe('cameraTopic', () => {
  it('reads topic, or source.topic, and refuses neither', () => {
    expect(cameraTopic({ topic: '/a' })).toBe('/a')
    expect(cameraTopic({ source: { topic: '/b' } })).toBe('/b')
    expect(() => cameraTopic({})).toThrow(/topic/)
  })
})

describe('sceneLook', () => {
  it('reads a camera by name, or a free view with its defaults', () => {
    expect(sceneLook({ camera: 'robot.cam' })).toEqual({ camera: 'robot.cam' })
    expect(sceneLook({ view: { position: [0, 0, 2], look_at: [3, 0, 0], attach: 'base_link' } }))
      .toEqual({
        view: {
          position: [0, 0, 2], look_at: [3, 0, 0], attach: 'base_link', fov: 60, up: [0, 0, 1],
          near: 0.05, far: 1000,
        },
      })
  })

  it('refuses neither, both, and a server render view by name', () => {
    expect(() => sceneLook({ kind: 'scene' })).toThrow(/camera.*view/)
    expect(() => sceneLook({ camera: 'a', view: { position: [0, 0, 1], look_at: [1, 0, 0] } }))
      .toThrow(/not both/)
    expect(() => sceneLook({ view: { azimuth: 90, distance: 12 } })).toThrow(/kind: render/)
  })
})

describe('resolveCameraSource', () => {
  it('recorded: places the registered video by its videos row', async () => {
    const src = await resolveCameraSource(spec({}), provider())
    expect(src).toEqual({
      kind: 'recorded', url: '/results/c/cfg/0/cam.webm', t0: 3.5, t1: undefined, fps: 25,
      topic: '/cam',
    })
  })

  it('recorded: is null without a videos table', async () => {
    expect(await resolveCameraSource(spec({}), provider({ has: async () => false }))).toBeNull()
  })

  it('recorded: refuses a path with no t0', async () => {
    await expect(
      resolveCameraSource(spec({ source: { path: 'x.webm' } }), provider()),
    ).rejects.toThrow(/t0/)
  })

  it('topic: reads the frame index through the index route', async () => {
    const fetchJson = vi.fn(async () => ({ topic: '/cam', times: [2, 1] }))
    const src = await resolveCameraSource(spec({ kind: 'topic', topic: '/cam' }), provider(), fetchJson)
    expect(fetchJson).toHaveBeenCalledWith(
      '/data/campaigns/c/frame-index?run=cfg%2F0&topic=%2Fcam',
    )
    expect(src).toEqual({
      kind: 'topic', topic: '/cam', run: { campaignId: 'c', configName: 'cfg', runId: '0' },
      times: [1, 2],
    })
  })

  it('topic: is null when the run recorded no such topic (404)', async () => {
    const src = await resolveCameraSource(
      spec({ kind: 'topic', topic: '/cam' }), provider(), async () => null,
    )
    expect(src).toBeNull()
  })

  it('render: carries the viewpoint and needs no request', async () => {
    const fetchJson = vi.fn()
    const src = await resolveCameraSource(
      spec({ kind: 'render', camera: 'top', view: { distance: 4 } }), provider(), fetchJson,
    )
    expect(fetchJson).not.toHaveBeenCalled()
    expect(src).toEqual({
      kind: 'render', run: { campaignId: 'c', configName: 'cfg', runId: '0' },
      view: { camera: 'top', view: { distance: 4 } },
    })
  })

  it('scene: names the camera, asks the service nothing and reads no table', async () => {
    const fetchJson = vi.fn()
    const data = provider()
    const src = await resolveCameraSource(spec({ camera: 'robot.oakd.oakd_camera' }), data, fetchJson)
    expect(src).toEqual({ kind: 'scene', look: { camera: 'robot.oakd.oakd_camera' } })
    expect(fetchJson).not.toHaveBeenCalled()
    expect(data.has).not.toHaveBeenCalled()
    expect(data.series).not.toHaveBeenCalled()
  })

  it('scene: refuses a panel with neither camera nor view', async () => {
    await expect(resolveCameraSource(spec({ kind: 'scene' }), provider())).rejects.toThrow(/camera/)
  })
})
