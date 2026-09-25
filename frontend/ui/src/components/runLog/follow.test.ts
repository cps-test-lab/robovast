import { describe, expect, it } from 'vitest'
import { TAIL_SLACK_PX, tailFollows } from './follow'

describe('tailFollows', () => {
  const g = (scrollTop: number, scrollHeight = 1000, clientHeight = 200) => ({
    scrollTop,
    scrollHeight,
    clientHeight,
  })

  it('follows at the bottom', () => {
    expect(tailFollows(g(800))).toBe(true)
  })

  it('follows within the slack of the bottom', () => {
    expect(tailFollows(g(800 - TAIL_SLACK_PX))).toBe(true)
    expect(tailFollows(g(800.4))).toBe(true)
  })

  it('pauses once scrolled up past the slack', () => {
    expect(tailFollows(g(800 - TAIL_SLACK_PX - 1))).toBe(false)
    expect(tailFollows(g(0))).toBe(false)
  })

  it('follows content shorter than the viewport', () => {
    expect(tailFollows(g(0, 100, 200))).toBe(true)
  })
})
