import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import Alert from '@mui/material/Alert'
import AlertTitle from '@mui/material/AlertTitle'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Collapse from '@mui/material/Collapse'
import Typography from '@mui/material/Typography'
import { ErrorText } from '@/components/StatusView'
import {
  addToast,
  dismissToast,
  expireToasts,
  extendDeadlines,
  isSticky,
  type Toast,
  type ToastSpec,
} from '@/lib/toasts'

// The single, app-wide way to say something short-lived. A provider mounts one fixed stack and
// hands out an imperative API via useToasts():
//   const { notify } = useToasts()
//   notify({ severity: 'success', message: 'Share link copied' })
// The sibling of DialogProvider: that one asks the user a question and blocks on the answer,
// this one states a fact and gets out of the way.
//
// Three things it deliberately is not:
//
//  - **Not for errors.** `Severity` has no `error` member (see lib/toasts.ts). A failure carries
//    backend text worth reading twice and belongs in an inline Alert with its ErrorText.
//  - **Not a notifier.** This draws a rectangle and nothing else -- no OS notification, no
//    sound, no push. A caller that also wants an OS-level notice calls browserNotify itself,
//    next to its notify(). Threading an `important` flag through the queue would put the
//    Notification API behind a React component and make every caller declare something only
//    some of them mean.
//  - **Not a route to ntfy.** Campaign lifecycle already reaches phones from the server
//    (robovast.execution.notify), once per campaign. Fanning out from the browser instead would
//    send one push per open tab and would need the ntfy token in the client.

interface ToastsApi {
  /** Show a toast; returns its id, which `dismiss` takes. */
  notify: (spec: ToastSpec) => number
  dismiss: (id: number) => void
}

const ToastsContext = createContext<ToastsApi | null>(null)

/** How often the stack is swept for expiries. */
const SWEEP_MS = 250
/** Collapse's own duration; a dismissed toast is unmounted once it has played. */
const EXIT_MS = 180

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])
  // Ids whose exit transition is playing. They stay in `toasts` until it finishes, so the row
  // has something to animate out of; nothing else distinguishes them.
  const [leaving, setLeaving] = useState<ReadonlySet<number>>(() => new Set())

  // Ids are a counter rather than a timestamp so two toasts raised in the same millisecond
  // cannot collide into one React key.
  const nextId = useRef(0)
  // key -> id, resolved synchronously. Reading the rendered list instead would be one render
  // stale, so two notifies in the same tick would each believe they were the first and the
  // keyed one would stack after all.
  const byKey = useRef(new Map<string, number>())
  // id -> pending removal, so an exit already under way can be found and cancelled.
  const exits = useRef(new Map<number, number>())
  // Both read by the interval below, which must not restart when either changes.
  const paused = useRef(false)
  const live = useRef<Toast[]>([])
  useEffect(() => { live.current = toasts }, [toasts])

  const unmark = useCallback((id: number) => {
    setLeaving((prev) => {
      if (!prev.has(id)) return prev
      const next = new Set(prev)
      next.delete(id)
      return next
    })
  }, [])

  /** The toast is really gone: drop it from the list and from both indexes. */
  const forget = useCallback((id: number) => {
    setToasts((list) => dismissToast(list, id))
    unmark(id)
    exits.current.delete(id)
    for (const [k, v] of byKey.current) if (v === id) byKey.current.delete(k)
  }, [unmark])

  const dismiss = useCallback((id: number) => {
    // Already on its way out: a second click, or an expiry landing on a toast the user just
    // closed, must not queue a second removal.
    if (exits.current.has(id)) return
    setLeaving((prev) => new Set(prev).add(id))
    exits.current.set(id, window.setTimeout(() => forget(id), EXIT_MS))
  }, [forget])

  const notify = useCallback((spec: ToastSpec) => {
    const known = spec.key === undefined ? undefined : byKey.current.get(spec.key)
    let id: number
    if (known !== undefined) {
      id = known
      // It may be mid-exit — dismissed by hand, or expired, moments ago. Cancel that: the
      // refreshed toast reuses the id, so the pending removal would otherwise take the new
      // one with it and leave a row that collapsed and then vanished.
      const pending = exits.current.get(id)
      if (pending !== undefined) {
        window.clearTimeout(pending)
        exits.current.delete(id)
        unmark(id)
      }
    } else {
      id = ++nextId.current
      if (spec.key !== undefined) byKey.current.set(spec.key, id)
    }
    setToasts((list) => addToast(list, spec, Date.now(), id))
    return id
  }, [unmark])

  // One timer for the whole stack. While the pointer is over it, deadlines are pushed forward by
  // the tick's own length instead of the sweep being suspended -- so a toast added mid-hover is
  // held too, and there is still only one clock.
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (live.current.length === 0) return
      if (paused.current) {
        setToasts((list) => extendDeadlines(list, SWEEP_MS))
        return
      }
      const now = Date.now()
      // Routed through `dismiss` rather than dropped outright so an expiry and a click look the
      // same on screen; `expireToasts` is what decides which are over.
      const surviving = new Set(expireToasts(live.current, now))
      for (const t of live.current) if (!surviving.has(t)) dismiss(t.id)
    }, SWEEP_MS)
    return () => window.clearInterval(timer)
  }, [dismiss])

  // Nothing should outlive the provider: a pending removal firing after unmount would set state
  // on a component that is gone.
  useEffect(() => {
    const pending = exits.current
    return () => { for (const handle of pending.values()) window.clearTimeout(handle) }
  }, [])

  const api = useMemo<ToastsApi>(() => ({ notify, dismiss }), [notify, dismiss])

  return (
    <ToastsContext.Provider value={api}>
      {children}
      <Box
        onMouseEnter={() => { paused.current = true }}
        onMouseLeave={() => { paused.current = false }}
        sx={{
          position: 'fixed',
          right: 24,
          bottom: 24,
          // `snackbar` is above `modal`, which is deliberate rather than incidental: the copy
          // button in the share-import dialog acknowledges itself with a toast, and one drawn
          // under the dialog that raised it would say nothing at all.
          zIndex: (t) => t.zIndex.snackbar,
          display: 'flex',
          flexDirection: 'column',
          width: 'min(400px, calc(100vw - 48px))',
          // The column is only a hit target where a toast actually is, so the empty space above
          // the stack does not swallow clicks on the page beneath it.
          pointerEvents: 'none',
          '& > *': { pointerEvents: 'auto' },
        }}
      >
        {orderedForDisplay(toasts).map((t) => (
          <Collapse
            key={t.id}
            in={!leaving.has(t.id)}
            appear
            timeout={EXIT_MS}
            sx={{ '&:not(:last-of-type)': { mb: 1 } }}
          >
            <ToastRow toast={t} onClose={() => dismiss(t.id)} />
          </Collapse>
        ))}
      </Box>
    </ToastsContext.Provider>
  )
}

