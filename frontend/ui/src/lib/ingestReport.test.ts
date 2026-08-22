import { describe, expect, it } from 'vitest'

import { describeImport, describeImportError, stageRows } from './ingestReport'
import type { IngestReport, IngestStage } from './ingestReport'

// What these defend is the wording, because the outcome box is COLLAPSED by default: the
// headline is the entire message for anyone who does not click it. Two mistakes are possible
// and both are silent — a caveat that never reaches the headline (so a migrated or thin import
// reads as unqualified success), and a *degraded* import worded as a failure (so somebody
// discards a campaign they just recovered).

// The generated response types mark every field required (the service serializes whole models),
// so a stage is built from defaults rather than written partially — which also keeps each case
// below down to the one or two fields it is actually about.
const stage = (over: Partial<IngestStage>): IngestStage => ({
  verdict: 'ok',
  detail: '',
  version: null,
  steps: [],
  schema_version: null,
  runs: null,
  rebuilt: null,
  recovery: '',
  ...over,
})

const report = (over: Partial<IngestReport> = {}): IngestReport => ({
  campaign_id: 'nav-2026-03-04-152130',
  ok: true,
  blocking: [],
  path: '/results/nav-2026-03-04-152130',
  stages: {
    layout: stage({ detail: '_config/ and _execution/ present' }),
    config: stage({ detail: 'config version 2' }),
    campaign_store: stage({ detail: 'registered at schema v7, indexing 20 run(s)' }),
    analysis_db: stage({ detail: '1 analysis database(s) present' }),
  },
  ...over,
})

describe('describeImport', () => {
  it('says only that it imported when nothing needs qualifying', () => {
    const out = describeImport(report())
    expect(out.headline).toBe('Imported nav-2026-03-04-152130')
    expect(out.tone).toBe('ok')
  })

  it('names a migration in the headline rather than hiding it in the fold', () => {
    const out = describeImport(
      report({
        stages: {
          ...report().stages,
          config: stage({
            verdict: 'migrated',
            detail: 'config version 1 migrates to 2',
            version: 1,
          }),
        },
      }),
    )
    expect(out.headline).toContain('Imported')
    expect(out.headline).toContain('config migrated')
    expect(out.tone).toBe('warning')
  })

  it('reads a degraded import as imported-with-a-caveat, not as a failure', () => {
    // The property the service is explicit about: a campaign that lists but under-reports is a
    // real, useful outcome. Wording it as a failure would invite throwing it away.
    const out = describeImport(
      report({
        stages: {
          ...report().stages,
          campaign_store: stage({ verdict: 'degraded', detail: 'it indexes no runs' }),
        },
      }),
    )
    expect(out.tone).not.toBe('error')
    expect(out.headline).toMatch(/^Imported /)
    expect(out.headline).toContain('campaign_store degraded')
  })

  it('names which stage blocked when the import did not land', () => {
    const out = describeImport(
      report({
        ok: false,
        blocking: ['layout'],
        stages: { layout: stage({ verdict: 'failed', detail: 'no _config/ directory' }) },
      }),
    )
    expect(out.headline).toContain('could not be imported')
    expect(out.headline).toContain('layout')
    expect(out.tone).toBe('error')
  })

  it('treats a newer archive as loud, since nothing here can bring it back', () => {
    const out = describeImport(
      report({
        stages: {
          ...report().stages,
          config: stage({ verdict: 'newer', detail: 'config version 3 is newer than supported' }),
        },
      }),
    )
    // Not blocking (it still displays), so the import succeeded — but the stage row is red,
    // because a schema cannot be migrated downwards and there is no recovery to offer.
    expect(out.headline).toMatch(/^Imported /)
    expect(out.rows.find((r) => r.name === 'config')?.tone).toBe('error')
  })

  it('offers a store rebuild exactly when a stage names that recovery', () => {
    expect(describeImport(report()).offerRebuild).toBe(false)
    const corrupt = describeImport(
      report({
        ok: false,
        blocking: ['campaign_store'],
        stages: {
          campaign_store: stage({
            verdict: 'failed',
            detail: 'campaign.db is present but unreadable',
            recovery: '--rebuild-store',
          }),
        },
      }),
    )
    expect(corrupt.offerRebuild).toBe(true)
  })
})

describe('stageRows', () => {
  it('marks the stages the report blames', () => {
    const rows = stageRows(
      report({
        ok: false,
        blocking: ['config'],
        stages: {
          layout: stage({ detail: 'present' }),
          config: stage({ verdict: 'failed', detail: 'could not be parsed' }),
        },
      }),
    )
    expect(rows).toHaveLength(2)
    expect(rows.find((r) => r.name === 'config')?.blocking).toBe(true)
    expect(rows.find((r) => r.name === 'layout')?.blocking).toBe(false)
  })

  it('survives a verdict this UI has not been taught yet', () => {
    // A newer service could report a stage word this build does not know. Showing it verbatim
    // is right; dropping the row, or crashing the campaign view, is not.
    const rows = stageRows(report({ stages: { odd: stage({ verdict: 'quarantined' }) } }))
    expect(rows[0].label).toBe('quarantined')
    expect(rows[0].tone).toBe('warning')
  })
})

describe('describeImportError', () => {
  it('offers Replace only for the conflict a user can actually clear', () => {
    expect(describeImportError(409, 'already here').offerReplace).toBe(true)
    expect(describeImportError(400, 'not a tarball').offerReplace).toBe(false)
    expect(describeImportError(undefined, 'network down').offerReplace).toBe(false)
  })

  it("keeps the service's own message as the detail", () => {
    // The service's messages carry the recovery; paraphrasing them here would lose it.
    expect(describeImportError(400, 'archive holds 2 top-level entries').detail).toBe(
      'archive holds 2 top-level entries',
    )
  })
})
