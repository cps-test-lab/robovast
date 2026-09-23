// What the priority chip says and which typed values the priority prompt lets through.
//
// Scope is deliberately narrow -- see the testing convention in docs/developer_guide.rst.

import { describe, expect, it } from 'vitest'
import { offersQueueControls, priorityInputError, priorityLabel } from './queueStanding'

describe('priorityLabel', () => {
  it('is null at the default rank', () => {
    expect(priorityLabel(0)).toBeNull()
  })
  it('signs a raised rank and keeps the sign of a lowered one', () => {
    expect(priorityLabel(2)).toBe('prio +2')
    expect(priorityLabel(-1)).toBe('prio -1')
  })
})

describe('priorityInputError', () => {
  it('accepts whole numbers, signed or not, with surrounding space', () => {
    for (const v of ['0', '3', '-2', ' 5 ', '+1']) expect(priorityInputError(v)).toBeNull()
  })
  it('refuses an empty field rather than reading it as 0', () => {
    expect(priorityInputError('')).not.toBeNull()
    expect(priorityInputError('   ')).not.toBeNull()
  })
  it('refuses fractions and text', () => {
    for (const v of ['1.5', 'high', '2x']) expect(priorityInputError(v)).not.toBeNull()
  })
})

describe('offersQueueControls', () => {
  it('offers them only on a lane that says it queues campaigns', () => {
    expect(offersQueueControls({ can_schedule: true })).toBe(true)
    expect(offersQueueControls({ can_schedule: false })).toBe(false)
  })
  it('does not offer them while the answer is unknown', () => {
    expect(offersQueueControls(undefined)).toBe(false)
    expect(offersQueueControls({ can_schedule: null })).toBe(false)
    expect(offersQueueControls({})).toBe(false)
  })
})
