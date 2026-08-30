import { useEffect, useState } from 'react'
import Alert from '@mui/material/Alert'
import AlertTitle from '@mui/material/AlertTitle'
import Button from '@mui/material/Button'
import Collapse from '@mui/material/Collapse'
import Stack from '@mui/material/Stack'
import NotificationsRoundedIcon from '@mui/icons-material/NotificationsRounded'
import * as browserNotify from '@/lib/browserNotify'
import { isRunning } from '@/lib/robovastClient'
import { useCampaignStream } from './CampaignStreamProvider'
import { useToasts } from './ToastProvider'

// The one-time ask for OS notifications, and the only place the permission prompt is raised
// from a first visit.
//
// Three decisions worth keeping:
//
// **It is a banner, not a toast.** A toast is short-lived by contract (lib/toasts.ts) and clears
// itself after ten seconds; an ask that evaporates while nobody is looking is spent for nothing.
// This one stays until it is answered.
//
// **It waits for a reason.** Nothing is asked until this browser sees a campaign actually
// running -- the moment the offer means something. Asking on arrival is the pattern browsers
// answer with their quiet UI, and it would need a delay invented to stand in for a trigger.
//
// **The browser prompt hangs off the button, not off mount.** Firefox and Safari ignore a
// requestPermission() that no gesture stands behind: it resolves `default` without ever showing
// the prompt. So the real ask can only be the click on Enable, which is what this banner exists
// to produce.
export function NotificationAsk() {
  const { data } = useCampaignStream()
  const { notify } = useToasts()
  const [asking, setAsking] = useState(false)
  const [busy, setBusy] = useState(false)

  const live = !!data?.campaigns.some(isRunning)
  useEffect(() => {
    // Raised once and then left up: a campaign that ends while the banner is on screen has not
    // withdrawn the question, and a banner that vanished mid-read would take the answer with it.
    if (live && browserNotify.shouldAsk()) setAsking(true)
  }, [live])

  const answer = async (wanted: boolean) => {
    setBusy(true)
    try {
      if (!wanted) {
        // Stored, not merely left unset: a decline is an answer, and an unstored one would be
        // re-asked on the next campaign.
        browserNotify.setOptedIn(false)
        return
      }
      const result = await browserNotify.requestPermission()
      if (result === 'granted') {
        browserNotify.setOptedIn(true)
        notify({ severity: 'success', message: 'Notifications on for this browser' })
        return
      }
      browserNotify.setOptedIn(false)
      notify({
        severity: 'info',
        message: 'The browser did not allow notifications',
        note: 'Admin → This browser can turn them on once the browser permits them.',
      })
    } finally {
      setBusy(false)
      setAsking(false)
    }
  }

  return (
    <Collapse in={asking} unmountOnExit>
      <Alert
        severity="info"
        icon={<NotificationsRoundedIcon fontSize="inherit" />}
        // No ✕: MUI renders `action` in its place anyway, and a third control would only ask
        // which of two dismissals is the real one. "No thanks" is the decline, and saying it in
        // words is what makes clear that it is remembered.
        action={
          <Stack direction="row" spacing={1} sx={{ alignItems: 'center', mr: 1 }}>
            <Button color="inherit" size="small" disabled={busy} onClick={() => void answer(false)}>
              No thanks
            </Button>
            <Button variant="contained" size="small" disabled={busy} onClick={() => void answer(true)}>
              Enable
            </Button>
          </Stack>
        }
        sx={{ mb: 2 }}
      >
        <AlertTitle sx={{ mb: 0 }}>Tell you when a campaign finishes?</AlertTitle>
        Only while this tab is in the background — the page already shows you the rest.
      </Alert>
    </Collapse>
  )
}
