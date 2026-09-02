// Resolving a scene descriptor through the service: ask, POST once if nothing is cached, then poll
// until it is.
//
// Shared by the run view's scene3d panel and the config view's, because the *protocol* is the same
// on both — only which endpoint answers differs. Asking never builds, which is what makes it safe
// to re-render, prefetch, or reload mid-build; the single POST is the explicit trigger, and
// re-posting each tick would be harmless (the service joins an in-flight build) but would hide a
// build that silently never starts.
//
// Unlike its neighbours in this directory, this one does touch the client — it is the seam between
// the renderer and the service, and pretending otherwise would mean each panel re-implementing the
// state machine around it.

import { useEffect, useState } from 'react'
import { robovast, type SceneStatus } from '@/lib/robovastClient'

/** How often to re-ask while geometry is being built. A warm cluster build is ~8 s and a cold one up
 *  to a couple of minutes (a 2 GB image pull), so a second is responsive without making the wait
 *  itself expensive. */
export const SCENE_POLL_MS = 1000

/** What each stage is called, naming the *cost* rather than the mechanism -- the point of showing a
 *  stage at all is that a two-minute image pull must not look like a hang. Every key here is a stage
 *  the service can actually report (`scene_cache.STAGES`); an entry for one it cannot is a message
 *  no reader will ever see, and reads as supported. */
export const STAGE_TEXT: Record<string, string> = {
  queued: 'Waiting for a node to build on — the cluster has no free capacity yet',
  pulling: 'Fetching the simulation image onto the node — first time only',
  starting: 'Starting the build container',
  compiling: 'Compiling the world geometry',
}

export interface SceneGeometry {
  /** The service's last answer, or null before the first one. */
  status: SceneStatus | null
  /** A transport/trigger failure, as distinct from `status.error` (the build's own reason). */
  error: string | null
  /** URL of `scene.json` once cached; '' while there is nothing to load. */
  url: string
  /** Non-empty exactly while a build is in flight, naming the stage. */
  buildingText: string
  /** The service's own words for that stage, when it has any -- a pod's `ImagePullBackOff` message,
   *  say. Shown under the stage rather than instead of it: the stage says which wait this is, and
   *  only this says that the wait is one that will never end. */
  buildingDetail: string
}

/**
 * Drive one descriptor to `cached`, whatever endpoint answers for it.
 *
 * @param ask    read the status; must never start work.
 * @param start  the explicit trigger, called at most once per mount.
 * @param key    re-run when this changes (the run, or the project).
 */
export function useSceneGeometry(
  ask: () => Promise<SceneStatus>,
  start: () => Promise<{ ok: boolean; message: string | null }>,
  key: string,
): SceneGeometry {
  const [status, setStatus] = useState<SceneStatus | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined
    let asked = false
    setStatus(null)
    setError(null)

    const poll = async () => {
      try {
        const next = await ask()
        if (cancelled) return
        setStatus(next)
        if (next.error) return
        if (next.cached) return
        if (!asked && !next.in_progress) {
          asked = true
          const started = await start()
          if (cancelled) return
          if (!started.ok) {
            setError(started.message || 'the geometry build could not be started')
            return
          }
        }
        timer = setTimeout(poll, SCENE_POLL_MS)
      } catch (err: unknown) {
        if (cancelled) return
        setError(err instanceof Error ? err.message : String(err))
      }
    }
    void poll()
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
    // `ask`/`start` are closures rebuilt every render; `key` is what actually identifies the
    // descriptor being resolved.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])

  // The status of a build in flight, or null. Never once it is cached or has failed -- polling a
  // dead task while showing "nearly there" is worse than showing the error.
  const building = status && !status.cached && !status.error && !error ? status : null

  return {
    status,
    error,
    url: status?.cached && status.url ? robovast.sceneAssetUrl(status.url) : '',
    buildingText: building ? STAGE_TEXT[building.stage] ?? 'Building the world geometry' : '',
    buildingDetail: building?.stage_detail ?? '',
  }
}
