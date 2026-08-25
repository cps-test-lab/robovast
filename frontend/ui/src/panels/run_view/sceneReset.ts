// The channel the run view's header menu resets a 3D view through.
//
// The gear belongs to the view, the camera belongs to a panel's viewport, and nothing in the panel
// contract carries a command from the one to the other -- a panel receives a clock and a data
// provider, both of them state to read rather than a mailbox. So a panel holding a viewport says,
// while it is mounted, what "reset" means for it, and the menu calls whatever is registered.
//
// Registration is also the availability answer, which is why it is a set and not a single slot: the
// entry is enabled exactly while some viewport is mounted rather than while the vast happens to
// declare a `scene3d` panel. A declared panel whose geometry never arrived still has a camera worth
// re-framing, a view with no 3D panel at all has nothing to re-frame, and a panel that grows a
// second viewport -- or a remote one that grows its first -- needs no change here to be included.

import { useSyncExternalStore } from 'react'

const resets = new Set<() => void>()
const listeners = new Set<() => void>()

function announce(): void {
  listeners.forEach((listener) => listener())
}

/** Offer a mounted viewport's reset to the menu; call the returned function on unmount. */
export function registerSceneReset(reset: () => void): () => void {
  resets.add(reset)
  announce()
  return () => {
    resets.delete(reset)
    announce()
  }
}

/** Re-frame every mounted 3D view. */
export function resetSceneViews(): void {
  resets.forEach((reset) => reset())
}

/** Whether there is a 3D view to reset -- as an external store, because a panel registers in its
 *  mount effect (and PanelHost remounts panels per run), which is after the menu first renders. */
export function useSceneResetAvailable(): boolean {
  return useSyncExternalStore(
    (onChange) => {
      listeners.add(onChange)
      return () => {
        listeners.delete(onChange)
      }
    },
    () => resets.size > 0,
  )
}
