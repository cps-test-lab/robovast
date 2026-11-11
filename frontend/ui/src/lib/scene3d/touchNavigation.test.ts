// What fingers do to the view, against a fake viewport that records each operation.
import { describe, expect, it } from 'vitest'
import { PINCH_PX, TouchNavigation, type ClientPoint, type TouchView } from './touchNavigation'

function rig(free = true) {
  let t = 0
  const calls: string[] = []
  const dollies: number[] = []
  let rotate = true
  const view: TouchView = {
    free: () => free,
    anchor: (at: ClientPoint) => calls.push(`anchor ${at.clientX},${at.clientY}`),
    dolly: (_at, deltaY) => {
      calls.push('dolly')
      dollies.push(deltaY)
    },
    panBy: () => calls.push('pan'),
    lookBy: () => calls.push('look'),
    focusAt: (at) => calls.push(`focus ${at.clientX},${at.clientY}`),
    setRotate: (enabled) => {
      rotate = enabled
    },
  }
  const nav = new TouchNavigation(view, () => t)
  const finger = (pointerId: number, clientX: number, clientY: number) =>
    ({ pointerId, clientX, clientY, pointerType: 'touch' })
  return {
    nav,
    calls,
    dollies,
    finger,
    rotate: () => rotate,
    wait: (ms: number) => {
      t += ms
    },
  }
}

describe('TouchNavigation', () => {
  it('leaves a one-finger drag to the orbit controls: no pan, no flight, no turn of its own', () => {
    const { nav, calls, finger, rotate } = rig()
    nav.down(finger(1, 100, 100))
    nav.move(finger(1, 160, 140))
    nav.up(finger(1, 160, 140))
    expect(calls).toEqual([])
    expect(rotate()).toBe(true)
  })

  it('anchors a pinch once at the midpoint, then flies with the wheel\'s sign', () => {
    const { nav, calls, dollies, finger } = rig()
    nav.down(finger(1, 100, 200))
    nav.down(finger(2, 200, 200))
    nav.move(finger(2, 300, 200)) // spread to twice the distance: fly in
    nav.move(finger(2, 400, 200))
    expect(calls.filter((c) => c.startsWith('anchor'))).toEqual(['anchor 200,200'])
    // Spreading the fingers flies toward the scene, as a wheel turned away from you (negative
    // deltaY) does; and twice the distance is PINCH_PX * ln 2 of wheel.
    expect(dollies[0]).toBeCloseTo(-PINCH_PX * Math.log(2), 9)
    expect(dollies.every((d) => d < 0)).toBe(true)
  })

  it('flies back out when the fingers close', () => {
    const { nav, dollies, finger } = rig()
    nav.down(finger(1, 100, 200))
    nav.down(finger(2, 300, 200))
    nav.move(finger(2, 200, 200))
    expect(dollies.length).toBeGreaterThan(0)
    expect(dollies.every((d) => d > 0)).toBe(true)
  })

  it('pans and twists with two fingers', () => {
    const { nav, calls, finger } = rig()
    nav.down(finger(1, 100, 100))
    nav.down(finger(2, 200, 100))
    nav.move(finger(1, 130, 130))
    nav.move(finger(2, 230, 130)) // both moved together: a pan
    expect(calls).toContain('pan')
    nav.move(finger(2, 130, 230)) // the pair turned a quarter: a twist
    expect(calls).toContain('look')
  })

  it('turns rotate off from the second finger until the last one lifts', () => {
    const { nav, finger, rotate } = rig()
    nav.down(finger(1, 100, 100))
    expect(rotate()).toBe(true)
    nav.down(finger(2, 200, 100))
    expect(rotate()).toBe(false)
    nav.up(finger(2, 200, 100))
    expect(rotate()).toBe(false) // the finger left behind must not start a turn
    nav.up(finger(1, 100, 100))
    expect(rotate()).toBe(true)
  })

  it('gives rotate back when the browser cancels the fingers', () => {
    const { nav, finger, rotate } = rig()
    nav.down(finger(1, 100, 100))
    nav.down(finger(2, 200, 100))
    nav.cancel(finger(1, 100, 100))
    nav.cancel(finger(2, 200, 100))
    expect(rotate()).toBe(true)
  })

  it('flies to what a double tap names', () => {
    const { nav, calls, finger, wait } = rig()
    nav.down(finger(1, 120, 80))
    wait(50)
    nav.up(finger(1, 120, 80))
    wait(100)
    nav.down(finger(2, 122, 81))
    wait(50)
    nav.up(finger(2, 122, 81))
    expect(calls).toEqual(['focus 122,81'])
  })

  it('moves nothing through a sensor\'s camera, but still counts the fingers', () => {
    const { nav, calls, finger, rotate } = rig(false)
    nav.down(finger(1, 100, 100))
    nav.down(finger(2, 200, 100))
    nav.move(finger(2, 400, 160))
    expect(calls).toEqual([])
    expect(nav.fingers).toBe(2)
    nav.up(finger(1, 100, 100))
    nav.up(finger(2, 400, 160))
    expect(rotate()).toBe(true)
  })

  it('ignores a mouse and a pen', () => {
    const { nav, calls, rotate } = rig()
    for (const pointerType of ['mouse', 'pen']) {
      nav.down({ pointerId: 1, clientX: 0, clientY: 0, pointerType })
      nav.down({ pointerId: 2, clientX: 100, clientY: 0, pointerType })
      nav.move({ pointerId: 2, clientX: 300, clientY: 0, pointerType })
    }
    expect(calls).toEqual([])
    expect(rotate()).toBe(true)
    expect(nav.fingers).toBe(0)
  })
})
