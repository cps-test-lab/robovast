import { useEffect, useState, type ReactNode } from 'react'
import StopRoundedIcon from '@mui/icons-material/StopRounded'
import Box from '@mui/material/Box'
import Chip from '@mui/material/Chip'
import IconButton from '@mui/material/IconButton'
import Stack from '@mui/material/Stack'
import Tab from '@mui/material/Tab'
import Tabs from '@mui/material/Tabs'
import { useTheme, type Theme } from '@mui/material/styles'
import Tooltip from '@mui/material/Tooltip'
import Typography from '@mui/material/Typography'
import {
  robovast,
  type BudgetItem,
  isTerminalPhase,
  type JobCounts,
  type JobSummary,
  type ListJobsResponse,
  readUploadProgress,
  type Status,
  type UploadProgress,
} from '@/lib/robovastClient'
import {
  batchesBudget,
  estimateBatchesEtaSeconds,
  isBatchesBudget,
  estimateEtaSeconds,
  criterionOp,
  finishedRuns,
  hasDrawableFloor,
  noResultRuns,
  ringBudget,
} from '@/lib/eta'
import { isCalibrationJob, isPostprocessingJob, nonRunsFirst } from '@/lib/jobKind'
import { formatBytes, formatDuration } from '@/lib/format'
import { runMeterFailedText, runMeterSegments, runMeterText } from '@/lib/runMeter'
import { useQuery } from '@tanstack/react-query'
import { formatLocalClock, formatLocalTime } from '@/lib/time'
import { NEUTRAL, withAlpha } from '@/colors'
import { BatchObjectiveChart } from './BatchObjectiveChart'
import { CollapsibleBox } from './CollapsibleBox'
import { DetailsBox } from './DetailsBox'
import { FactRows, HoverFacts } from './HoverFacts'
import { LogPanel } from './LogPanel'
import { MeterBar } from './MeterBar'

// The upload-to-share bar, shown only while the campaign is in the `sharing` phase.
//
// The bar measures the CAMPAIGN bytes fed into the archive, not the bytes on the wire:
// the archive is gzipped on the fly, so its compressed length is unknown until the last
// byte and there is no wire denominator to divide by. `sent` is reported beside it as
// text, which is also why the two numbers disagree — that difference is the compression
// ratio, not an error.
//
// With no total (a provider or lane that cannot say), the bar goes indeterminate rather
// than showing a made-up 0%: a bar pinned at zero through a multi-hour upload is the
// exact failure this replaced.
function UploadSection({ upload }: { upload: UploadProgress }) {
  const { percent, sourceDone, sourceTotal, sent, rate } = upload
  const meta = [
    sourceTotal > 0 ? `${formatBytes(sourceDone)} / ${formatBytes(sourceTotal)}` : null,
    `${formatBytes(sent)} sent`,
    rate != null && rate > 0 ? `${formatBytes(rate)}/s` : null,
    percent != null && rate != null && rate > 0 && sourceTotal > sourceDone
      ? `~${formatDuration(estimateUploadEtaSeconds(upload))} left`
      : null,
  ]
    .filter(Boolean)
    .join(' · ')
  return (
    <Box>
      <Stack direction="row" justifyContent="space-between">
        <Typography variant="caption" color="text.secondary">
          upload to share
        </Typography>
        <Typography variant="caption" color="text.secondary">
          {meta}
        </Typography>
      </Stack>
      {percent == null ? (
        <MeterBar segments={[{ fraction: 1, color: 'info.main', striped: true }]} />
      ) : (
        <MeterBar fraction={percent / 100} color="info.main" text={`${percent.toFixed(1)}%`} />
      )}
    </Box>
  )
}

// Time left on the upload, from the rate the wire is actually moving at. The remaining
// SOURCE bytes are scaled by the compression ratio observed so far (sent/done), because
// `rate` counts compressed bytes and the remainder is counted uncompressed — dividing one
// by the other directly would over-estimate the wait by exactly that ratio.
function estimateUploadEtaSeconds(upload: UploadProgress): number {
  const { sourceDone, sourceTotal, sent, rate } = upload
  const ratio = sourceDone > 0 ? sent / sourceDone : 1
  return Math.max(0, ((sourceTotal - sourceDone) * ratio) / (rate ?? 1))
}

/** The run meter, short enough to sit in a campaign card's header row.
 *
 *  The same bar the open card draws full width -- same segments -- shrunk to a fixed column and
 *  carrying its label inside the track instead of above it, so a COLLAPSED campaign says what its
 *  runs did without the row growing a second line.
 *
 *  Kept in this file rather than beside the card that uses it, deliberately: it is the same meter
 *  as the one above and a change to either must be made looking at the other.
 *
 *  The track's label follows the campaign's tense (see `runMeterText`): the share done while it
 *  runs, the size of the campaign once it is over. Beside it, what failed, in that same unit and
 *  only when there is something -- `28.3% (3.2% x)` on a running campaign, `156 (5 failed)` on a
 *  finished one (see `runMeterFailedText`). One color for the whole label: the segments under it
 *  already carry the red, and a label in two colors reads as two labels. The rest -- the counts,
 *  the two failure axes apart, the wall clock -- is on the hover: a bar 140px wide holds one short label, and the rest is asked of a
 *  single row.
 */
