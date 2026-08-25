// Shared Module-Federation loader for EXTERNAL plugins that ship a built remoteEntry.js
// (served by the service). Used by variation-type web previews (RemotePreview) and by the
// run-view's package/user-authored panels (PanelHost). The MF runtime is initialized once,
// seeding the host's React singletons so remotes share them (no duplicate React). Fully
// guarded: any failure surfaces as an error string so a broken/absent plugin never crashes
// the host.

import { useEffect, useState } from 'react'
import * as React from 'react'
import * as ReactDOM from 'react-dom'
import type { ComponentType } from 'react'

/** A Module-Federation remote descriptor, as emitted by the service (variation previews and
 *  run-view panels use the same shape). `module` is the exposed module id, e.g. `./costmap`. */
export interface RemoteDescriptor {
  name: string
  remote_entry_url: string
  module: string
}

type MFRuntime = typeof import('@module-federation/runtime')
let runtime: Promise<MFRuntime> | null = null

function mf(): Promise<MFRuntime> {
  if (!runtime) {
    runtime = import('@module-federation/runtime').then((m) => {
      m.init({
        name: 'robovast_ui',
        remotes: [],
        shared: {
          react: { version: '18.3.1', lib: () => React, shareConfig: { singleton: true, requiredVersion: '^18' } },
          'react-dom': { version: '18.3.1', lib: () => ReactDOM, shareConfig: { singleton: true, requiredVersion: '^18' } },
        },
      })
      return m
    })
  }
  return runtime
}

/** Why a remote failed to load, in terms of the actual exception.
 *
 *  Module Federation's `Module.getEntry` catches the browser's `import()` rejection and
 *  re-throws its own assert — `remoteEntryExports is undefined` — which names neither the
 *  cause nor the file. Its `loadEntryError` hook is not passed the error either, so the real
 *  one is unreachable from inside the runtime. This re-imports the entry directly, on the
 *  failure path only, to recover it.
 *
 *  A syntax error in the bundle, a 404, a MIME type the browser refuses to execute and a
 *  runtime version skew all reach the user as the same sentence otherwise. That sentence has
 *  already cost one debugging round — the comment above `type: 'module'` is what it bought.
 */
async function diagnose(remote: RemoteDescriptor, mfError: Error): Promise<string> {
  let real = ''
  try {
    await import(/* @vite-ignore */ remote.remote_entry_url)
    // The direct import succeeded, so the entry itself is loadable and the failure is in
    // what MF did with it — sharing, the exposed module id, or the container name.
    real = 'the entry imports cleanly on its own, so this is not the bundle: ' +
      'check the exposed module id and the container name.'
  } catch (probe) {
    real = `importing the entry failed: ${(probe as Error).message}`
  }
  // Both runtimes, because a skew between them produces exactly the message MF threw and
  // names none of it. The remote embeds its own; this is the host's.
  const hostRuntime = MF_RUNTIME_VERSION ? ` [host MF runtime ${MF_RUNTIME_VERSION}]` : ''
  return `${mfError.message}\n\n${real}${hostRuntime}`
}

/** The host's Module-Federation runtime version, for the diagnosis above. Read from the
 *  package rather than hard-coded, so it cannot drift from what is installed. */
const MF_RUNTIME_VERSION: string =
  (import.meta as unknown as { env?: Record<string, string> }).env?.VITE_MF_RUNTIME_VERSION ?? ''

/** Load a remote's exposed component at runtime. Returns `{Comp, err}`: `Comp` is null while
 *  loading, then the component (or stays null with `err` set on failure). The caller renders
 *  the loading/error states and passes the component its props. */
export function useRemoteComponent<P>(
  remote: RemoteDescriptor,
): { Comp: ComponentType<P> | null; err: string | null } {
  const [Comp, setComp] = useState<ComponentType<P> | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setComp(null)
    setErr(null)
    ;(async () => {
      try {
        const m = await mf()
        // `type: 'module'` — our remotes are built by @module-federation/vite, whose remoteEntry.js
        // is an ES module (loaded via dynamic import()). Without this the runtime defaults to
        // 'global' (script injection + window[name]), which fails for an ESM entry with
        // "remoteEntryExports is undefined".
        m.registerRemotes([{ name: remote.name, entry: remote.remote_entry_url, type: 'module' }])
        const mod = (await m.loadRemote(`${remote.name}/${remote.module.replace(/^\.\//, '')}`)) as
          | { default?: ComponentType<P> }
          | ComponentType<P>
        if (!cancelled) {
          const C =
            (mod as { default?: ComponentType<P> }).default ?? (mod as ComponentType<P>)
          setComp(() => C)
        }
      } catch (e) {
        if (!cancelled) setErr(await diagnose(remote, e as Error))
      }
    })()
    return () => {
      cancelled = true
    }
  }, [remote.name, remote.remote_entry_url, remote.module])

  return { Comp, err }
}
