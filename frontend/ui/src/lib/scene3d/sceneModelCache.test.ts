// The cache that lets a run switch between two runs of one world skip the geometry reload. What is
// pinned is what would go wrong *silently*: a reused model still wearing the previous run's poses, a
// graph handed to a second viewer while the first still shows it, and parked worlds that are never
// freed.
import { Group } from 'three'
import { describe, expect, it, vi } from 'vitest'

import type { SceneModel } from './sceneLoader'
import { createSceneModelCache } from './sceneModelCache'

function fakeModel(): SceneModel {
  return {
    root: new Group(),
    jointMap: {},
    initialJoints: {},
    bodies: [],
    joints: [],
    basePose: () => {},
    reset: vi.fn(),
    dispose: vi.fn(),
  }
}

const A = '/campaigns/c/scene_assets/keyA/scene.json'
const B = '/campaigns/c/scene_assets/keyB/scene.json'

describe('sceneModelCache', () => {
  it('reuses a released model of the same scene, seated back at rest, without loading it again', async () => {
    const load = vi.fn(async () => fakeModel())
    const cache = createSceneModelCache(load)
    const first = await cache.acquire(A)
    const viewport = new Group()
    viewport.add(first.model.root)
    first.release()
    expect(first.model.root.parent).toBeNull()

    const second = await cache.acquire(A)
    expect(load).toHaveBeenCalledTimes(1)
    expect(second.model).toBe(first.model)
    expect(second.model.reset).toHaveBeenCalledTimes(1)
    expect(second.model.dispose).not.toHaveBeenCalled()
  })

  it('loads a different scene, and disposes the oldest parked one beyond capacity', async () => {
    const load = vi.fn(async () => fakeModel())
    const cache = createSceneModelCache(load, 1)
    const a = await cache.acquire(A)
    a.release()
    const b = await cache.acquire(B)
    expect(load).toHaveBeenCalledTimes(2)
    expect(a.model.dispose).not.toHaveBeenCalled() // still parked while B is leased
    b.release()
    expect(a.model.dispose).toHaveBeenCalledTimes(1)
    expect(cache.parked()).toBe(1)
  })

  it('never hands one model to two viewers at once', async () => {
    const load = vi.fn(async () => fakeModel())
    const cache = createSceneModelCache(load)
    const one = await cache.acquire(A)
    const two = await cache.acquire(A)
    expect(two.model).not.toBe(one.model)
    one.release()
    two.release()
    // Only one copy of a world is worth parking; the other is freed.
    expect(cache.parked()).toBe(1)
    expect(one.model.dispose).toHaveBeenCalledTimes(1)
    expect(two.model.dispose).not.toHaveBeenCalled()
  })

  it('treats a second release as a no-op', async () => {
    const cache = createSceneModelCache(async () => fakeModel())
    const lease = await cache.acquire(A)
    lease.release()
    lease.release()
    expect(cache.parked()).toBe(1)
  })
})
