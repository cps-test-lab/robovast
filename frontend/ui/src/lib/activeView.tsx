import { createContext, useContext } from 'react'
import type { ReactNode } from 'react'

// Whether the view a component sits in is the one on screen.
//
// `KeepAlive` keeps every visited page mounted so its state survives navigation (editor buffers, an
// upgrade roll in flight, a log panel's scrollback — see components/KeepAlive.tsx). That is right
// for state and wrong for data: React Query's freshness model is built on mount and unmount, so a
// page that never unmounts never refetches on return, and never stops polling while hidden. Every
// non-polling query in the app was therefore read once per session, and Admin kept paying for a
// registry round trip a minute while nobody was looking at it.
//
// This context is the seam that separates the two concerns. The component stays mounted, so its
// state is kept; its queries take `enabled: useActiveView() && …`, so their *data* behaves as if
// the page had unmounted:
//
//   - a disabled observer schedules no `refetchInterval`, so a hidden page stops polling;
//   - re-enabling refetches when the query is stale, so arriving at a page re-reads it.
//
// Both are @tanstack/query-core's own semantics (`shouldFetchOptionally`, `#updateRefetchInterval`)
// rather than anything reimplemented here, and `staleTime` is what bounds how often an arrival may
// cost a round trip. The last value is kept while disabled, so switching back shows the data that
// was there and refreshes it underneath rather than blanking to a spinner.
//
// Only queries that are cheap *and* can change while the user is elsewhere take the gate. A
// finished campaign's results can do neither, so they are deliberately left ungated; the rule is
// written down once under `Staying up to date` in docs/web_ui.rst.
const ActiveViewContext = createContext(true)

// `true` outside any KeepAlive, and that default is load-bearing rather than a fallback: the
// sidebar's meters and every dialog are mounted exactly when they are on screen, so reading a
// context that nobody provided must leave their queries enabled, not silently disable them.
//
// Nesting composes with AND, which matters because KeepAlives nest: the Results topic keeps one per
// view (Explorer / Run / Data) inside the one App keeps for the topic. Being the current view
// *within* a page and that page being on screen are different questions, and only their conjunction
// means "the user can see this". Providing the inner flag on its own would tell the Run view it was
// visible while the user was reading the campaign monitor.
export function ActiveViewProvider({ active, children }: { active: boolean; children: ReactNode }) {
  const parentActive = useActiveView()
  return (
    <ActiveViewContext.Provider value={active && parentActive}>{children}</ActiveViewContext.Provider>
  )
}

export function useActiveView() {
  return useContext(ActiveViewContext)
}
