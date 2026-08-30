import { describe, expect, it } from 'vitest'
import { formatAge } from './time'

const NOW = Date.parse('2026-08-29T12:00:00Z')
const ago = (seconds: number) => new Date(NOW - seconds * 1000).toISOString()

describe('formatAge', () => {
  it('says nothing rather than guessing when there is no timestamp', () => {
    expect(formatAge(null, NOW)).toBe('')
    expect(formatAge('not a date', NOW)).toBe('')
  })

  it('collapses the first minute and a half, where a number would only flicker', () => {
    expect(formatAge(ago(3), NOW)).toBe('just now')
    expect(formatAge(ago(89), NOW)).toBe('just now')
  })

  it('coarsens as it ages — minutes, hours, days, then weeks', () => {
    expect(formatAge(ago(20 * 60), NOW)).toBe('20 min ago')
    expect(formatAge(ago(5 * 3600), NOW)).toBe('5 h ago')
    expect(formatAge(ago(3 * 86400), NOW)).toBe('3 d ago')
    expect(formatAge(ago(40 * 86400), NOW)).toBe('6 wk ago')
  })

  it('never reads as the future when a clock skews', () => {
    expect(formatAge(new Date(NOW + 60_000).toISOString(), NOW)).toBe('just now')
  })
})
