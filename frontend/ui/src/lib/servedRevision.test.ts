import { beforeEach, describe, expect, it } from 'vitest'
import { acceptRevision, resetSeenRevision, revisionChanged } from './servedRevision'

beforeEach(resetSeenRevision)

describe('revisionChanged', () => {
  it('takes the first revision it is given as the reference', () => {
    expect(revisionChanged('93697b8')).toBe(false)
    expect(revisionChanged('93697b8')).toBe(false)
  })

  it('reports a later, different revision as a change', () => {
    revisionChanged('93697b8')
    expect(revisionChanged('b3808c6')).toBe(true)
    // And keeps reporting it: the tab is still holding the old build.
    expect(revisionChanged('b3808c6')).toBe(true)
  })

  it('cannot tell without a revision, and does not become able to', () => {
    expect(revisionChanged(null)).toBe(false)
    expect(revisionChanged(undefined)).toBe(false)
    // A source checkout reports none at all; the first usable value is still a reference
    // and not a change.
    expect(revisionChanged('93697b8')).toBe(false)
  })

  it('does not read a dropped revision as a restart', () => {
    revisionChanged('93697b8')
    expect(revisionChanged(null)).toBe(false)
  })
})

describe('acceptRevision', () => {
  it('makes a dismissal stick across later reads', () => {
    revisionChanged('93697b8')
    expect(revisionChanged('b3808c6')).toBe(true)
    acceptRevision('b3808c6')
    expect(revisionChanged('b3808c6')).toBe(false)
    // A further restart is still caught.
    expect(revisionChanged('c0ffee1')).toBe(true)
  })

  it('ignores an absent revision rather than clearing the reference', () => {
    revisionChanged('93697b8')
    acceptRevision(null)
    expect(revisionChanged('b3808c6')).toBe(true)
  })
})
