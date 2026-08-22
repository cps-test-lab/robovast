// Client-side model for an import's outcome: the campaign view shows what happened to an
// uploaded archive, per stage, with a recovery where one exists. Pure and React-free, in the
// same spirit as `campaignDetails.ts` — the component renders what this returns and owns no
// wording of its own.
//
// The shape is `IngestReport` from the service (see `robovast.service.ingest`): four stages,
// each with a verdict, and `ok` false only when one genuinely BLOCKS. The distinction this
// module exists to keep is that a *degraded* import succeeded — it is a campaign somebody
// already has, listing and displaying but under-reporting — so it must read as a caveat on an
// import that worked, never as a failure. Collapsing the two would tell a user to throw away
// data they just recovered.

// Hand-declared rather than taken from `api.generated.ts`, because this is not an HTTP response
// shape: the import is asynchronous, so the service writes its stage report to the campaign's
// `_execution/import.json` and no route returns it. FastAPI therefore emits no schema for it.
//
// The source of truth is `robovast.service.ingest.ingest_campaign`'s return value and the
// `IngestReport` pydantic model beside it. Every field is optional-by-default here for that
// reason — this type is a reader's promise about a file, not a generated guarantee, so a report
// written by a newer service must degrade rather than crash the campaign view.
export interface IngestStage {
  verdict?: string
  detail?: string
  version?: number | null
  steps?: string[]
  schema_version?: number | null
  runs?: number | null
  rebuilt?: boolean | null
  recovery?: string
}

export interface IngestReport {
  campaign_id?: string
  ok?: boolean
  blocking?: string[]
  stages?: Record<string, IngestStage>
  path?: string
}

/** How each verdict is shown. `newer` is not a blocker — the campaign still displays — but
 *  nothing here can fix it (a schema cannot migrate downwards), so it reads as loudly as a
 *  failure rather than as a caveat somebody might act on. */
const VERDICTS: Record<string, { label: string; tone: 'error' | 'warning' | 'ok' }> = {
  failed: { label: 'failed', tone: 'error' },
  newer: { label: 'newer', tone: 'error' },
  degraded: { label: 'degraded', tone: 'warning' },
  absent: { label: 'absent', tone: 'warning' },
  migrated: { label: 'migrated', tone: 'warning' },
  ok: { label: 'ok', tone: 'ok' },
}

export interface StageRow {
  /** The stage's name as the service reports it (`layout`, `config`, …). */
  name: string
  verdict: string
  label: string
  tone: 'error' | 'warning' | 'ok'
  detail: string
  /** Present only where the stage names something the caller can do. */
  recovery: string
  /** True when this stage is why the import did not succeed. */
  blocking: boolean
}

export interface ImportOutcome {
  /** What goes on the collapsed row — the whole message when nobody expands it. */
  headline: string
  /** Drives the box's colour. `error` only for an import that did not land. */
  tone: 'error' | 'warning' | 'ok'
  rows: StageRow[]
  /** Offer a retry that rebuilds `campaign.db` from the results tree. */
  offerRebuild: boolean
}

export function stageRows(report: IngestReport): StageRow[] {
  const blocking = new Set(report.blocking ?? [])
  return Object.entries(report.stages ?? {}).map(([name, stage]) => {
    const verdict = stage?.verdict ?? ''
    const known = VERDICTS[verdict] ?? { label: verdict || 'unknown', tone: 'warning' as const }
    return {
      name,
      verdict,
      label: known.label,
      tone: known.tone,
      detail: stage?.detail ?? '',
      recovery: stage?.recovery ?? '',
      blocking: blocking.has(name),
    }
  })
}

/** What the campaign view says about a finished import.
 *
 *  The headline names the campaign and anything notable that happened to it, because the box is
 *  COLLAPSED by default: whatever is not in this string is something the reader has to click to
 *  discover, so "imported X" alone would hide a migration and a store that indexes no runs. */
export function describeImport(report: IngestReport): ImportOutcome {
  const rows = stageRows(report)
  const id = report.campaign_id || 'the archive'
  const offerRebuild = rows.some((r) => r.recovery.includes('rebuild-store'))

  if (!report.ok) {
    const which = (report.blocking ?? []).join(', ')
    return {
      headline: `${id} could not be imported${which ? ` — ${which} failed` : ''}`,
      tone: 'error',
      rows,
      offerRebuild,
    }
  }

  // It imported. Say so first, then what is worth knowing about it — a caveat on a success is
  // not a failure and must not be worded as one.
  const caveats = rows.filter((r) => r.tone !== 'ok')
  if (!caveats.length) return { headline: `Imported ${id}`, tone: 'ok', rows, offerRebuild }
  return {
    headline: `Imported ${id} — ${caveats.map((r) => `${r.name} ${r.label}`).join(', ')}`,
    tone: 'warning',
    rows,
    offerRebuild,
  }
}

/** The message for an import that never produced a report (a refused archive, a dead service).
 *
 *  Kept here so the component has one source of wording. A 409 is the one worth naming: it is
 *  the only failure a user can clear from the UI, by importing again with `force`. */
export function describeImportError(
  status: number | undefined,
  message: string,
): { headline: string; detail: string; offerReplace: boolean } {
  if (status === 409) {
    return {
      headline: 'A campaign with this id is already here',
      detail: message,
      offerReplace: true,
    }
  }
  if (status === 400) {
    return { headline: 'That file is not a campaign archive', detail: message, offerReplace: false }
  }
  return { headline: 'The import failed', detail: message, offerReplace: false }
}
