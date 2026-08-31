import { describe, expect, it } from 'vitest'
import { describeCampaignEvent, diffCampaignPhases, seedPhases } from './campaignEvents'
import type { CampaignSummary } from './robovastClient'

const c = (campaign_id: string, phase: string): CampaignSummary =>
  ({ campaign_id, phase } as CampaignSummary)

const kinds = (evts: ReturnType<typeof diffCampaignPhases>) => evts.map((e) => e.kind)

describe('seedPhases', () => {
  it('absorbs the first frame silently — otherwise opening the app announces everything', () => {
    const list = [c('a', 'running'), c('b', 'finished')]
    expect(diffCampaignPhases(seedPhases(list), list)).toEqual([])
  })

  it('reports nothing for a campaign it has never seen, even a running one', () => {
    // This is what makes a reconnect quiet: the stream re-sends the whole list, and the
    // baseline the caller kept still holds every id in it.
    expect(diffCampaignPhases(new Map(), [c('a', 'running')])).toEqual([])
  })
})

describe('diffCampaignPhases', () => {
  it('announces a start when a campaign leaves a terminal phase', () => {
    const prev = seedPhases([c('a', 'unknown')])
    expect(kinds(diffCampaignPhases(prev, [c('a', 'running')]))).toEqual(['started'])
  })

  it('says nothing while a running campaign moves between running phases', () => {
    const prev = seedPhases([c('a', 'building')])
    expect(diffCampaignPhases(prev, [c('a', 'running')])).toEqual([])
  })

  it('maps each terminal phase to what it actually was', () => {
    const prev = seedPhases([c('a', 'running'), c('b', 'running'), c('d', 'running')])
    const evts = diffCampaignPhases(prev, [c('a', 'finished'), c('b', 'failed'), c('d', 'stopped')])
    expect(kinds(evts)).toEqual(['finished', 'failed', 'stopped'])
  })

  it('treats crashed as a failure rather than inventing a fourth ending', () => {
    const prev = seedPhases([c('a', 'running')])
    expect(kinds(diffCampaignPhases(prev, [c('a', 'crashed')]))).toEqual(['failed'])
  })

  it('stays silent on a drop into unknown — a lost driver is not an ending', () => {
    // A service restart reconstructs every live campaign to `unknown` at once. Announcing that
    // would fire an "ended" notice per campaign, none of which ended.
    const prev = seedPhases([c('a', 'running'), c('b', 'running')])
    expect(diffCampaignPhases(prev, [c('a', 'unknown'), c('b', 'unknown')])).toEqual([])
  })

  it('announces a campaign that legitimately ends twice', () => {
    // finished -> postprocessing -> finished is a rerun, not a duplicate: the campaign really
    // did work again and really did finish again. No once-only guard.
    let prev = seedPhases([c('a', 'finished')])
    const restart = [c('a', 'postprocessing')]
    expect(kinds(diffCampaignPhases(prev, restart))).toEqual(['started'])
    prev = seedPhases(restart)
    expect(kinds(diffCampaignPhases(prev, [c('a', 'finished')]))).toEqual(['finished'])
  })

  it('ignores a campaign whose phase did not move', () => {
    const prev = seedPhases([c('a', 'running')])
    expect(diffCampaignPhases(prev, [c('a', 'running')])).toEqual([])
  })

  it('reports each changed campaign once in a mixed frame', () => {
    const prev = seedPhases([c('a', 'running'), c('b', 'running'), c('d', 'finished')])
    const evts = diffCampaignPhases(prev, [c('a', 'finished'), c('b', 'running'), c('d', 'finished')])
    expect(evts).toHaveLength(1)
    expect(evts[0]).toMatchObject({ campaignId: 'a', kind: 'finished', phase: 'finished' })
  })

  it('carries the summary so a caller can say more than the kind does', () => {
    const prev = seedPhases([c('a', 'running')])
    const [evt] = diffCampaignPhases(prev, [c('a', 'finished')])
    expect(evt.summary.campaign_id).toBe('a')
  })
})

describe('describeCampaignEvent', () => {
  const evt = (kind: 'failed' | 'finished', summary: Partial<CampaignSummary> = {}) =>
    ({ campaignId: 'nav-2026-08-30-1916', kind, phase: kind,
       summary: { campaign_id: 'nav-2026-08-30-1916', ...summary } as CampaignSummary })

  it('says WHY a campaign failed, not just that it did', () => {
    // The whole point: without the reason on the listing, every failure anywhere in the app
    // read "Campaign failed" and nothing else, and the reason was a per-campaign request away.
    const { message, note } = describeCampaignEvent(
      evt('failed', { error: 'every job in batch 3 was dropped' }))
    expect(message).toBe('Campaign failed')
    expect(note).toContain('every job in batch 3 was dropped')
    expect(note).toContain('nav-2026-08-30-1916')
  })

  it('still names the campaign when no reason was recorded', () => {
    // An older campaign, or one whose Status was reconstructed without an error: the notice
    // must degrade to naming the campaign rather than to a dangling separator.
    expect(describeCampaignEvent(evt('failed')).note).toBe('nav-2026-08-30-1916')
  })

  it('leaves a successful ending reporting its size, not an error', () => {
    const { note } = describeCampaignEvent(evt('finished', { num_runs: 180, num_failed: 75 }))
    expect(note).toBe('nav-2026-08-30-1916 · 180 runs · 75 failed')
  })
})
