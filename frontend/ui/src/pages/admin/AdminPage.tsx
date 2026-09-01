import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import CircularProgress from '@mui/material/CircularProgress'
import FormControlLabel from '@mui/material/FormControlLabel'
import IconButton from '@mui/material/IconButton'
import Paper from '@mui/material/Paper'
import Stack from '@mui/material/Stack'
import Switch from '@mui/material/Switch'
import Tooltip from '@mui/material/Tooltip'
import Typography from '@mui/material/Typography'
import RefreshRoundedIcon from '@mui/icons-material/RefreshRounded'
import { CollapsibleBox } from '@/components/CollapsibleBox'
import { McpToolsPanel } from './McpToolsPanel'
import { ServiceEventsPanel } from './ServiceEventsPanel'
import { useDialogs } from '@/components/DialogProvider'
import { useToasts } from '@/components/ToastProvider'
import * as browserNotify from '@/lib/browserNotify'
import { LogPanel } from '@/components/LogPanel'
import { useActiveView } from '@/lib/activeView'
import { robovast, RobovastError, type UpgradeInfo } from '@/lib/robovastClient'
import { formatAge, formatLocalTime } from '@/lib/time'
import { ServiceConfigPanel } from './ServiceConfigPanel'
import { UsageHistoryChart } from './UsageHistoryChart'

// How long to keep watching for the new pod before saying we cannot tell. Matches the
// default `vast service upgrade --timeout`, so both surfaces give up at the same
// point and an operator comparing them is not told two different stories.
const ROLL_TIMEOUT_MS = 180_000

// Grace between "the new pod is serving" and the reload. Long enough to read the line and
// stop it, short enough that nobody sits watching a page that said it would reload.
const RELOAD_COUNTDOWN_S = 5

/** Hover delay on the notification switch's caveat.
 *
 *  Past MUI's default, which fires fast enough that the tip appears while the pointer is only
 *  passing over the row on its way somewhere else. This text is read once, by someone who has
 *  stopped at the switch and is deciding -- so it waits for the pause that means deciding, and
 *  stays out of the way of every other crossing. */
const PREF_TIP_MS = 600

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

// Absolute time and relative age together: the first is the fact, the second is the one that
// answers "is this deployment old?" at a glance. Beside `upgradeVerdict` because both turn a
// machine-readable field into the sentence a reader actually wants.
function builtLine(iso: string): string {
  return `${formatLocalTime(iso)} (${formatAge(iso)})`
}

// What the digests add up to, in words — and in words that do not assume the reader knows
// what a tag or a registry is. Three outcomes and not two: `upgrade_available` is null when
// the registry did not answer, and rendering that as "up to date" is the one wrong answer
// here — it would tell someone a fix they just published is not there.
function upgradeVerdict(info: UpgradeInfo): string {
  if (info.upgrade_available === true) return 'a newer version is available'
  if (info.upgrade_available === false) return 'this is the newest version'
  return 'could not check whether a newer version exists'
}

