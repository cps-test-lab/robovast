// One row per campaign, and — the part that matters — WHICH archive that row means.
//
// A campaign is on the share twice whenever it was uploaded at campaign end and exported
// again after postprocessing. Two rows differing only by a chip is a misclick; one row that
// does not say which archive it will fetch is worse, because the service resolves a bare
// campaign id to whichever archive its listing hits first. So the row carries the archive's
// own name, and these tests are about that name being the one the row advertises.
//
// Scope is deliberately narrow -- see the testing convention in docs/developer_guide.rst.

import { describe, expect, it } from 'vitest'
import { archiveName, matchRows, preferredArchive, shareRows } from './shareArchives'
import type { ShareArchive } from './robovastClient'

const archive = (
  campaign_id: string,
  variant: string,
  extra: Partial<ShareArchive> = {},
): ShareArchive => ({
  campaign_id,
  variant,
  object_name: `${campaign_id}.${variant}.tar.gz`,
  size: variant === 'postprocessed' ? 1200 : 900,
  url: null,
  ...extra,
})

const NAV = 'nav-2026-08-18-194018'
const OBST = 'obst-2026-08-22-084411'

describe('shareRows', () => {
  it('collapses a campaign held twice into one row naming the postprocessed archive', () => {
    const rows = shareRows(
      [archive(NAV, 'postprocessed'), archive(NAV, 'raw')],
      new Set(),
    )
    expect(rows).toHaveLength(1)
    expect(rows[0]).toMatchObject({
      campaignId: NAV,
      variant: 'postprocessed',
      size: 1200,
      archive: `${NAV}.postprocessed.tar.gz`,
    })
  })

  it('prefers the postprocessed archive whichever order it is listed in', () => {
    // The listing's order between two archives of one campaign is stable but arbitrary --
    // they share an id and a timestamp -- so the preference may not depend on it.
    const forwards = shareRows([archive(NAV, 'raw'), archive(NAV, 'postprocessed')], new Set())
    const backwards = shareRows([archive(NAV, 'postprocessed'), archive(NAV, 'raw')], new Set())
    expect(forwards[0].archive).toBe(backwards[0].archive)
    expect(forwards[0].variant).toBe('postprocessed')
  })

  it('keeps a raw archive when that is all the share has', () => {
    const rows = shareRows([archive(OBST, 'raw')], new Set())
    expect(rows[0]).toMatchObject({ variant: 'raw', archive: `${OBST}.raw.tar.gz` })
  })

  it('keeps the order the service sent, campaign by campaign', () => {
    // The service answers newest-campaign-first. Re-deriving that here would be the same
    // rule in a second language, free to drift from the first.
    const rows = shareRows(
      [archive(OBST, 'raw'), archive(NAV, 'raw'), archive(NAV, 'postprocessed')],
      new Set(),
    )
    expect(rows.map((r) => r.campaignId)).toEqual([OBST, NAV])
  })

  it('marks a campaign this deployment already has', () => {
    const rows = shareRows([archive(NAV, 'raw'), archive(OBST, 'raw')], new Set([NAV]))
    expect(rows.map((r) => r.present)).toEqual([true, false])
  })

  it('carries a size the provider could not report through unchanged', () => {
    // -1 is "the provider cannot say", not a size. Rendering is the dialog's problem; what
    // must not happen here is it being mistaken for a small archive.
    const rows = shareRows([archive(NAV, 'raw', { size: -1 })], new Set())
    expect(rows[0].size).toBe(-1)
  })
})

describe('archiveName', () => {
  it('strips a provider key prefix', () => {
    // GCS prefixes its keys. The archive-name parser at the far end refuses any name with a
    // separator in it, so an import that sent the whole key would simply not resolve.
    expect(archiveName(archive(NAV, 'raw', { object_name: `results/${NAV}.raw.tar.gz` })))
      .toBe(`${NAV}.raw.tar.gz`)
  })
})

describe('preferredArchive', () => {
  it('is the postprocessed one, or the first when neither is', () => {
    const raw = archive(NAV, 'raw')
    const post = archive(NAV, 'postprocessed')
    expect(preferredArchive(raw, post)).toBe(post)
    expect(preferredArchive(post, raw)).toBe(post)
    expect(preferredArchive(raw, raw)).toBe(raw)
  })
})

describe('matchRows', () => {
  const rows = shareRows([archive(NAV, 'raw'), archive(OBST, 'raw')], new Set())

  it('matches a substring of the campaign id, either case', () => {
    expect(matchRows(rows, 'OBST').map((r) => r.campaignId)).toEqual([OBST])
    expect(matchRows(rows, '2026-08').map((r) => r.campaignId)).toEqual([NAV, OBST])
  })

  it('treats an empty or blank query as no filter', () => {
    // The dialog opens with an empty box, and a deep link may carry only whitespace.
    expect(matchRows(rows, '')).toEqual(rows)
    expect(matchRows(rows, '   ')).toEqual(rows)
  })

  it('returns nothing when nothing matches', () => {
    expect(matchRows(rows, 'no-such-campaign')).toEqual([])
  })
})
