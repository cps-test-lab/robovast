import { describe, expect, it, vi } from 'vitest'

import { FrameCache } from './frameCache'

describe('FrameCache', () => {
  it('holds at most its capacity, evicting the least recently used and revoking it', () => {
    const revoke = vi.fn()
    const c = new FrameCache(2, revoke)
    c.set(1, 'a')
    c.set(2, 'b')
    expect(c.get(1)).toBe('a') // 1 is now the most recent
    c.set(3, 'c')
    expect(c.has(2)).toBe(false)
    expect(revoke).toHaveBeenCalledWith('b')
    expect(c.has(1)).toBe(true)
    expect(c.size).toBe(2)
  })

  it('revokes a replaced entry and everything on clear', () => {
    const revoke = vi.fn()
    const c = new FrameCache(4, revoke)
    c.set(1, 'a')
    c.set(1, 'a2')
    expect(revoke).toHaveBeenCalledWith('a')
    c.set(2, 'b')
    c.clear()
    expect(revoke).toHaveBeenCalledWith('a2')
    expect(revoke).toHaveBeenCalledWith('b')
    expect(c.size).toBe(0)
  })
})
