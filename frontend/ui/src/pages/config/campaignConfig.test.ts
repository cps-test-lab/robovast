// What a campaign's unreadable `_config/` may be reported as. Tested here because the failure it
// guards against reads as a normal sentence: the view used to append one fixed diagnosis to every
// error, so a lookup that failed, and a configuration not staged yet, both came out as "this
// campaign froze no configuration" — a statement about the campaign that nothing downstream could
// tell from a true one.
//
// Scope is deliberately narrow -- see the testing convention in docs/developer_guide.rst.

import { describe, expect, it } from 'vitest'
import { RobovastError } from '@/lib/robovastClient'
import { campaignConfigNote } from './campaignConfig'

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