export function MiniRunMeter({
  status,
  campaignId,
  counts,
  started,
  finished,
  width = 140,
}: {
  status: Status
  /** Needed only to offer a search campaign's rounds ring; omitted → no ring. */
  campaignId?: string
  counts?: JobCounts
  /** ISO timestamps from the campaign listing, for the hover. Omitted → those rows are dropped. */
  started?: string | null
  finished?: string | null
  width?: number
}) {
  const { runs } = status
  const done = finishedRuns(status, counts)
  const succeeded = Math.max(0, runs.completed - runs.failed)
  const noResult = noResultRuns(status, counts)
  const failedText = runMeterFailedText(status, counts)
  return (
    // The ring's slot is reserved whether or not there is a ring, so this whole group is a
    // constant width. Without that, a search campaign's row pushed the time cell beside it 32px
    // left of every other row -- and that cell is a column readers scan down. The reserved space
    // costs nothing visible: it lands between the time and the meter, where it reads as gap.
    <Stack direction="row" spacing={0.75} alignItems="center" sx={{ flexShrink: 0 }}>
      <Box sx={{ width: RING_SIZE, flexShrink: 0 }}>
        {campaignId && status.mode === 'search' ? (
          <SearchRing campaignId={campaignId} status={status} />
        ) : null}
      </Box>
    <HoverFacts
      title="runs"
      facts={[
        { label: 'done', value: `${done} of ${runs.total}` },
        // Zero is dropped by HoverFacts, which is the point: a campaign with no failures says
        // nothing about failures rather than printing a reassuring `0`.
        { label: 'passed', value: succeeded || null },
        { label: 'failed', value: runs.failed || null },
        { label: 'no result', value: noResult || null },
        { label: 'running', value: counts?.running || null },
        { label: 'started', value: started ? formatLocalTime(started) : null },
        { label: 'finished', value: finished ? formatLocalTime(finished) : null },
      ].map((f) => ({ ...f, value: f.value == null ? null : String(f.value) }))}
    >
      <Box sx={{ width, flexShrink: 0, cursor: 'help' }}>
        <MeterBar
          height={16}
          segments={runMeterSegments(status, counts)}
          // `text.primary`, not MeterBar's default secondary: that default was chosen for a bar
          // with nothing behind the label, and here the label sits over filled segments.
          text={
            <Box component="span" sx={{ color: 'text.primary', fontVariantNumeric: 'tabular-nums' }}>
              {runMeterText(status, counts)}
              {failedText ? ` (${failedText})` : ''}
            </Box>
          }
        />
      </Box>
    </HoverFacts>
    </Stack>
  )
}

/** What a SEARCH campaign has spent, as a ring, for a collapsed campaign card.
 *
 *  The run meter beside it answers a smaller question here than it does for a batch campaign: a
 *  search's `runs` counters are scoped to the CURRENT BATCH (see lib/eta.ts), so on a finished
 *  search they describe its last round, not the campaign. The ring is the campaign-scope figure.
 *
 *  It measures whichever declared budget is CLOSEST TO EXHAUSTING (`ringBudget`), not the `batches`
 *  row: a `batches` criterion is one way to bound a search, and hanging the ring on it left every
 *  search bounded by runs, time or evaluations with no arc at all -- an unfilled circle beside a
 *  campaign 67% through its run budget. Six of the eight shipped `nav_search` examples are in that
 *  position, and the budget those campaigns declare is deliberate.
 *
 *  The hole carries the SHARE as a percent rather than a count, because the unit varies: `120` runs,
 *  `4` rounds and `1800` seconds cannot share one 18px hole, and a percent is at most four
 *  characters whatever the criterion is. The counts, their unit and their scope are on the hover.
 *
 *  With nothing FRACTIONABLE declared there is still no denominator and none is invented: a search
 *  bounded only by convergence (`target_objective`, `no_improvement`, `metric`) draws the bare track
 *  with its round count inside, exactly as before. A filled ring there would claim a limit the
 *  campaign never declared; a percent there would be a share of nothing.
 */
function SearchRing({
  campaignId,
  status,
  size = RING_SIZE,
}: {
  campaignId: string
  status: Status
  size?: number
}) {
  const theme = useTheme()
  const done = status.batches_done ?? 0
  const bound = ringBudget(status)
  const share = bound ? bound.share * 100 : 0
  const radius = RING.radius
  const stroke = RING.stroke
  return (
    <Tooltip
      placement="top"
      // The chart inside needs more than a tooltip's default 300px, and it is the reason to hover.
      slotProps={{ tooltip: { sx: { maxWidth: 'none' } } }}
      title={<SearchHover campaignId={campaignId} status={status} />}
    >
      <Box sx={{ position: 'relative', width: size, height: size, flexShrink: 0, cursor: 'help' }}>
        <Box component="svg" viewBox={`0 0 ${RING.viewBox} ${RING.viewBox}`} sx={{ width: size, height: size, display: 'block' }}>
          {/* Concrete theme values, not palette paths: `sx` does not resolve those for SVG
              presentation properties — see the same note on the Details panel's ring. */}
          <Box
            component="circle" cx="20" cy="20" r={radius}
            sx={{ fill: 'none', stroke: theme.palette.action.hover, strokeWidth: stroke }}
          />
          {bound ? (
            <Box
              component="circle" cx="20" cy="20" r={radius}
              // -90deg so the first round starts at twelve o'clock, where a reader expects it.
              transform="rotate(-90 20 20)"
              sx={{
                fill: 'none',
                // The arc's LENGTH is the budget spent; its COLOUR is the search's state --
                // see `ringArcRole`.
                stroke: ringArcPalette(theme, ringArcRole(status)),
                strokeWidth: stroke,
                strokeDasharray: `${share} ${100 - share}`,
              }}
            />
          ) : null}
          {/* An opaque field for the label, so it never borrows the arc's colour -- see RING. */}
          <Box
            component="circle" cx="20" cy="20" r={RING.labelField}
            sx={{ fill: theme.palette.background.paper, stroke: 'none' }}
          />
        </Box>
        <Typography
          variant="caption"
          sx={{
            position: 'absolute', inset: 0, display: 'flex', alignItems: 'center',
            justifyContent: 'center', fontSize: RING.fontSize, fontWeight: 600,
            // Tabular figures because this number changes under a fixed-width mark: proportional
            // digits make the label shift inside the hole as it climbs through 1%..100%.
            fontVariantNumeric: 'tabular-nums',
          }}
        >
          {ringLabel(bound ? bound.share : null, done)}
        </Typography>
      </Box>
    </Tooltip>
  )
}

/** The role the ring's arc paints in: what the search IS, not how far it got.
 *
 *  While the search runs the arc is `info` -- the same blue every in-progress indicator in the
 *  scheme uses -- so a moving ring reads as "under way" and nothing more. Amber stays reserved for
 *  the one thing about a live search worth flagging: a stopping criterion about to fire, where
 *  "67% spent" implies a third left to run that will not be run. That verdict is the service's
 *  (`stopping_soon_report`), never re-derived here, and colour is not carrying it alone -- the
 *  time cell in the same row reads "may stop early" and the hover carries the criterion's sentence.
 *
 *  Once the campaign is over the arc becomes the OUTCOME, which is what a finished card is read
 *  for: green when the search finished and found an objective, red when it failed, was stopped or
 *  crashed, neutral when it ended without ever scoring one (`unknown` -- a driver lost to a service
 *  restart -- included: that is an absent verdict, not a failed one). `stopping_soon` is dropped
 *  on a terminal campaign: whether it *was about to* stop early is stale the moment it has stopped.
 */
