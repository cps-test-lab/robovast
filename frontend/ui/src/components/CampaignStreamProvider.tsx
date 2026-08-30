import { createContext, useContext, useState } from 'react'
import type { ReactNode } from 'react'
import { robovast, type ListCampaignsResponse } from '@/lib/robovastClient'
import { useLiveStream } from '@/lib/liveStream'

// The campaign list, streamed once for the whole app.
//
// This lived inside Monitor until it needed a second reader. Monitor is wrapped in `KeepAlive`
// and is only mounted once its page has been *visited*, so a deep link straight to `#/results`
// left the stream unopened -- fine while the list was the only thing that wanted it, and wrong
// the moment anything app-wide depends on campaigns changing. Hoisting it to a provider makes it
// independent of which page is on screen while still opening exactly one EventSource: the hook
// below runs once, in the provider, and readers take its value from context.
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

  return (
    <CampaignStreamContext.Provider value={value}>
      {children}
    </CampaignStreamContext.Provider>
  )
}

export function useCampaignStream(): CampaignStream {
  const ctx = useContext(CampaignStreamContext)
  if (!ctx) throw new Error('useCampaignStream must be used within a <CampaignStreamProvider>')
  return ctx
}
