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
 *  stage at all is that a two-minute image pull must not look like a hang. */
export const STAGE_TEXT: Record<string, string> = {
  queued: 'Waiting for cluster capacity — the campaign queue is busy',
  pulling: 'Fetching the simulation image onto the node — first time only',
  compiling: 'Compiling the world geometry',
  transferring: 'Copying the scene back from the container',
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

  return {
    status,
    error,
    url: status?.cached && status.url ? robovast.sceneAssetUrl(status.url) : '',
    // Non-empty exactly while building, and never once it is cached or has failed -- polling a
    // dead task while showing "nearly there" is worse than showing the error.
    buildingText:
      status && !status.cached && !status.error && !error
        ? STAGE_TEXT[status.stage] ?? 'Building the world geometry'
        : '',
  }
}