export type RingArcRole = 'info' | 'warning' | 'success' | 'error' | 'neutral'

export function ringArcRole(status: Status): RingArcRole {
  if (!isTerminalPhase(status.phase)) return status.stopping_soon === true ? 'warning' : 'info'
  if (status.phase === 'finished') return status.best_objective != null ? 'success' : 'neutral'
  if (status.phase === 'unknown') return 'neutral'
  return 'error'
}

/** The role as a concrete colour: SVG presentation properties do not resolve palette paths. */
function ringArcPalette(theme: Theme, role: RingArcRole): string {
  return role === 'neutral' ? NEUTRAL : theme.palette[role].main
}

/** How a budget row reads on the ring's hover: its unit, its position and its scope.
 *
 *  `time` is a duration and every other kind is a count, which is the only per-kind formatting
 *  here. The SCOPE on `runs` is not decoration: the meter immediately right of the ring shows the
 *  runs of the CURRENT BATCH (`15 of 24`) while a runs budget counts the campaign's (`120 of 180`),
 *  so without saying which is which the row shows two run figures that look like they disagree.
 *  The CLI labels its own bar `Runs (this batch)` for the same reason. */
function budgetFactValue(b: BudgetItem): string {
  if (b.current == null) return `— of ${b.limit}`
  if (b.kind === 'time') return `${formatDuration(b.current)} of ${formatDuration(b.limit)}`
  return `${b.current} of ${b.limit}`
}

/** The label a budget row wears on the hover: its own label, plus what the figure is scoped to and
 *  a mark when it has already fired. `done` is on the wire and is a fact rather than a proximity,
 *  so it is stated in text -- a colour alone would leave the reader to decode it. */
function budgetFactLabel(b: BudgetItem): string {
  const scope = b.kind === 'runs' ? ' (campaign)' : ''
  return `${b.label}${scope}${b.done ? ' ✓' : ''}`
}

/** The hover behind the ring: the rounds, what bounds the search, and the trajectory.
 *
 *  A separate component because that is what makes the chart cheap. MUI mounts a tooltip's title
 *  only while it is open, so the `/search/history` request below is issued on the first hover and
 *  never on a page of collapsed cards nobody pointed at. Same query key and staleTime as the open
 *  card's objective section, so hovering a card you later expand costs one request between them.
 *
 *  The ROUNDS lead, and they are here unconditionally rather than in the ring's hole, which
 *  carries the budget share -- rounds are the fact the ring exists to surface, since a
 *  search's run counters describe one batch. Below them, the binding criterion first (it is what
 *  the arc measures), then every other declared criterion including the `stopping` ones: any of
 *  them may be what actually ends this campaign, and the arc can only show one.
 */
function SearchHover({ campaignId, status }: { campaignId: string; status: Status }) {
  const done = status.batches_done ?? 0
  const bound = ringBudget(status)
  const history = useQuery({
    queryKey: ['search-history', campaignId, done],
    queryFn: () => robovast.getSearchHistory(campaignId),
    retry: false,
    staleTime: Infinity,
  })
  // The binding row first, then the rest in declaration order. Compared by identity, not by label:
  // two criteria can share a label (a metric named after the objective) and dropping the wrong one
  // would hide a criterion that is about to fire.
  const others = status.budget.filter((b) => b !== bound?.item)
  return (
    <Box sx={{ width: 260 }}>
      <FactRows
        title="search"
        facts={[
          // First, and it names the number in the ring's hole. The hole carries no `%` (it does
          // not fit -- see LABEL_FIELD), so a bare `67` there could be read as a count, which is
          // exactly what it used to be before the ring measured a budget. This row is where the
          // unit lives. Absent when nothing bounds the search, and then the hole shows the round
          // count that the next row names instead.
          {
            label: 'budget spent',
            value: bound ? `${Math.round(bound.share * 100)}% of ${bound.item.label}` : null,
          },
          { label: 'rounds', value: String(done) },
          // A criterion whose position is not yet known keeps its row and shows `— of N`: the
          // criterion demonstrably exists (the campaign declared it), which is a different fact
          // from the one HoverFacts drops, where the panel never had the value at all. A
          // `target_objective` before the first result is exactly this case.
          ...(bound
            ? [{ label: budgetFactLabel(bound.item), value: budgetFactValue(bound.item) }]
            : []),
          ...others.map((b) => ({ label: budgetFactLabel(b), value: budgetFactValue(b) })),
          {
            label: 'best objective',
            value: status.best_objective != null ? String(status.best_objective) : null,
          },
          // Why the arc went amber. Dropped when absent, which is HoverFacts' rule and the
          // right one here: a search that is not near a stopping criterion says nothing about
          // one rather than reassuring the reader about it.
          { label: 'stopping soon', value: status.stopping_reason ?? null },
        ]}
      />
      {!bound ? (
        // Not a failure and not a warning: a search may legitimately be bounded only by
        // convergence. It is said because an unfilled ring otherwise reads as "0% spent".
        <Typography sx={{ fontSize: 11, opacity: 0.7, mb: 0.5 }}>
          no budget bounds this search — it stops on convergence
        </Typography>
      ) : null}
      {history.isLoading ? (
        <Typography sx={{ fontSize: 11, opacity: 0.7 }}>reading the search's rounds…</Typography>
      ) : history.isError ? (
        <Typography sx={{ fontSize: 11, opacity: 0.7 }}>
          no objective history ({(history.error as Error)?.message})
        </Typography>
      ) : history.data ? (
        <BatchObjectiveChart history={history.data} height={96} />
      ) : null}
    </Box>
  )
}

// Job states that mean the job is over, so the live view stops showing a row for it. A
// *failed* job is deliberately not here: a failure is the thing the reader came to look at.
const DONE_JOB_STATUSES: ReadonlySet<string> = new Set(['completed', 'killed'])

/** The card body's tabs. `jobs` and `details` are mutually exclusive by phase -- a running
 *  campaign has no measurements yet, a finished one has no live jobs -- so only one of them is
 *  ever offered beside `log`. */
type CardTab = 'jobs' | 'details' | 'log'

