// A descriptor of another format or a newer version is refused by name; an unstamped one loads.
import { afterEach, describe, expect, it, vi } from 'vitest'

import { SCENE_FORMAT, SCENE_VERSION, checkSceneFormat, loadScene } from './sceneLoader'

const URL_ = '/campaigns/c/scene_assets/k/scene.json'

describe('checkSceneFormat', () => {
  it('accepts an unstamped descriptor', () => {
    expect(() => checkSceneFormat({}, URL_)).not.toThrow()
  })

  it('accepts the format and version it reads', () => {
    expect(() => checkSceneFormat({ format: SCENE_FORMAT, version: SCENE_VERSION }, URL_)).not.toThrow()
  })

  it('refuses a newer version, naming both', () => {
    expect(() => checkSceneFormat({ format: SCENE_FORMAT, version: SCENE_VERSION + 1 }, URL_)).toThrow(
      new RegExp(`version ${SCENE_VERSION + 1}.*up to ${SCENE_VERSION}`),
    )
  })

  it('refuses another format, naming both', () => {
    expect(() => checkSceneFormat({ format: 'roqsim_scenes.scene_manifest', version: 1 }, URL_)).toThrow(
      /roqsim_scenes\.scene_manifest.*roqsim\.web_scene/,
    )
  })

  it('refuses a version that is not a positive integer', () => {
    for (const version of [0, 1.5, '1', true]) {
      expect(() => checkSceneFormat({ format: SCENE_FORMAT, version }, URL_)).toThrow(/version/)
    }
  })
})

describe('loadScene', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('refuses another format before it needs scene.bin', async () => {
    vi.stubGlobal('window', { location: { href: 'http://example.test/' } })
    vi.stubGlobal('fetch', vi.fn(async (url: string) =>
      url.endsWith('scene.json')
        ? { ok: true, json: async () => ({ format: 'roqsim_scenes.scene_manifest', version: 1 }) }
        : { ok: false, status: 404, statusText: 'Not Found' },
    ))
    await expect(loadScene(URL_)).rejects.toThrow(/roqsim_scenes\.scene_manifest.*roqsim\.web_scene/)
  })
})
