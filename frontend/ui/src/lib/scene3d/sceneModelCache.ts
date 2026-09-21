// Shared-candidate code: this file imports only sceneLoader.ts -- keep it free of robovast imports
// (see README.md in this directory).
//
// Parsed scene models kept across viewer mounts, so switching between two runs of one world does not
// fetch and rebuild the same geometry again.
//
// Keyed by the descriptor URL, which is only sound because that URL names its bytes: the service
// puts the scene cache key -- a fingerprint of everything the descriptor is compiled from -- in the
// path, so a different world is a different URL. A cache keyed on a location instead would hand a
// run another world's geometry.
//
// A model is LEASED, never shared: a three.js graph has one parent and carries the last run's poses,
// so two viewers cannot show one model at once. A lease hands out an idle model if there is one
// (seated back at rest), and otherwise loads a fresh one; releasing it detaches the graph from
// whatever viewport showed it and parks it for the next mount. Only a few are parked -- each holds a
// whole world's buffers -- and the oldest is disposed once there are more.

import { loadScene, type SceneModel } from './sceneLoader'

/** How many released models are kept. One covers the case this exists for: the viewer is
 *  remounted per run, so there is one released model at the moment the next run asks. */
export const PARKED_SCENE_MODELS = 1

export interface SceneLease {
  model: SceneModel
  /** Hand the model back. Idempotent; the lease must not touch the model afterwards. */
  release: () => void
}

export interface SceneModelCache {
  acquire: (url: string) => Promise<SceneLease>
  /** How many models are parked right now -- for tests and diagnostics. */
  parked: () => number
}

export function createSceneModelCache(
  load: (url: string) => Promise<SceneModel> = loadScene,
  capacity: number = PARKED_SCENE_MODELS,
): SceneModelCache {
  // Oldest first, so eviction takes from the front.
  const idle: { url: string; model: SceneModel }[] = []

  const park = (url: string, model: SceneModel) => {
    model.root.removeFromParent()
    // Two viewers of one world lease two models; only one of them is worth keeping.
    const dup = idle.findIndex((e) => e.url === url)
    if (dup >= 0) idle.splice(dup, 1)[0].model.dispose()
    idle.push({ url, model })
    while (idle.length > capacity) idle.shift()!.model.dispose()
  }

  return {
    async acquire(url) {
      const at = idle.findIndex((e) => e.url === url)
      let model: SceneModel
      if (at >= 0) {
        model = idle.splice(at, 1)[0].model
        model.reset()
      } else {
        model = await load(url)
      }
      let released = false
      return {
        model,
        release: () => {
          if (released) return
          released = true
          park(url, model)
        },
      }
    },
    parked: () => idle.length,
  }
}

/** The one cache the viewers share. */
export const sceneModels = createSceneModelCache()