/**
 * Transient first, sticky last -- so sticky toasts sit nearest the corner.
 *
 * The column grows upward from the bottom, so the last child is the one closest to the corner
 * and the most stable position on screen. Putting the sticky ones there means an arriving notice
 * pushes the stack up *above* them, instead of shifting an error out from under the cursor of
 * someone reaching for its close button. A stable sort, so age still orders within each group.
 */
function orderedForDisplay(list: Toast[]): Toast[] {
  return [...list].sort((a, b) => Number(isSticky(a)) - Number(isSticky(b)))
}

function ToastRow({ toast, onClose }: { toast: Toast; onClose: () => void }) {
  return (
    <Alert
      severity={toast.severity}
      variant="outlined"
      onClose={onClose}
      sx={{
        // `outlined` keeps the theme's own Paper backdrop rather than the tinted fill the
        // standard variant paints, so a toast is the same glass as the cards behind it; the
        // severity reads from the icon and the left edge instead.
        borderLeftWidth: 4,
        borderLeftColor: `${toast.severity}.main`,
        bgcolor: 'background.paper',
        boxShadow: 6,
        alignItems: 'flex-start',
      }}
    >
      <AlertTitle sx={{ mb: toast.note || toast.action ? 0.5 : 0, fontWeight: 400, fontSize: 14 }}>
        {toast.message}
      </AlertTitle>
      {toast.note ? (
        // A failure's note is the backend's own words -- often a paragraph, sometimes with a
        // ref or a path in it -- so it gets the same monospace, wrapped, scrolling treatment the
        // inline Alert on the campaign card gave it. Anything else is prose and reads as prose.
        isSticky(toast) ? (
          <ErrorText>{toast.note}</ErrorText>
        ) : (
          <Typography variant="body2" color="text.secondary">
            {toast.note}
          </Typography>
        )
      ) : null}
      {toast.action ? (
        <Button
          size="small"
          onClick={() => { toast.action?.onClick(); onClose() }}
          sx={{ mt: 1, ml: -0.5 }}
        >
          {toast.action.label}
        </Button>
      ) : null}
    </Alert>
  )
}

export function useToasts(): ToastsApi {
  const ctx = useContext(ToastsContext)
  if (!ctx) throw new Error('useToasts must be used within a <ToastProvider>')
  return ctx
}