/** The ring's geometry, in one place because these five numbers have to agree.
 *
 *  They did not. The label was sized independently of the band, and `67%` at 10px is about
 *  20px against a hole of `2 * (radius - stroke/2) * size/viewBox` = under 17px -- so the digits'
 *  outer edges sat on a saturated arc and lost their contrast against it, which is what a reader
 *  reported as the colour being too bright. The fix is arithmetic, not palette, and
 *  `ringLabelWidth` below is the assertion that keeps it so.
 *
 *  `labelField` is the opaque disc the label sits on. At these values it just fills the hole
 *  rather than covering any of the band, so it is insurance against a translucent surface or an
 *  antialiased edge rather than the thing that buys the contrast.
 *
 *  No `%` in the label, and that is a consequence of the same arithmetic rather than a
 *  preference: `%` is the widest glyph in the string at roughly 0.85em, so `100%` wants ~24px at
 *  9px type and does not fit a 26px ring at any legible size. The value is still a percent and
 *  the hover names it as one. */
const RING = {
  size: 26,
  viewBox: 40,
  /** Circumference 100, so a dash length IS a percentage. */
  radius: 15.9155,
  stroke: 6,
  /** Radius of the opaque disc the label sits on. Deliberately INSIDE the band rather than at
   *  its inner edge: the disc is what defines the label's field, so widening it buys room for
   *  the label without thinning the stroke or shrinking the type. The band still shows
   *  `radius + stroke/2 - labelField` of itself, which at these values is 2.4px -- a normal
   *  thin donut, and filled and track are clipped equally so the contrast between them is
   *  untouched. */
  labelField: 15.2,
  fontSize: 9,
} as const

/** The ring's diameter, and so the width its slot reserves on every row. */
const RING_SIZE = RING.size

/** What the hole shows once the budget is spent.
 *
 *  A tick rather than `100%`, and that is what lets the rest keep its `%` sign: three digits plus
 *  the sign wants about 24px and does not fit a 26px ring at any legible size, and it was the
 *  reason the sign was dropped from every value. Replacing only the one value that does not fit
 *  costs one glyph and buys the sign back for the other hundred.
 *
 *  It also reads better than the number it replaces: at 100% the binding budget is exhausted, so
 *  "done" is what the reader wants, not an arithmetic identity. */
const RING_DONE = '\u2713'

/** The hole's label for a share in [0,1], or the round count when nothing bounds the search.
 *
 *  Only an EXACT full share earns the tick. `Math.round` would hand it to 99.6%, claiming a
 *  budget spent while a run of it remains -- so short of exhaustion the number is capped at 99
 *  and understates by a fraction, which is the same direction `ringBudget` clamps in. */
export function ringLabel(share: number | null, rounds: number): string {
  if (share == null) return String(rounds)
  if (share >= 1) return RING_DONE
  return `${Math.min(99, Math.round(share * 100))}%`
}

/** How wide the label's field is, in rendered pixels: the opaque disc, not the hole.
 *
 *  The disc is what the label may occupy, and it is wider than the hole on purpose -- see
 *  `RING.labelField`. Measuring against the hole instead is what made the first attempt at this
 *  shrink the type and drop the `%` sign when neither was necessary. */
export function ringLabelField(): number {
  return 2 * RING.labelField * (RING.size / RING.viewBox)
}

/** Roughly how wide *label* renders at the ring's type size.
 *
 *  An estimate, deliberately: measuring text needs a laid-out document, and the thing worth
 *  guarding is not the exact pixel but that nobody re-sizes the label past its hole again. The
 *  per-character ems are conservative for a semibold tabular sans -- digits are set to one
 *  width by `tabular-nums`, and `%` is the widest glyph the string could contain. */
export function ringLabelWidth(label: string): number {
  const em = [...label].reduce(
    (w, ch) => w + (ch === '%' ? 0.85 : ch === RING_DONE ? 1.0 : 0.6), 0)
  return em * RING.fontSize
}

// Renders one campaign's live Status — the browser analog of what `vast cluster monitor` prints:
// phase, run-level progress within the current batch, batch counter, and each budget/stopping
// criterion. Purely presentational; the caller supplies the (polled) Status and, optionally, the
// (polled) live jobs listing.

