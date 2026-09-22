// The viewport shows a scene root and never owns one: `sceneModelCache` parks a model for the
// next mount, so a viewport that freed what it was shown would hand that mount an emptied
// graph -- a world that renders as nothing rather than as an error.
import { BoxGeometry, Group, Mesh, MeshBasicMaterial } from 'three'
import { describe, expect, it, vi } from 'vitest'

import { detachSceneRoot } from './viewport'

describe('detachSceneRoot', () => {
  it('takes the root out of the viewport and leaves it loadable into another', () => {
    const viewport = new Group()
    const root = new Group()
    const geometry = new BoxGeometry()
    const material = new MeshBasicMaterial()
    const disposeGeometry = vi.spyOn(geometry, 'dispose')
    const disposeMaterial = vi.spyOn(material, 'dispose')
    root.add(new Mesh(geometry, material))
    viewport.add(root)

    detachSceneRoot(viewport, root)

    expect(root.parent).toBeNull()
    expect(viewport.children).toEqual([])
    expect(disposeGeometry).not.toHaveBeenCalled()
    expect(disposeMaterial).not.toHaveBeenCalled()

    const next = new Group()
    next.add(root)
    expect(root.parent).toBe(next)
  })

  it('does nothing when no root is shown', () => {
    const viewport = new Group()
    expect(() => detachSceneRoot(viewport, null)).not.toThrow()
  })
})
