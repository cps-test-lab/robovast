import { describe, expect, it } from 'vitest'
import { describeCampaignEvent, seedPhases, trackCampaignPhases } from './campaignEvents'
import type { CampaignActivity, CampaignEventKind } from './campaignEvents'
import type { CampaignSummary } from './robovastClient'

const c = (campaign_id: string, phase: string): CampaignSummary =>
  ({ campaign_id, phase } as CampaignSummary)

const diffCampaignPhases = (
  prev: Parameters<typeof trackCampaignPhases>[0],
  next: Parameters<typeof trackCampaignPhases>[1],
) => trackCampaignPhases(prev, next).events

const kinds = (evts: ReturnType<typeof diffCampaignPhases>) => evts.map((e) => e.kind)

/** Walk a campaign through phases from a silently seeded baseline, collecting what was announced. */
const walk = (id: string, phases: readonly string[]) => {
  let baseline = seedPhases([c(id, phases[0])])
  const events = []
  for (const phase of phases.slice(1)) {
    const step = trackCampaignPhases(baseline, [c(id, phase)])
    baseline = step.baseline
    events.push(...step.events)
  }
  return events
}

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
    const evts = walk('a', ['finished', 'postprocessing', 'finished'])
    expect(kinds(evts)).toEqual(['started', 'finished'])
    expect(evts.map((e) => e.activity)).toEqual(['postprocessing', 'postprocessing'])
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

describe('activity', () => {
  it('names a share re-trigger an export, at both ends', () => {
    // The bug this exists for: `run_share` re-activates a finished campaign, so the phase diff
    // saw a campaign start and finish again.
    const evts = walk('a', ['finished', 'sharing', 'finished'])
    expect(kinds(evts)).toEqual(['started', 'finished'])
    expect(evts.map((e) => e.activity)).toEqual(['export', 'export'])
  })

  it('still calls a real campaign a campaign, though it also ends via sharing', () => {
    // The regression guard. The controller shares on the way out, so `sharing -> finished` is
    // the last transition of an ordinary run too -- naming the ending after the phase it passed
    // through last would relabel every campaign ending as an export.
    const evts = walk('a', ['running', 'finishing', 'sharing', 'finished'])
    expect(kinds(evts)).toEqual(['finished'])
    expect(evts[0].activity).toBe('campaign')
  })

  it('falls back to campaign for a spell that was already under way when we first looked', () => {
    // The first frame seeds silently, so the entry phase was never observed. Generic wording is
    // the honest answer -- and the safe direction if a frame is ever coalesced away.
    const evts = walk('a', ['sharing', 'finished'])
    expect(evts.map((e) => e.activity)).toEqual(['campaign'])
  })

  it('names an import', () => {
    expect(walk('a', ['finished', 'importing', 'finished'])[0].activity).toBe('import')
  })

  it('forgets the entry once the spell is over, so the next one is judged on its own', () => {
    const evts = walk('a', ['finished', 'sharing', 'finished', 'postprocessing', 'finished'])
    expect(evts.map((e) => e.activity))
      .toEqual(['export', 'export', 'postprocessing', 'postprocessing'])
  })
})

describe('describeCampaignEvent', () => {
  const evt = (
    kind: CampaignEventKind,
    summary: Partial<CampaignSummary> = {},
    activity: CampaignActivity = 'campaign',
  ) =>
    ({ campaignId: 'nav-2026-08-30-1916', kind, phase: kind, activity,
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

  it('names the operation rather than the campaign, and drops the run tally with it', () => {
    // The tally belongs to the campaign. Printed under an export it claimed the upload had run
    // 180 trials and failed 75 of them, having run none.
    const { message, note } = describeCampaignEvent(
      evt('finished', { num_runs: 180, num_failed: 75 }, 'export'))
    expect(message).toBe('Export finished')
    expect(note).toBe('nav-2026-08-30-1916')
  })

  it('reports an export failure with the share error, not the campaign error', () => {
    const { message, note } = describeCampaignEvent(
      evt('failed', { share_error: 'bucket rejected the upload', error: 'a job was dropped' },
          'export'))
    expect(message).toBe('Export failed')
    expect(note).toContain('bucket rejected the upload')
    expect(note).not.toContain('a job was dropped')
  })

  it('falls back to the campaign error when the operation recorded none of its own', () => {
    expect(describeCampaignEvent(evt('failed', { error: 'lane unreachable' }, 'export')).note)
      .toContain('lane unreachable')
  })
})