export function StatusView({
  status,
  campaignId,
  jobs,
  hideLog = false,
  liveOnly = false,
  newest = true,
  quotaCpu,
  postprocessed = false,
  resultsBytes,
  onStopJob,
  stoppingJob,
}: {
  status: Status
  // The campaign this status belongs to. Passed in because the caller already knows it
  // and `status.campaign_id` does not: the controller fills that field, and a campaign
  // waiting for its image build has no controller yet — which would otherwise leave the log
  // button off exactly the card that has nothing else to show. Falls back to the status
  // for any caller that only holds one.
  campaignId?: string
  jobs?: ListJobsResponse
  // The Launcher hides the campaign log — it's a launch confirmation, not a viewer;
  // the full log lives in Monitor.
  hideLog?: boolean
  // Monitor cares only about jobs still meaningful right now: it drops completed ones
  // from both the count summary and the jobs list (the Launcher lists everything).
  liveOnly?: boolean
  // The top card in Monitor's newest-first campaign list — see FailureBox. Defaults to
  // true so the Launcher and other single-campaign callers keep the box open.
  newest?: boolean
  // Lane CPU capacity, for Details' "jobs in flight" estimate. Omitted → not shown.
  quotaCpu?: number | null
  // Whether the metric tables exist yet -- the Details panel re-queries when this flips, since a
  // campaign is postprocessed a few minutes after it finishes.
  postprocessed?: boolean
  // Total bytes the campaign's results occupy, measured once when it ended. `null`/omitted
  // means not recorded -- a campaign that ended before this was measured, or one still
  // running -- and Details then shows no size rather than "0 B".
  resultsBytes?: number | null
  // Offer each running job a Stop button. Omitted → no buttons, which is what the Launcher
  // wants: this view stays presentational and the caller owns the confirm + the mutation,
  // because it also owns the jobs query that has to be invalidated afterwards.
  onStopJob?: (job: JobSummary) => void
  // The job a stop is currently in flight for, so its button can disable itself rather than
  // inviting a second click at a job that is already going away.
  stoppingJob?: string | null
}) {
  const { runs, budget } = status
  const cid = campaignId ?? status.campaign_id
  const terminal = isTerminalPhase(status.phase)
  const counts = jobs?.counts
  // The jobs list's expansion state, kept here rather than in JobsSection / JobRow
  // because both of those unmount underneath the reader: the section whenever the live
  // set momentarily empties (local runs are sequential, so between every pair of runs),
  // a row the instant its job completes and the `liveOnly` filter below drops it —
  // which threw away the log the reader was in the middle of. `expandedJobs` also feeds
  // that filter, so a job whose log is open survives its own completion until collapsed.
  const [expandedJobs, setExpandedJobs] = useState<Set<string>>(() => new Set())
  // Which tab is showing. Local state per card, deliberately not the URL: there are as many of
  // these as there are campaigns on the page, and the hash carries one view's selection, not a
  // per-card one (the Explorer's tab is in the URL because there is exactly one Explorer).
  //
  // Seeded from whether the campaign is over AT FIRST RENDER rather than tracking `terminal`, so
  // a campaign that finishes while it is open does not swap the tab under the reader; the effect
  // below moves it only when the tab it is on stops existing.
  const [tab, setTab] = useState<CardTab>(() => (isTerminalPhase(status.phase) ? 'details' : 'jobs'))
  useEffect(() => {
    setTab((t) => (t === 'jobs' && terminal ? 'details' : t === 'details' && !terminal ? 'jobs' : t))
  }, [terminal])
  const toggleJob = (jobName: string) =>
    setExpandedJobs((prev) => {
      const next = new Set(prev)
      if (!next.delete(jobName)) next.add(jobName)
      return next
    })
  // The jobs list mirrors what actually exists on the cluster — the same set `k9s`
  // shows: jobs that own a pod. A `waiting` job is the un-admitted backlog: it has
  // no pod, nothing distinguishes one queued job from the next, and there is no log to
  // expand (the row's only affordance). Whole batches sit there at launch, so listing
  // them buries the handful of jobs that are really doing something. The backlog is
  // reported by the `waiting N` counter instead, which is what makes it legible anyway.
  // `killed` is dropped from the live view for the same reason as `completed`: the job is
  // over. Kept, it would show as `running` — its `test.xml` never arrives —
  // so the Jobs list would carry a row, and a Stop button, on a job that is already dead.
  const shownJobs = jobs?.jobs.filter(
    (j) =>
      j.status !== 'waiting' &&
      (!liveOnly || !DONE_JOB_STATUSES.has(j.status) || expandedJobs.has(j.job_name)),
  )
  // Live-view count summary: every non-completed state that is present. `waiting` is
  // the only way the queued backlog shows up at all now that it has no rows.
  const liveCountText = [
    counts && counts.running > 0 ? `running ${counts.running}` : null,
    counts && counts.pending > 0 ? `pending ${counts.pending}` : null,
    counts && counts.waiting > 0 ? `waiting ${counts.waiting}` : null,
    counts && counts.failed > 0 ? `failed ${counts.failed}` : null,
    counts && counts.blocked > 0 ? `blocked ${counts.blocked}` : null,
    // Beside the run states rather than among them, because it is not one of them. A batch
    // that has started nothing because it is still measuring its nodes otherwise reads as
    // `waiting N` with nothing anywhere saying what it is waiting for.
    counts && counts.calibration > 0 ? `calibrating ${counts.calibration}` : null,
    // Beside them for the same reason, and with the opposite problem to solve: by the time
    // the conversion runs every run state is zero, so a summary without this is an empty
    // line on a campaign that is still working.
    counts && counts.postprocessing > 0 ? 'converting rosbags' : null,
  ]
    .filter(Boolean)
    .join(' · ')
  // This is a progress view, so a run is "done" whether it produced a result or
  // delivered none — both have reached a terminal state. Counting the resultless ones
  // toward the numerator is what makes `done/total` reach total when the batch is over,
  // and what keeps this number, the meter and the estimate the same number. Runs that
  // produced a *failing* result are already in `completed`.
  const done = finishedRuns(status, counts)
  const etaSeconds = estimateEtaSeconds(status, counts, terminal)
  // Upload-to-share is the one phase with real progress that the run meter cannot show:
  // the runs are over and their bar is frozen, while gigabytes move to somebody else's
  // storage. Rendered first because during `sharing` it is the only thing happening.
  const upload = status.phase === 'sharing' ? readUploadProgress(status) : null
  return (
    <Stack spacing={1.5}>
      {upload ? <UploadSection upload={upload} /> : null}
      <Box>
        <Stack direction="row" justifyContent="space-between">
          {/* Just "runs", and no batch counter -- `batch 2 (3 done)` -- riding along: it
              says what the batches bar directly below already shows, and this label sits
              above a bar measuring RUNS, so a batch number on it invites reading the bar as
              batch progress. */}
          <Typography variant="caption" color="text.secondary">
            runs
          </Typography>
          <Typography variant="caption" color="text.secondary">
            {liveOnly
              ? liveCountText
                ? `${liveCountText} · `
                : ''
              : counts && (counts.running > 0 || counts.pending > 0 || counts.waiting > 0)
                ? `running ${counts.running} · pending ${counts.pending}` +
                  (counts.waiting > 0 ? ` · waiting ${counts.waiting}` : '') +
                  ' · '
                : ''}
            {done}/{runs.total}
            {runs.failed > 0 ? (
              <Box component="span" sx={{ color: 'error.main' }}>
                {' · '}
                {runs.failed} failed
              </Box>
            ) : null}
            {runs.no_result > 0 ? (
              <Box component="span" sx={{ color: 'error.main' }}>
                {' · '}
                {runs.no_result} no result
              </Box>
            ) : null}
            {etaSeconds != null
              ? ` · ~${formatDuration(etaSeconds)} left (≈ ${formatLocalClock(etaSeconds)})`
              : ''}
          </Typography>
        </Stack>
        {/* Segments from `lib/runMeter`, not spelled out here: the collapsed campaign card draws
            this same meter short, inside its header row, and a reader who folds a card open to
            look at a red segment is looking at THE SAME BAR. */}
        <MeterBar segments={runMeterSegments(status, counts)} />
      </Box>

      {/* The rounds a search has run, and the objective they moved. Rendered for every
          search rather than off a budget row: a `batches` criterion BOUNDS the rounds, it
          does not create them, so a search bounded by runs or time has both just the same.
          Hanging this on the budget row is what left those campaigns with no batch counter,
          no estimate and no objective chart at all. It sits above the criteria because
          rounds are not one of them. */}
      {cid && status.mode === 'search' ? (
        <ObjectiveSection
          campaignId={cid}
          status={status}
          counts={counts}
          runsEta={etaSeconds}
        />
      ) : null}

      {budget.filter((b) => !isBatchesBudget(b)).map((b, i) => (
        // Every criterion except the batch counter, which the rounds section above owns.
        // These are measured in units nothing here can turn into a duration, so no estimate.
        //
        // Index in the key, not the label alone: two criteria may share a label (a `metric` named
        // after the objective), and keying on the label drops one of the rows.
        <Box key={`${b.label}-${i}`}>
          <Stack direction="row" justifyContent="space-between">
            {/* The criterion as a SENTENCE -- `coverage >= 0.8` -- not a bare `0.1 / 0.8` pair.
                A pair silently implies the comparison is `>=`, so a `metric` written `<=` read
                as 12% of the way to firing when it had already fired. The threshold belongs
                beside the name it is a threshold ON. */}
            <Typography variant="caption" color="text.secondary">
              {b.label} {criterionOp(b)} {b.limit}
              {b.done ? ' ✓' : ''}
            </Typography>
            <Typography variant="caption" color="text.secondary">
              now {b.current == null ? '—' : b.current}
            </Typography>
          </Stack>
          {/* A bar only where the criterion has a FLOOR to measure from -- see hasDrawableFloor.
              The resource caps do, and so does `no_improvement`, counting up from zero to its
              patience. A `metric` and a `target_objective` do not: knowing a metric fires at
              `<= 0.8` says nothing about where it started, and an objective's initial value is
              whatever the first batch measured. Those two carry the comparison in the label
              instead, which is the honest rendering -- publishing `op` made the row readable,
              not the fraction computable. */}
          {hasDrawableFloor(b) ? (
            <MeterBar
              height={10}
              fraction={b.current == null || b.limit <= 0 ? 0 : b.current / b.limit}
              color="secondary.main"
            />
          ) : null}
        </Box>
      ))}

      {status.best_objective != null ? (
        <Typography variant="caption" color="text.secondary">
          best objective: {status.best_objective}
        </Typography>
      ) : null}
      {status.stop ? (
        <Typography variant="caption" color="text.secondary">
          stop: {JSON.stringify(status.stop)}
        </Typography>
      ) : null}
      {status.error ? <FailureBox error={status.error} defaultOpen={newest} /> : null}
      {/* Nothing to show means no affordance: a "Show jobs (0)" button opens onto an
          empty list, so it is only noise. The section may therefore come and go as the
          live set empties and refills between runs, which is why neither piece of its
          expansion state lives inside it — see jobsOpen / expandedJobs. */}
      {/* Everything above is PROGRESS and stays visible whichever tab is showing. Below it, the
          two things a reader digs into. They were three foldable boxes inside an already-folded
          card, so what a campaign cost, and what it printed, were each two clicks deep.

          Which two tabs depends on what the campaign is, because a running campaign and a
          finished one are dug into for different reasons: one is being watched (which jobs are
          up, what is the log saying), the other reviewed (what did it cost). Details is not
          offered while running at all -- its numbers come from the `resource_usage` table, which
          postprocessing writes, so there is provably nothing to show.

          The panel is a ternary, not two hidden divs: an unselected Log tab that stayed mounted
          would hold its EventSource open invisibly. The cost is that switching back re-opens the
          stream; the jobs' expansion state survives because StatusView owns it. */}
      {cid && !hideLog ? (
        <Box>
          <Tabs
            value={tab}
            onChange={(_e, v) => setTab(v as CardTab)}
            sx={{ minHeight: 36, '& .MuiTab-root': { minHeight: 36, py: 0 } }}
          >
            {terminal ? (
              <Tab value="details" label="Details" />
            ) : (
              // The count is on the label because it is the one number that says whether the tab
              // is worth opening, and a tab nobody opens should not have to be opened to say so.
              <Tab value="jobs" label={`Jobs${shownJobs?.length ? ` (${shownJobs.length})` : ''}`} />
            )}
            <Tab value="log" label="Log" />
          </Tabs>
          <Box sx={{ borderTop: 1, borderColor: 'divider' }}>
            {tab === 'log' ? (
              <CampaignLog campaignId={cid} />
            ) : tab === 'details' ? (
              <DetailsBox
                campaignId={cid}
                quotaCpu={quotaCpu}
                postprocessed={postprocessed}
                resultsBytes={resultsBytes}
                selected
              />
            ) : (
              <JobsSection
                campaignId={cid}
                jobs={shownJobs ?? []}
                expanded={expandedJobs}
                onToggle={toggleJob}
                onStopJob={onStopJob}
                stoppingJob={stoppingJob}
              />
            )}
          </Box>
        </Box>
      ) : null}
    </Stack>
  )
}