export function AdminPage() {
  const qc = useQueryClient()
  const { confirm } = useDialogs()
  const { notify } = useToasts()
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
  // Collapsed, unlike the log above: the log is one of the things this page exists to
  // show, while the configuration is what you come looking for on a particular day. Its
  // query is gated on this flag, so an unopened panel costs nothing.
  const [configOpen, setConfigOpen] = useState(false)
  // Collapsed too, and for the configuration's reason rather than the log's: this answers
  // "why did that not work?", which is a question somebody arrives with. Its query is gated
  // on the flag, so an unopened panel costs nothing.
  const [eventsOpen, setEventsOpen] = useState(false)
  // Collapsed, like the two above: this answers "which tools do agents actually use, and
  // what happened when they did?", which is a question somebody arrives with rather than
  // one the page owes on every visit. Its two queries are gated on the flag.
  const [mcpOpen, setMcpOpen] = useState(false)

  // This page is kept mounted once visited, so both readings are gated on it being the one on
  // screen: they then stop while it is not, and are re-read on the way back in — which is the
  // moment someone asks "did the version I just published land?". See lib/activeView.tsx.
  const active = useActiveView()
  const version = useQuery({
    queryKey: ['version'],
    queryFn: robovast.version,
    enabled: active,
    retry: false,
  })
  const upgrade = useQuery({
    queryKey: ['upgradeInfo'],
    queryFn: robovast.upgradeInfo,
    // A roll keeps this live wherever the user has navigated to: the panel below still describes
    // a handover in progress, and describing it from the pod that is going away would be worse
    // than saying nothing.
    enabled: active || rolling,
    // Fast while a roll is in flight — this poll IS how the handover is detected — and
    // slow otherwise: it costs a registry round trip on the cluster lane.
    refetchInterval: rolling ? 3_000 : 60_000,
    // For the same reason, a floor on the arrival read: flipping to Admin and back is a plausible
    // thing to do, and it must not spend a registry round trip each time.
    staleTime: 10_000,
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
      title: 'Upgrade RoboVAST now?',
      confirmLabel: 'Upgrade now',
      // Written for someone who has never heard of Kubernetes, and kept to three lines:
      // what happens, what it costs, what it does not cover. The image ref is not repeated
      // here -- the `image` field sits directly above this button.
      message: (
        <>
          <p>
            RoboVAST restarts on the newest published version. It stays reachable, and this
            page reloads itself once the new version is up.
          </p>
          {live.length > 0 && (
            <p>
              <b>{live.length} campaign(s) are still running.</b> Their jobs keep running and
              the replacement re-attaches to them: {live.map((c) => `${c.campaign_id} (${c.phase})`).join(', ')}.
              If any of them could not be picked up again, the server refuses the restart and
              says which -- there is nothing to decide here yet.
            </p>
          )}
          <p>
            This updates RoboVAST only. For the rest of the installation — permissions,
            registry, credentials, image builder — run <code>vast service upgrade</code>.
          </p>
        </>
      ),
    })
    if (!ok) return
    const before = info.running_digest
    setRollNote(null)
    setHandover(false)
    setReloadIn(null)
    // A roll outlives the page that started it: this component stays mounted behind
    // KeepAlive and keeps polling wherever the user has navigated to. The handover alert
    // below therefore lands on a panel nobody is looking at, and the countdown it starts
    // reloads the whole document a few seconds later -- which the comment on that effect
    // describes as "an expected blink", and it only is one for someone who is watching.
    // So say it where the user actually is, and offer the same way out the alert offers.
    const announceHandover = () => {
      notify({
        severity: 'success',
        key: 'upgrade-handover',
        message: 'RoboVAST upgraded',
        note: `This page reloads in ${RELOAD_COUNTDOWN_S}s to pick up the new build.`,
        action: { label: 'Not now', onClick: () => setReloadIn(null) },
      })
      // The second caller of this sink, and for the same reason as the first: the tab may not
      // be on screen at all. `post` decides that for itself.
      browserNotify.post({
        title: 'RoboVAST upgraded',
        body: 'Reload the page to finish.',
        tag: 'upgrade-handover',
      })
    }

    setRolling(true)
    try {
      // Never `force` on the first attempt. The server refuses (409) only for the campaigns
      // its own resume planner says the replacement could NOT pick up again -- and a live
      // campaign is not itself one of those. This page cannot make that call: `active_campaigns`
      // carries no resumability. So it asks without `force`, which is what makes the guard
      // reachable at all, and lets the refusal supply the text the override is decided on.
      await robovast.upgradeService(false)
    } catch (e) {
      setRolling(false)
      if (!(e instanceof RobovastError) || e.status !== 409) {
        setRollNote(String(e))
        return
      }
      // The one refusal that has an override. Its message names each campaign that would be
      // lost and why, so it is shown verbatim rather than summarised -- the reason is what
      // the operator would have to act on, and this page does not know it any other way.
      const override = await confirm({
        title: 'Upgrade anyway?',
        danger: true,
        confirmLabel: 'Upgrade anyway',
        message: (
          <>
            <p>RoboVAST refused the restart:</p>
            <p>{e.message}</p>
            <p>Forcing it stops those campaigns for good.</p>
          </>
        ),
      })
      if (!override) return
      setRolling(true)
      try {
        await robovast.upgradeService(true)
      } catch (forced) {
        setRolling(false)
        setRollNote(String(forced))
        return
      }
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
          announceHandover()
          return
        }
      } catch {
        // Expected once or twice at the handover: keep waiting rather than calling a
        // reconnect a failure.
      }
    }
    setRolling(false)
    // Deliberately not phrased as a failure: the upgrade may simply be slow. The three
    // causes are kept because each has a different fix, but named in a clause rather than
    // a paragraph.
    setRollNote(
      'The new version has not taken over yet. Run `vast service upgrade` to see why —'
      + ' usually a download that failed, no room to start, or a crash on startup.',
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
          <Typography variant="subtitle2">This service</Typography>
          {version.isSuccess ? (
            <>
              {/* The release, not `robovast_version`. That field prefers a revision
                  wherever one can be had, and a deployed image always bakes one in, so it
                  printed the same SHA as the revision line below it — one string, twice,
                  under two labels, and the semver nowhere. */}
              {version.data.package_version ? (
                <Field label="version" value={version.data.package_version} />
              ) : null}
              {/* Absent means "this deployment cannot tell", which is not a mismatch — so
                  print nothing rather than a blank or a placeholder that reads as one. */}
              {version.data.code_revision ? (
                <Field label="revision" value={version.data.code_revision} />
              ) : null}
              {/* The question the two lines above cannot answer between them: how old is
                  what is deployed? A revision and a semver are each only comparable against
                  something else — a checkout, a changelog — while a date reads on its own,
                  which is what someone about to press Upgrade is actually asking. Same rule
                  as the lines above for an absent value. */}
              {version.data.built_at ? (
                <Field label="built" value={builtLine(version.data.built_at)} />
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
          {/* What the tag points at in the registry right now. Beside `running` because the
              two are only useful as a pair -- one digest alone says nothing a reader can act
              on, and the verdict below puts the comparison in words. Absent when the registry
              did not answer, which the verdict already says.

              The date is the same question `built` answers above, asked of the image this
              service is *not* executing: two differing digests say the published bytes are
              other bytes, never which of them is newer. It comes from the image's own OCI
              label, so it is appended to this line rather than given one of its own -- it
              describes the digest beside it, and can be missing while the digest is not. */}
          {info?.registry_digest ? (
            <Field
              label="available"
              value={
                info.registry_digest
                + (info.registry_built_at ? ` · built ${builtLine(info.registry_built_at)}` : '')
              }
            />
          ) : null}

          {/* The roll is offered only where it exists. On a local service, or a cluster
              service running outside the cluster, there is no Deployment of its own to
              roll — so there is no button, and the reason is a caption rather than a
              disabled control that invites clicking. */}
          {info?.supported ? (
            <Stack direction="row" spacing={1} alignItems="center" sx={{ pt: 1 }}>
              {/* Live only where there is something to roll onto — greyed rather than gone,
                  so the page keeps the same shape in both states and the control stays where
                  the operator last saw it. Note the test is `=== false` and not `!== true`: a
                  null `upgrade_available` means the registry did not answer, which is not the
                  same as up to date, and greying the button there would refuse an operator who
                  knows a newer image is published. The caption beside it says which it is. */}
              <Button
                variant="contained"
                size="small"
                disabled={rolling || info.upgrade_available === false}
                onClick={() => roll(info)}
              >
                {rolling ? 'Upgrading…' : 'Upgrade'}
              </Button>
              {/* Beside the button it re-arms: what it fetches is the answer that decides
                  whether that button is live, so a stale "no upgrade available" is one click
                  from being re-asked rather than a minute of waiting for the poll. */}
              <Tooltip title="Check again for a newer version">
                {/* Kept enabled while it runs so the tooltip stays reachable; a second click
                    is a no-op refetch. */}
                <IconButton size="small" aria-label="Reload service info" onClick={refreshService}>
                  {refreshing
                    ? <CircularProgress size={18} />
                    : <RefreshRoundedIcon fontSize="small" />}
                </IconButton>
              </Tooltip>
              <Typography variant="caption" color="text.secondary" sx={{ pl: 1 }}>
                {rolling
                  ? 'waiting for the new version to take over…'
                  : upgradeVerdict(info)}
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
                ? 'Upgraded — this page is still on the old version. Reload to finish.'
                : `Upgraded. Reloading in ${reloadIn}s…`}
            </Alert>
          ) : null}
          {rollNote ? (
            <Alert severity="info" sx={{ mt: 1 }} onClose={() => setRollNote(null)}>
              {rollNote}
            </Alert>
          ) : null}
        </Stack>
      </Paper>

      <Paper variant="outlined" sx={{ p: 2 }}>
        <BrowserPreferences />
      </Paper>

      <CollapsibleBox
        open={configOpen}
        onToggle={() => setConfigOpen((v) => !v)}
        title={
          <Tooltip
            placement="right"
            title={
              'What this service is running with, read back out of its own environment. '
              + 'Credentials are reported as set or not set; their values never leave the '
              + 'service.'
            }
          >
            <span>Service configuration</span>
          </Tooltip>
        }
      >
        {/* Mounted only while open, so the request is made the first time somebody asks
            for it rather than on every visit to this page. */}
        {configOpen ? <ServiceConfigPanel /> : null}
      </CollapsibleBox>

      <CollapsibleBox
        open={eventsOpen}
        onToggle={() => setEventsOpen((v) => !v)}
        title={
          <Tooltip
            placement="right"
            title={
              'What this service did, kept across restarts — unlike the log below it, which '
              + 'is this process\u2019s recent output and dies with the pod. Refusals mostly: '
              + 'a reason that would otherwise exist only in the reply that carried it.'
            }
          >
            <span>Service events</span>
          </Tooltip>
        }
      >
        {/* Mounted only while open, like the panels around it: the request is made the first
            time somebody asks rather than on every visit. */}
        {eventsOpen ? <ServiceEventsPanel /> : null}
      </CollapsibleBox>

      <CollapsibleBox
        open={mcpOpen}
        onToggle={() => setMcpOpen((v) => !v)}
        title={
          <Tooltip
            placement="right"
            title={
              'Every MCP tool call this deployment served \u2014 the ranking, and the calls '
              + 'behind it with what each was given and what it answered, truncated to a few '
              + 'lines. Kept in the central index, so it outlives this process but not the '
              + 'results store.'
            }
          >
            <span>MCP tools</span>
          </Tooltip>
        }
      >
        {/* Mounted only while open, like the panels around it. */}
        {mcpOpen ? <McpToolsPanel active={active} /> : null}
      </CollapsibleBox>

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


/**
 * Preferences that belong to the browser rather than to the service.
 *
 * Its own paper, deliberately not a row in ServiceConfigPanel. That panel reports one shared
 * environment read back out of the service, and this instance is used by several people behind
 * one token -- so a browser-local switch sitting among those rows would read as something one
 * person can change for everybody. The heading is what keeps the two apart, and the caption
 * says the same thing in words for anyone who reads the switch before the heading.
 *
 * This is also the only way back once the one-time ask has been answered (NotificationAsk),
 * which is why it exists at all rather than the ask standing alone.
 */
function BrowserPreferences() {
  const [on, setOn] = useState(browserNotify.optedIn)
  const [denied, setDenied] = useState(() => browserNotify.permission() === 'denied')
  const supported = browserNotify.supported()

  const toggle = async () => {
    if (on) {
      browserNotify.setOptedIn(false)
      setOn(false)
      return
    }
    // A click, so the gesture requirement is met: Firefox and Safari show no prompt at all for
    // a request that no user action stands behind.
    const result = await browserNotify.requestPermission()
    if (result !== 'granted') {
      // A denial is sticky and cannot be re-prompted, so say so rather than leaving a switch
      // that silently refuses to stay on.
      setDenied(result === 'denied')
      browserNotify.setOptedIn(false)
      return
    }
    browserNotify.setOptedIn(true)
    setOn(true)
    setDenied(false)
  }

  return (
    <Stack spacing={1}>
      <Typography variant="subtitle2">This browser</Typography>
      {supported ? (
        <>
          <Tooltip
            placement="right"
            enterDelay={PREF_TIP_MS}
            title={
              'Shown only while this tab is in the background. Applies to this browser only — '
              + 'everyone signed in to this service keeps their own setting, and this is not '
              + 'a service setting.'
            }
          >
            {/* A disabled switch reports no events, so the tooltip needs a wrapper to hang on. */}
            <Box sx={{ alignSelf: 'flex-start' }}>
              <FormControlLabel
                control={<Switch size="small" checked={on} disabled={denied} onChange={() => void toggle()} />}
                label={
                  <Typography variant="body2">Notify me when a campaign finishes</Typography>
                }
                sx={{ ml: 0 }}
              />
            </Box>
          </Tooltip>
          {/* The denial stays in print. It is not a caveat about the switch, it is the reason
              the switch is dead, and an explanation for a disabled control that has to be
              hunted for by hovering is not an explanation. */}
          {denied ? (
            <Typography variant="caption" color="text.secondary">
              This browser has blocked notifications for this site; re-allow them in its site
              settings.
            </Typography>
          ) : null}
        </>
      ) : (
        <Typography variant="caption" color="text.secondary">
          This browser has no notification support, so a campaign ending can only be announced
          inside the page.
        </Typography>
      )}
    </Stack>
  )
}
