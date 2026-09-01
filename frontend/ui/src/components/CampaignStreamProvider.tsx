import { createContext, useContext, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { hasResults, robovast, type ListCampaignsResponse } from '@/lib/robovastClient'
import { useLiveStream } from '@/lib/liveStream'
import { describeCampaignEvent, seedPhases, trackCampaignPhases, type CampaignSpell } from '@/lib/campaignEvents'
import { openCampaignCard, openResultsView } from '@/lib/nav'
import * as browserNotify from '@/lib/browserNotify'
import { useToasts } from './ToastProvider'

// The campaign list, streamed once for the whole app.
//
// This lived inside Monitor until it needed a second reader. Monitor is wrapped in `KeepAlive`
// and is only mounted once its page has been *visited*, so a deep link straight to `#/results`
// left the stream unopened -- fine while the list was the only thing that wanted it, and wrong
// the moment anything app-wide (a notification, a badge) depends on campaigns changing. Hoisting
// it to a provider makes it independent of which page is on screen while still opening exactly
// one EventSource: the hook below runs once, in the provider, and readers take its value from
// context.
//
// The stream itself is unchanged. The server pushes the full list on connect and on every change
// (a server-side loop over list_campaigns), so this is the single source for the list -- no
// polling. useLiveStream owns the recovery: a dropped connection, a stream the browser gave up
// on, and a socket that died silently while the tab was in the background all end in a fresh
// EventSource, which re-sends the whole list. `reconnect` is the same path on demand (the
// Refresh button).

export interface CampaignStream {
  /** The most recent full list, or null before the first frame arrives. */
  data: ListCampaignsResponse | null
  /** A `streamerror` frame from the server, which is a fault in the listing rather than the socket. */
  error: string | null
  /** False whenever what is on screen may already be behind. */
  live: boolean
  /** Tear down and rebuild the stream (the Refresh button, and the import flow). */
  reconnect: () => void
}

const CampaignStreamContext = createContext<CampaignStream | null>(null)

export function CampaignStreamProvider({ children }: { children: ReactNode }) {
  const [data, setData] = useState<ListCampaignsResponse | null>(null)
  const [error, setError] = useState<string | null>(null)

  const { state, reconnect } = useLiveStream(robovast.campaignsStreamUrl(), {
    onMessage: (e) => {
      setData(JSON.parse(e.data) as ListCampaignsResponse)
      setError(null)
    },
    events: {
      streamerror: (e) => setError(JSON.parse(e.data)),
    },
  })

  // Anything but `open` means the list on screen may already be behind; keep showing it
  // (it is still the best we have) and say so.
  const value: CampaignStream = { data, error, live: state === 'open', reconnect }

  useCampaignLifecycleNotices(data)

  return (
    <CampaignStreamContext.Provider value={value}>
      {children}
    </CampaignStreamContext.Provider>
  )
}

/**
 * Announce campaigns starting and ending.
 *
 * The two sinks are called side by side here, and only here: a toast always, and -- for an
 * ending, which is the only kind worth interrupting someone for -- an OS notification, which
 * `browserNotify.post` itself drops unless the tab is hidden and the user asked for them. That
 * is why neither the toast queue nor its provider has any notion of an event being important.
 */
function useCampaignLifecycleNotices(data: ListCampaignsResponse | null) {
  const { notify } = useToasts()
  // Survives reconnects on purpose: a new EventSource re-sends the whole list, and a baseline
  // that reset with the socket would re-announce every campaign each time it blinked.
  const phases = useRef<Map<string, CampaignSpell> | null>(null)

  useEffect(() => {
    const campaigns = data?.campaigns
    if (!campaigns) return

    // First frame of the session seeds silently -- otherwise opening the app announces
    // everything already in the list.
    if (phases.current === null) {
      phases.current = seedPhases(campaigns)
      return
    }

    const { events, baseline } = trackCampaignPhases(phases.current, campaigns)
    phases.current = baseline

    for (const evt of events) {
      const { message, note } = describeCampaignEvent(evt)
      notify({
        severity: evt.kind === 'failed' ? 'warning' : evt.kind === 'started' ? 'info' : 'success',
        message,
        note,
        // Offered only once the results actually exist: `finished` is reached before
        // postprocessing, and a campaign without it never grows the data these views read.
        //
        // A failure gets somewhere to go instead. The notice states the first line of the
        // reason and then clears itself, so the card -- which has the whole of it, and the
        // logs beside it -- has to be one click away rather than a search through the list.
        action: evt.kind === 'failed'
          ? { label: 'Open campaign', onClick: () => openCampaignCard(evt.campaignId) }
          : evt.kind === 'finished' && hasResults(evt.summary)
            ? { label: 'View results', onClick: () => openResultsView('explorer', evt.campaignId) }
            : undefined,
      })
      if (evt.kind !== 'started') {
        browserNotify.post({ title: message, body: note, tag: evt.campaignId })
      }
    }
  }, [data, notify])
}

export function useCampaignStream(): CampaignStream {
  const ctx = useContext(CampaignStreamContext)
  if (!ctx) throw new Error('useCampaignStream must be used within a <CampaignStreamProvider>')
  return ctx
}
