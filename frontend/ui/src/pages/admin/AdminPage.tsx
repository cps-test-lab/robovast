import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import CircularProgress from '@mui/material/CircularProgress'
import IconButton from '@mui/material/IconButton'
import Paper from '@mui/material/Paper'
import Stack from '@mui/material/Stack'
import Tooltip from '@mui/material/Tooltip'
import Typography from '@mui/material/Typography'
import RefreshRoundedIcon from '@mui/icons-material/RefreshRounded'
import { CollapsibleBox } from '@/components/CollapsibleBox'
import { useDialogs } from '@/components/DialogProvider'
import { LogPanel } from '@/components/LogPanel'
import { robovast, type UpgradeInfo } from '@/lib/robovastClient'
import { UsageHistoryChart } from './UsageHistoryChart'

// How long to keep watching for the new pod before saying we cannot tell. Matches the
// default `vast service upgrade --timeout`, so both surfaces give up at the same
// point and an operator comparing them is not told two different stories.
const ROLL_TIMEOUT_MS = 180_000

// Grace between "the new pod is serving" and the reload. Long enough to read the line and
// stop it, short enough that nobody sits watching a page that said it would reload.
const RELOAD_COUNTDOWN_S = 5

/** One `label: value` line. Values are monospace: most of them are digests and refs. */
function Field({ label, value }: { label: string; value: string }) {
  return (
    <Stack direction="row" spacing={1}>
      <Typography variant="caption" color="text.secondary" sx={{ width: 96, flexShrink: 0 }}>
        {label}
      </Typography>
      <Typography variant="caption" sx={{ fontFamily: 'monospace', wordBreak: 'break-all' }}>
        {value}
      </Typography>
    </Stack>
  )
}

// What the digests add up to, in words. Three outcomes and not two: `upgrade_available` is
// null when the registry did not answer, and rendering that as "up to date" is the one
// wrong answer here — it would tell someone a fix they just published is not there.
function upgradeVerdict(info: UpgradeInfo): string {
  if (info.upgrade_available === true) return 'a newer image is published at this tag'
  if (info.upgrade_available === false) return 'running the newest image at this tag'
  return 'could not ask the registry what this tag points at'
}