// -- the search's objective over its batches ---------------------------------

/** A search's rounds, foldable over a chart of how its objective has moved.
 *
 *  Owns the whole presentation of "rounds", because how they read depends on whether anything
 *  bounds them and that is one decision, not two: with a `batches` criterion this is the bar it
 *  always was — `3 / 6`, a meter, and an estimate — and without one it is the same row carrying
 *  only the count, no meter and no estimate. Not a fallback: an unbounded search HAS no limit,
 *  and drawing a meter would need a denominator the campaign never declared.
 *
 *  Closed by default and fetched only while open. That gating is the whole reason this can sit on
 *  a campaign card at all: the Monitor renders every campaign in the list, so anything a card does
 *  unconditionally is paid for by the whole page — see `useDetails`, which is closed by default for
 *  exactly this reason.
 *
 *  `batches_done` is in the query key rather than a refetch interval. It is an integer already on
 *  the polled status, and it is precisely the thing whose change makes this answer stale — so the
 *  series is re-read once per completed batch (minutes apart) instead of on a timer that would
 *  mostly re-fetch an unchanged answer.
 */
function ObjectiveSection({
  campaignId,
  status,
  counts,
  runsEta,
}: {
  campaignId: string
  status: Status
  counts?: JobCounts
  runsEta: number | null
}) {
  const batchesDone = status.batches_done
  // The criterion bounding the rounds, when one was declared. Only the batches budget converts
  // into time from what we can observe, which is why the estimate lives on this row alone.
  const bound = batchesBudget(status)
  const batchesEta = bound ? estimateBatchesEtaSeconds(status, counts, bound, runsEta) : null
  const label = bound ? bound.label + (bound.done ? ' ✓' : '') : 'batches'
  const meta = bound ? (
    <>
      {bound.current == null ? '—' : bound.current} / {bound.limit}
      {batchesEta != null
        ? ` · ~${formatDuration(batchesEta)} left (≈ ${formatLocalClock(batchesEta)})`
        : ''}
    </>
  ) : (
    // The count alone, and said as a count: `4 done` cannot be misread as progress toward a
    // total the way a bare `4` sitting where `4 / 6` usually sits could be.
    `${batchesDone} done`
  )
  const bar = bound ? (
    <MeterBar
      height={10}
      fraction={bound.current == null || bound.limit <= 0 ? 0 : bound.current / bound.limit}
      color="secondary.main"
    />
  ) : null
  const [open, setOpen] = useState(false)
  const history = useQuery({
    queryKey: ['search-history', campaignId, batchesDone],
    queryFn: () => robovast.getSearchHistory(campaignId),
    enabled: open,
    retry: false,
    // Within one batch count the answer cannot change, so it is read once per round.
    staleTime: Infinity,
  })
  return (
    <CollapsibleBox
      // `row`, not `card`: this bar sits in a stack of budget bars, and giving one of them a
      // border and a tinted header would read as a different KIND of thing rather than as the
      // one that opens. The chevron and the hover tint are the affordance.
      variant="row"
      flush
      title={label}
      meta={meta}
      subheader={bar}
      open={open}
      onToggle={() => setOpen((o) => !o)}
    >
      <Box sx={{ p: 1 }}>
        {history.isLoading ? (
          <Typography variant="caption" color="text.secondary">
            reading the search's rounds…
          </Typography>
        ) : history.isError ? (
          <Typography variant="caption" color="text.secondary">
            no objective history for this campaign ({(history.error as Error)?.message})
          </Typography>
        ) : history.data ? (
          <BatchObjectiveChart history={history.data} />
        ) : null}
      </Box>
    </CollapsibleBox>
  )
}

