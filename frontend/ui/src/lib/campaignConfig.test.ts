// When a campaign's frozen `_config/` may be linked to, and what an unreadable one may be reported
// as. Both are tested here because both failures read as normal UI: the card offered the link to a
// campaign still arriving from an archive, and the view then appended one fixed diagnosis to every
// error — so a lookup that failed, an import whose bytes had not landed, and a campaign that
// genuinely froze nothing all came out as "this campaign froze no configuration", a statement about
// the campaign that nothing downstream could tell from a true one.
//
// Scope is deliberately narrow -- see the testing convention in docs/developer_guide.rst.

import { describe, expect, it } from 'vitest'
import { RobovastError } from './robovastClient'
import { campaignConfigNote, mayHaveStagedConfig } from './campaignConfig'

const notFound = new RobovastError(404, "no directory at '/results/c1/_config/'")

describe('campaignConfigNote', () => {
  it('says nothing about staging when the lookup itself failed', () => {
    expect(campaignConfigNote(new RobovastError(503, 'object store unreachable'), 'finished'))
      .toBe('')
    expect(campaignConfigNote(new Error('Failed to fetch'), 'running')).toBe('')
  })

  it('reads a running campaign as not there yet', () => {
    expect(campaignConfigNote(notFound, 'running')).toMatch(/not reached that point yet/)
  })

  it('reads a terminal campaign as one that never got there', () => {
    expect(campaignConfigNote(notFound, 'failed')).toMatch(/ended before it did/)
    expect(campaignConfigNote(notFound, 'finished')).toMatch(/ended before it did/)
  })

  it('claims neither while the phase is unknown', () => {
    const note = campaignConfigNote(notFound, undefined)
    expect(note).not.toMatch(/yet|ended/)
    expect(note).toMatch(/first batch is prepared/)
  })
})

describe('mayHaveStagedConfig', () => {
  it('refuses every phase before the run loop', () => {
    for (const phase of ['initializing', 'building', 'starting', 'plugin install', 'variation']) {
      expect(mayHaveStagedConfig(phase)).toBe(false)
    }
  })

  it('refuses a campaign whose archive is still arriving', () => {
    // An import is listed from before its first byte lands (the campaign directory does not
    // exist yet), so this is the one phase where a link would be offered on a campaign that has
    // nothing at all behind it.
    expect(mayHaveStagedConfig('importing')).toBe(false)
  })

  it('allows a campaign that has reached its run loop, and every terminal phase', () => {
    for (const phase of ['running', 'finishing', 'postprocessing', 'sharing',
                         'finished', 'failed', 'stopped', 'crashed', 'unknown']) {
      expect(mayHaveStagedConfig(phase)).toBe(true)
    }
  })
})