export function AdminPage() {
  const qc = useQueryClient()
  const { confirm } = useDialogs()
  const [rolling, setRolling] = useState(false)
  const [rollNote, setRollNote] = useState<string | null>(null)
  // Its own flag rather than `isFetching`, which is also true for the background poll below
  // and would spin the icon every minute on its own. This one means *the user asked*.
  const [refreshing, setRefreshing] = useState(false)
  // The roll reached the new pod. Kept after the countdown is declined, because the tab is
  // still on the old build either way and that is worth saying.
  const [handover, setHandover] = useState(false)
  // Seconds left before the reload, or null once it is declined or done.
  const [reloadIn, setReloadIn] = useState<number | null>(null)
  // Open by default: unlike a campaign log, which is one of many on a crowded
  // page, this is one of the three things the Admin page exists to show.
  const [logOpen, setLogOpen] = useState(true)

  const version = useQuery({ queryKey: ['version'], queryFn: robovast.version, retry: false })
  const upgrade = useQuery({
    queryKey: ['upgradeInfo'],
    queryFn: robovast.upgradeInfo,
    // Fast while a roll is in flight — this poll IS how the handover is detected — and
    // slow otherwise: it costs a registry round trip on the cluster lane.
    refetchInterval: rolling ? 3_000 : 60_000,
    retry: false,
  })

  // Reload the document once the count reaches zero. It is not a nicety: this tab holds a
  // build whose hashed chunks the service no longer serves, so every view it has not opened
  // yet is already broken (see ErrorBoundary, which exists to catch exactly that). Doing it
  // here, in the second the user was expecting a restart, turns a future error screen into
  // an expected blink -- and navigation lives in the URL hash, so it lands back on Admin.
  useEffect(() => {
    if (reloadIn === null) return
    if (reloadIn <= 0) {
      window.location.reload()
      return
    }
    const t = setTimeout(() => setReloadIn((n) => (n === null ? null : n - 1)), 1_000)
    return () => clearTimeout(t)
  }, [reloadIn])

  // Both queries, because the panel shows both and half a refresh is the confusing kind:
  // the version line and the digests would then disagree about when they were read.
  function refreshService() {
    setRefreshing(true)
    void Promise.allSettled([version.refetch(), upgrade.refetch()])
      .finally(() => setRefreshing(false))
  }

  async function roll(info: UpgradeInfo) {
    const live = info.active_campaigns
    const ok = await confirm({
      title: 'Roll onto the newest image?',
      danger: live.length > 0,
      confirmLabel: live.length > 0 ? 'Roll anyway' : 'Roll the pod',
      message: (
        <>
          <p>
            The pod restarts onto whatever <code>{info.image_ref}</code> resolves to now.
            The new pod starts before this one stops, so the API stays up.
          </p>
          {live.length > 0 && (
            <p>
              <b>{live.length} campaign(s) are live</b> — their controller runs in the pod
              this replaces: {live.map((c) => `${c.campaign_id} (${c.phase})`).join(', ')}
            </p>
          )}
          <p>
            This does <b>not</b> reconcile RBAC, the registry route, the credential Secrets
            or the build daemon. For any of those, run{' '}
            <code>vast service upgrade</code>.
          </p>
        </>
      ),
    })
    if (!ok) return
    const before = info.running_digest
    setRollNote(null)
    setHandover(false)
    setReloadIn(null)
    setRolling(true)
    try {
      // `force` is exactly the dialog's answer: the only thing the server refuses is a
      // roll over live campaigns, and confirming above with campaigns listed IS the
      // override. Sending it unconditionally would make the server's guard unreachable.
      await robovast.upgradeService(live.length > 0)
    } catch (e) {
      setRolling(false)
      setRollNote(String(e))
      return
    }
    // Watch for the handover rather than trusting the POST: it returns as soon as the roll
    // is asked for. `running_image_digest` reads the newest Running pod, so this answers
    // the same whichever pod serves the poll.
    const deadline = Date.now() + ROLL_TIMEOUT_MS
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 3_000))
      try {
        const now = await qc.fetchQuery({
          queryKey: ['upgradeInfo'], queryFn: robovast.upgradeInfo, staleTime: 0,
        })
        if (now.running_digest && now.running_digest !== before) {
          setRolling(false)
          // The panel's own numbers first, so it stops describing the pod that just went
          // away even in the seconds before the reload -- and for good if it is declined.
          // `upgradeInfo` is already current: the poll above is what fetched this.
          void version.refetch()
          setHandover(true)
          setReloadIn(RELOAD_COUNTDOWN_S)
          return
        }
      } catch {
        // Expected once or twice at the handover: keep waiting rather than calling a
        // reconnect a failure.
      }
    }
    setRolling(false)
    // Deliberately not phrased as a failure. The roll may simply be slow, and the command
    // that can actually say why is the one named here.
    setRollNote(
      'the new pod has not taken over yet. `vast service upgrade` reports the reason'
      + ' Kubernetes gave — an image it cannot pull, a node it cannot schedule on, a'
      + ' crash-loop.',
    )
  }

  const info = upgrade.data
  return (
    <Stack spacing={2}>
      <Typography variant="h6">Admin</Typography>

      <Paper variant="outlined" sx={{ p: 2 }}>
        <UsageHistoryChart />
      </Paper>

      <Paper variant="outlined" sx={{ p: 2 }}>
        <Stack spacing={1}>
          <Stack direction="row" alignItems="center" justifyContent="space-between">
            <Typography variant="subtitle2">This service</Typography>
            <Tooltip title="Re-read the version and ask the registry what this tag points at now">
              {/* Kept enabled while it runs so the tooltip stays reachable; a second click
                  is a no-op refetch. */}
              <IconButton size="small" aria-label="Reload service info" onClick={refreshService}>
                {refreshing
                  ? <CircularProgress size={18} />
                  : <RefreshRoundedIcon fontSize="small" />}
              </IconButton>
            </Tooltip>
          </Stack>
          {version.isSuccess ? (
            <>
              <Field label="version" value={version.data.robovast_version} />
              {/* Absent means "this deployment cannot tell", which is not a mismatch — so
                  print nothing rather than a blank or a placeholder that reads as one. */}
              {version.data.code_revision ? (
                <Field label="revision" value={version.data.code_revision} />
              ) : null}
              <Field
                label="backend"
                value={
                  version.data.backend
                  + (version.data.namespace ? ` · ${version.data.namespace}` : '')
                  + (version.data.in_pod ? ' · in pod' : '')
                }
              />
            </>
          ) : (
            <Typography variant="caption" color="text.disabled">
              {version.isError ? 'could not read the version' : 'loading…'}
            </Typography>
          )}
          {info?.image_ref ? <Field label="image" value={info.image_ref} /> : null}
          {info?.running_digest ? <Field label="running" value={info.running_digest} /> : null}

          {/* The roll is offered only where it exists. On a local service, or a cluster
              service running outside the cluster, there is no Deployment of its own to
              roll — so there is no button, and the reason is a caption rather than a
              disabled control that invites clicking. */}
          {info?.supported ? (
            <Stack direction="row" spacing={2} alignItems="center" sx={{ pt: 1 }}>
              {/* Only where there is something to roll onto. Note `!== false` and not
                  `=== true`: a null `upgrade_available` means the registry did not answer,
                  and hiding the button there would stand an operator who knows a newer
                  image is published in front of a page that offers them nothing. The
                  verdict caption beside it says which of the two it is. `rolling` holds the
                  button in place across the handover, where the poll flips the flag — a
                  control that disappears while it is working reads as a crash. */}
              {rolling || info.upgrade_available !== false ? (
                <Button
                  variant="contained"
                  size="small"
                  disabled={rolling}
                  onClick={() => roll(info)}
                >
                  {rolling ? 'Rolling…' : 'Upgrade'}
                </Button>
              ) : null}
              <Typography variant="caption" color="text.secondary">
                {rolling ? 'waiting for the new pod to take over…' : upgradeVerdict(info)}
              </Typography>
            </Stack>
          ) : info ? (
            <Typography variant="caption" color="text.disabled" sx={{ pt: 1 }}>
              {info.unsupported_reason}
            </Typography>
          ) : null}
          {/* The two halves of a finished roll. Counting down, the reload is the default and
              declining is the button; declined, the tab keeps a standing warning rather than
              a success line, because a build the service no longer has is a fault waiting to
              surface in the next view opened, not a completed job. */}
          {handover ? (
            <Alert
              severity={reloadIn === null ? 'warning' : 'success'}
              sx={{ mt: 1 }}
              onClose={reloadIn === null ? () => setHandover(false) : undefined}
              action={
                <>
                  <Button color="inherit" size="small" onClick={() => window.location.reload()}>
                    {reloadIn === null ? 'Reload' : 'Reload now'}
                  </Button>
                  {reloadIn === null ? null : (
                    <Button color="inherit" size="small" onClick={() => setReloadIn(null)}>
                      Not now
                    </Button>
                  )}
                </>
              }
            >
              {reloadIn === null
                ? 'The new pod is serving. This tab is still running the previous build, so'
                  + ' views it has not opened yet may fail to load until it is reloaded.'
                : `The new pod is serving. This tab is still running the previous build —`
                  + ` reloading in ${reloadIn}s.`}
            </Alert>
          ) : null}
          {rollNote ? (
            <Alert severity="info" sx={{ mt: 1 }} onClose={() => setRollNote(null)}>
              {rollNote}
            </Alert>
          ) : null}
        </Stack>
      </Paper>

      <CollapsibleBox
        open={logOpen}
        onToggle={() => setLogOpen((v) => !v)}
        // On hover rather than standing under the header: it is a caveat about the log's
        // reach, worth having within reach and not worth a line of its own above every
        // reading of it.
        title={
          <Tooltip
            placement="right"
            title={
              'The last few hundred kB this process logged, kept in memory. A container '
              + 'that has already died is only in `kubectl logs -p`.'
            }
          >
            <span>Service log</span>
          </Tooltip>
        }
      >
        <Box sx={{ height: 360 }}>
          <LogPanel resetKey="service" streamUrl={robovast.serviceLogStreamUrl()} />
        </Box>
      </CollapsibleBox>
    </Stack>
  )
}
