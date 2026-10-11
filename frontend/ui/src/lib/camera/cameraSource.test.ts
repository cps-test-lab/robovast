import { describe, expect, it, vi } from 'vitest'

import type { DataProvider, PanelSpec } from '@robovast/panel-kit'
import { cameraKind, cameraTopic, resolveCameraSource } from './cameraSource'

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
})