// -- jobs (live) ------------------------------------------------------------

const JOB_STATUS_COLOR: Record<string, 'default' | 'info' | 'success' | 'error' | 'warning'> = {
  running: 'info',
  pending: 'warning',
  completed: 'success',
  failed: 'error',
  // Neutral, not red: somebody chose this. Painting it as an error would put a deliberate
  // intervention next to the trials that actually failed.
  killed: 'default',
  blocked: 'error',
}

// Two of the campaign's jobs are not trials: a node-calibration probe, which measures the
// machine the runs will be sized against, and the postprocessing conversion, which turns the
// finished runs' rosbags into CSV. Both are listed because they hold real capacity, and both
// are marked because they are not runs — unmarked, a probe arrives carrying batch job 0's
// display name and a conversion reads as a run that outlived the batch.
//
// Marked with a second chip rather than by recolouring the status one, for two reasons. Their
// `status` is telling the truth — a failed probe must still read as failed, and that is the
// one probe worth looking at. And the four status hues are the only colours on this
// screen that carry a meaning (see `colors.ts`): the band is held to one lightness on purpose
// so no status shouts, and `data.series` is a brighter register tuned for 1px chart lines
// rather than filled chips. Muted neutral is what this file already uses to say "listed, but
// not a trial outcome" — see `killed` in JOB_STATUS_COLOR above.
function NonRunChip({ label, title }: { label: string; title: string }) {
  return (
    <Tooltip title={title}>
      <Chip
        label={label}
        size="small"
        variant="outlined"
        sx={{
          height: 18,
          color: NEUTRAL,
          borderColor: withAlpha(NEUTRAL, 0.7),
          '& .MuiChip-label': { px: 0.75, fontSize: '0.65rem' },
        }}
      />
    </Tooltip>
  )
}

// The campaign's current-batch jobs. Collapsed by default; each job row expands its
// own live log (running pod on the cluster / live system.log locally). Capped so a
// huge fan-out stays responsive.
const JOBS_RENDER_CAP = 100

function JobsSection({
  campaignId,
  jobs,
  expanded,
  onToggle,
  onStopJob,
  stoppingJob,
}: {
  campaignId: string
  jobs: JobSummary[]
  // Owned by StatusView, not here, because this section unmounts underneath the reader --
  // whenever the tab is switched away, and formerly whenever the live set emptied. It is what
  // makes a job's own log survive a trip to the Log tab and back, which is the whole answer to
  // the tab bar costing the side-by-side view.
  expanded: Set<string>
  onToggle: (jobName: string) => void
  onStopJob?: (job: JobSummary) => void
  stoppingJob?: string | null
}) {
  // What is not a trial goes ahead of the cap, not behind it: a batch wide enough to truncate
  // is exactly the one where a probe is both the reason nothing has started and the row that
  // falls off the end, and where the conversion is the one row saying what the campaign is
  // doing. There is at most one probe per node and one conversion, so the runs lose nothing.
  const shown = nonRunsFirst(jobs).slice(0, JOBS_RENDER_CAP)
  // The empty state is the reason this renders at all now. As a foldable section it simply
  // vanished when the live set emptied -- which happens between every pair of runs on the local
  // lane, where runs are sequential. A TAB that vanished would take the tab bar's shape with it,
  // and the selected tab out from under the reader, so the tab stays and says why it is empty.
  if (!shown.length) {
    return (
      <Typography variant="caption" color="text.secondary" sx={{ display: 'block', p: 1 }}>
        no job running right now
      </Typography>
    )
  }
  return (
    <>
      {/* Flat rows separated by hairlines rather than a bordered card each: the panel is
          already a card, and a box per job turned a 20-job batch into 20 nested frames. */}
      <Stack divider={<Box sx={{ borderTop: 1, borderColor: 'divider' }} />}>
        {shown.map((job) => (
          <JobRow
            key={job.job_name}
            campaignId={campaignId}
            job={job}
            open={expanded.has(job.job_name)}
            onToggle={() => onToggle(job.job_name)}
            onStopJob={onStopJob}
            stopping={stoppingJob === job.job_name}
          />
        ))}
        {jobs.length > shown.length ? (
          <Typography variant="caption" color="text.secondary" sx={{ px: 1, py: 0.5 }}>
            … {jobs.length - shown.length} more not shown
          </Typography>
        ) : null}
      </Stack>
    </>
  )
}

