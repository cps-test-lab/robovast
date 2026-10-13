// A scene states the contract of its extras; one of another contract or a newer version is refused
// by name, and so is a glTF file that states none.
import { afterEach, describe, expect, it, vi } from 'vitest'

import { fetchOf, gltfScene } from './gltfFixture'
import { SCENE_FORMAT, SCENE_VERSION, checkSceneFormat, loadScene } from './sceneLoader'

const URL_ = '/campaigns/c/scene_assets/k/scene.glb'

describe('checkSceneFormat', () => {
  it('accepts the format and version it reads', () => {
    expect(() => checkSceneFormat({ format: SCENE_FORMAT, version: SCENE_VERSION }, URL_)).not.toThrow()
  })

  it('refuses a scene that states no contract', () => {
    expect(() => checkSceneFormat({}, URL_)).toThrow(/no scene contract.*roqsim\.scene/)
  })

  it('refuses a newer version, naming both', () => {
    expect(() => checkSceneFormat({ format: SCENE_FORMAT, version: SCENE_VERSION + 1 }, URL_)).toThrow(
      new RegExp(`version ${SCENE_VERSION + 1}.*up to ${SCENE_VERSION}`),
    )
  })

  it('refuses another format, naming both', () => {
    expect(() => checkSceneFormat({ format: 'roqsim.web_scene', version: 1 }, URL_)).toThrow(
      /roqsim\.web_scene.*roqsim\.scene/,
    )
  })

  it('refuses a version that is not a positive integer', () => {
    for (const version of [undefined, 0, 1.5, '1', true]) {
      expect(() => checkSceneFormat({ format: SCENE_FORMAT, version }, URL_)).toThrow(/version/)
    }
  })
})

describe('loadScene', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('refuses a glTF file without the contract, naming the file', async () => {
    vi.stubGlobal('window', { location: { href: 'http://example.test/' } })
    vi.stubGlobal('fetch', vi.fn(fetchOf(gltfScene([{ name: 'world', parent: 0 }], {}))))
    await expect(loadScene(URL_)).rejects.toThrow(/scene\.glb states no scene contract/)
  })

  it('names a failed fetch', async () => {
    vi.stubGlobal('window', { location: { href: 'http://example.test/' } })
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 404, statusText: 'Not Found' })))
    await expect(loadScene(URL_)).rejects.toThrow(/Failed to fetch .*scene\.glb \(404 Not Found\)/)
  })
})
