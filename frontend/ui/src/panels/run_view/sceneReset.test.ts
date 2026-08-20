// The registry behind the header's "Reset 3D view" entry. What is worth pinning is not that a
// callback can be called, but the lifetime fact the panel's remount depends on: a deregistered
// viewport is not reset. PanelHost builds a fresh panel per run, so an entry left behind would
// re-frame a disposed viewport -- a use-after-dispose the menu itself cannot see.
import { describe, expect, it, vi } from 'vitest'
import { registerSceneReset, resetSceneViews } from './sceneReset'

describe('scene reset registry', () => {
  it('resets every registered viewport, and none that unregistered', () => {
    const live = vi.fn()
    const gone = vi.fn()
    registerSceneReset(gone)()
    const unregister = registerSceneReset(live)

    resetSceneViews()
    expect(live).toHaveBeenCalledTimes(1)
    expect(gone).not.toHaveBeenCalled()

    unregister()
    resetSceneViews()
    expect(live).toHaveBeenCalledTimes(1)
  })

  it('resets nothing, without throwing, when no viewport is mounted', () => {
    expect(() => resetSceneViews()).not.toThrow()
  })
})
