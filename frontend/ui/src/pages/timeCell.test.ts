import { describe, expect, it } from 'vitest'
import type { Status } from '@/lib/robovastClient'
import { runningTimeCell } from './Monitor'

const st = (over: Partial<Status> = {}) => over as Status

// The column asks "how much longer". For a search the estimate alone cannot answer it: it is
// projected from what BOUNDS the search, so it says when the declared work runs out and not when
// the search stops. This cell was EMPTY for a search bounded only by convergence -- exactly the
// case whose answer is interesting.
describe('runningTimeCell', () => {
  it('shows the estimate when nothing is about to fire', () => {
    expect(runningTimeCell(st({ stopping_soon: false }), 720)).toBe('~12m left')
  })

  it('lets the warning supersede the estimate', () => {
    // The more decision-relevant fact, and the one the duration would otherwise misstate: a
    // "~12m left" beside a search stopping next round is worse than no number at all. Also a
    // width constraint -- AGE_COLUMN is fixed and a joined pair would overflow it.
    expect(runningTimeCell(st({ stopping_soon: true, stopping_reason: 'x' }), 720))
      .toBe('may stop early')
  })

  it('names the mechanism instead of going blank when there is no estimate', () => {
    // A convergence-bounded search has no duration to project, but it is not silent about WHY.
    expect(runningTimeCell(st({ stopping_soon: false }), null)).toBe('stops on convergence')
  })

  it('says nothing when no verdict was possible', () => {
    // Tri-state: null is "not checked", and `stops on convergence` would be a claim about a
    // criterion this campaign never declared.
    expect(runningTimeCell(st({ stopping_soon: null }), null)).toBe('')
    expect(runningTimeCell(st(), null)).toBe('')
    expect(runningTimeCell(undefined, null)).toBe('')
  })

  it('still shows the estimate when no verdict was possible', () => {
    // A batch campaign, or a search with no convergence criterion: the duration is all there is
    // to say, and it is not made less true by the absent verdict.
    expect(runningTimeCell(st({ stopping_soon: null }), 720)).toBe('~12m left')
    expect(runningTimeCell(undefined, 720)).toBe('~12m left')
  })
})