function JobRow({
  campaignId,
  job,
  open,
  onToggle,
  onStopJob,
  stopping,
}: {
  campaignId: string
  job: JobSummary
  open: boolean
  onToggle: () => void
  onStopJob?: (job: JobSummary) => void
  stopping?: boolean
}) {
  const calibration = isCalibrationJob(job)
  const postprocessing = isPostprocessingJob(job)
  // Offered only on a `running` job — the same rule the service enforces, so the UI never
  // shows a button the server would refuse. A pending or queued job has not started, and a
  // blocked one has a cause that deleting it does not fix. Nor on a probe or a conversion:
  // neither carries a run, so the service refuses to record one as killed.
  const canStop =
    Boolean(onStopJob) && job.status === 'running' && !calibration && !postprocessing
  // Why a job is stuck — e.g. a Kubernetes ImagePullBackOff reason + message — so a job
  // that can never start is legible without opening its (empty) log.
  const detail = job.detail ? (
    <Typography
      variant="caption"
      sx={{
        display: 'block',
        color: 'error.main',
        whiteSpace: 'pre-wrap',
        wordBreak: 'break-word',
      }}
    >
      {job.detail}
    </Typography>
  ) : null
  // Where the conversion's output is, said on the row itself. A row that cannot be opened has
  // to answer the question the missing chevron raises, or it reads as a job nobody can see
  // into.
  const where = postprocessing ? (
    <Typography variant="caption" color="text.secondary" sx={{ display: 'block' }}>
      output is in the Log tab, under POSTPROCESSING — where it stays after this job is gone
    </Typography>
  ) : null
  const note = detail || where ? (
    <>
      {detail}
      {where}
    </>
  ) : null
  const header = {
    variant: 'row' as const,
    actions: canStop ? (
      <Tooltip title="Stop this job. The campaign continues; this run is recorded as killed.">
        <IconButton
          size="small"
          color="error"
          aria-label={`Stop job ${job.display_name || job.job_name}`}
          disabled={stopping}
          onClick={() => onStopJob?.(job)}
          sx={{ p: 0.25 }}
        >
          <StopRoundedIcon fontSize="small" />
        </IconButton>
      </Tooltip>
    ) : null,
    leading: (
      <>
        {calibration ? (
          <NonRunChip
            label="calibration"
            title="A node-calibration probe: it measures this node so the campaign's runs can be sized against it. Not one of the campaign's runs, and not counted as one."
          />
        ) : null}
        {postprocessing ? (
          <NonRunChip
            label="postprocessing"
            title="The campaign's postprocessing: it converts the finished runs' rosbags in the execution image they were recorded with. Not one of the campaign's runs, and not counted as one. What it prints goes to the campaign log's POSTPROCESSING section, which is why this row does not open."
          />
        ) : null}
        <Chip
          label={job.status}
          size="small"
          color={JOB_STATUS_COLOR[job.status] ?? 'default'}
          variant="outlined"
          sx={{ height: 18, '& .MuiChip-label': { px: 0.75, fontSize: '0.65rem' } }}
        />
      </>
    ),
    // Left a plain string, and the chip carries the distinction on its own: CollapsibleBox
    // derives its toggle's aria-label from the title only when it IS a string, so wrapping
    // this to mute the colour would take the accessible name off every job row.
    title: job.display_name || job.job_name,
    note,
  }
  // The postprocessing row is a header and nothing else, because the log it would open is not
  // this pod's to serve. The conversion runs in initContainers -- the bag download, then the
  // conversion itself -- and a pod log reader reports the containers that run for the pod's
  // whole life, so through the entire conversion the panel had nothing to show and said so.
  // The output is not missing: every container's, init ones included, is published to the
  // campaign's POSTPROCESSING section as it runs, and that copy is in the object store, so it
  // is still there minutes later when `ttlSecondsAfterFinished` has taken the pod away --
  // which is exactly when someone reads a failed postprocess.
  if (postprocessing) return <CollapsibleBox {...header} collapsible={false} />
  return (
    <CollapsibleBox {...header} open={open} onToggle={onToggle}>
      <LogPanel
        resetKey={`${campaignId}/${job.job_name}`}
        streamUrl={robovast.jobLogStreamUrl(campaignId, job.job_name)}
      />
    </CollapsibleBox>
  )
}

// Live unified infrastructure log for one campaign (variation + run + postprocessing
// phases, divider-separated), streamed over SSE.
//
// No frame and no open state of its own: it is a tab's content, and the tab is both. The stream
// therefore opens when the tab is selected and closes when it is not, because the tab panel
// unmounts -- which is why the panels are rendered as a ternary rather than hidden with CSS. A
// hidden-but-mounted Log would hold an EventSource open for a panel nobody can see.
export function CampaignLog({ campaignId }: { campaignId: string }) {
  return <LogPanel resetKey={campaignId} streamUrl={robovast.campaignLogStreamUrl(campaignId)} />
}

// One backend error string, shown verbatim. These are multi-line — an exception message plus a
// traceback tail — and inline in an Alert or a Typography the browser collapses every newline,
// running the whole trace into one paragraph. Anywhere such a string is printed goes through here.
export function ErrorText({ children }: { children: ReactNode }) {
  return (
    <Box
      component="span"
      sx={{
        display: 'block',
        mt: 0.5,
        fontFamily: 'monospace',
        fontSize: '0.75rem',
        whiteSpace: 'pre-wrap',
        wordBreak: 'break-word',
        // A long trace should scroll inside the box rather than push the campaign card open.
        maxHeight: 200,
        overflowY: 'auto',
      }}
    >
      {children}
    </Box>
  )
}

// The controller's failure reason (message + traceback tail) — what you'd otherwise have to dig
// out of the pod log. The header stays visible; the traceback below it opens by itself only for
// `defaultOpen` callers (the newest campaign) — same collapse rule as StepFailure in Monitor.tsx,
// so an older, finished campaign doesn't add its own wall of stack frames to the list.
export function FailureBox({ error, defaultOpen = true }: { error: string; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <CollapsibleBox
      title="Failure"
      tone="error"
      open={open}
      onToggle={() => setOpen((o) => !o)}
    >
      <Box
        component="pre"
        sx={{
          m: 0,
          px: 1,
          py: 0.75,
          bgcolor: 'background.default',
          color: 'text.primary',
          fontFamily: 'monospace',
          fontSize: '0.75rem',
          whiteSpace: 'pre-wrap',
          overflowX: 'auto',
          maxHeight: 240,
          overflowY: 'auto',
        }}
      >
        {error}
      </Box>
    </CollapsibleBox>
  )
}
