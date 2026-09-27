import { describe, expect, it } from 'vitest'
import { runsFromVast } from './LaunchBar'

// The launcher sends the count it read from the .vast, or 0 for "as declared" when it could not
// read one. What it must never do is send a count of its own: the service takes any positive
// number as the campaign's size, so a placeholder would shrink a campaign without a word.
describe('runsFromVast', () => {
  it('reads the integer directly under a top-level execution block', () => {
    expect(runsFromVast('execution:\n  runs: 12\n  timeout: 30\n')).toBe(12)
  })

  it('is null when the count is not a literal', () => {
    expect(runsFromVast('execution:\n  runs: ${n}\n')).toBeNull()
    expect(runsFromVast('execution:\n  runs: runs\n')).toBeNull()
  })

  it('is null when no count is declared, or it sits elsewhere', () => {
    expect(runsFromVast('execution:\n  timeout: 30\n')).toBeNull()
    expect(runsFromVast('search:\n  execution:\n    runs: 3\n')).toBeNull()
    expect(runsFromVast('')).toBeNull()
  })
})
