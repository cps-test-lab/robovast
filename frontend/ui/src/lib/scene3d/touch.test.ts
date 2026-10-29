// Fingers to gestures, over synthetic pointer events and an injected clock.
import { describe, expect, it } from 'vitest'
import {
  DOUBLE_TAP_MS,
  DOUBLE_TAP_PX,
  PINCH_START,
  SLOP_PX,
  TAP_MS,
  TWIST_START,
  TouchGestures,
  type Gesture,
} from './touch'

function rig() {
  let t = 0
  const out: Gesture[] = []
  const g = new TouchGestures((x) => out.push(x), () => t)
  const p = (pointerId: number, clientX: number, clientY: number) => ({ pointerId, clientX, clientY })
  return {
    g,
    out,
    p,
    wait: (ms: number) => {
      t += ms
    },
    types: () => out.map((o) => o.type),
    of: <T extends Gesture['type']>(type: T) =>
      out.filter((o): o is Extract<Gesture, { type: T }> => o.type === type),
  }
}

describe('TouchGestures: one finger', () => {
  it('takes a short press that does not travel as a tap', () => {
    const { g, p, wait, out } = rig()
    g.down(p(1, 100, 100))
    wait(80)
    g.up(p(1, 100 + SLOP_PX - 1, 100))
    expect(out).toEqual([{ type: 'tap', x: 100, y: 100 }])
  })

  it('takes a press held past TAP_MS as nothing at all', () => {
    const { g, p, wait, out } = rig()
    g.down(p(1, 100, 100))
    wait(TAP_MS + 1)
    g.up(p(1, 100, 100))
    expect(out).toEqual([])
  })

  it('takes two taps close in time and place as a tap, then a double tap', () => {
    const { g, p, wait, types } = rig()
    g.down(p(1, 100, 100))
    wait(60)
    g.up(p(1, 100, 100))
    // Measured from the first lift to the second landing, so how long the second press is held
    // (up to a tap's length) does not count against it.
    wait(DOUBLE_TAP_MS - 10)
    g.down(p(2, 110, 104))
    wait(TAP_MS - 10)
    g.up(p(2, 110, 104))
    expect(types()).toEqual(['tap', 'doubletap'])
  })

  it('does not take two taps as a double tap when they are too far apart in time or place', () => {
    const late = rig()
    late.g.down(late.p(1, 100, 100))
    late.g.up(late.p(1, 100, 100))
    late.wait(DOUBLE_TAP_MS + 1)
    late.g.down(late.p(2, 100, 100))
    late.g.up(late.p(2, 100, 100))
    expect(late.types()).toEqual(['tap', 'tap'])

    const far = rig()
    far.g.down(far.p(1, 100, 100))
    far.g.up(far.p(1, 100, 100))
    far.g.down(far.p(2, 100 + DOUBLE_TAP_PX, 100))
    far.g.up(far.p(2, 100 + DOUBLE_TAP_PX, 100))
    expect(far.types()).toEqual(['tap', 'tap'])
  })

  it('takes a finger that travels past the slop as a drag, start to end, measured from the press', () => {
    const { g, p, out } = rig()
    g.down(p(1, 0, 0))
    g.move(p(1, SLOP_PX - 1, 0)) // within the slop: nothing yet
    g.move(p(1, 40, 10))
    g.up(p(1, 40, 10))
    expect(out).toEqual([
      { type: 'drag', phase: 'start', x: 0, y: 0, dx: 0, dy: 0 },
      { type: 'drag', phase: 'move', x: 40, y: 10, dx: 40, dy: 10 },
      { type: 'drag', phase: 'end', x: 40, y: 10, dx: 40, dy: 10 },
    ])
  })
})

describe('TouchGestures: two fingers', () => {
  it('ends a one-finger drag when a second finger lands, and the finger left starts nothing', () => {
    const { g, p, out, types } = rig()
    g.down(p(1, 0, 0))
    g.move(p(1, 30, 0))
    g.down(p(2, 100, 0))
    expect(out.at(-1)).toMatchObject({ type: 'drag', phase: 'end' })
    g.up(p(2, 100, 0))
    g.move(p(1, 80, 0))
    g.up(p(1, 80, 0))
    expect(types()).toEqual(['drag', 'drag', 'drag', 'end', 'end'])
  })

  it('takes no tap from a press a second finger joined', () => {
    const { g, p, types } = rig()
    g.down(p(1, 0, 0))
    g.down(p(2, 100, 0))
    g.up(p(2, 100, 0))
    g.up(p(1, 0, 0))
    expect(types()).not.toContain('tap')
  })

  it('does not pinch within the dead zone, and past it pinches with all of the change', () => {
    const { g, p, of } = rig()
    g.down(p(1, 100, 100))
    g.down(p(2, 200, 100))
    const inside = 100 * Math.exp(PINCH_START * 0.8)
    g.move(p(2, 100 + inside, 100))
    expect(of('pinch')).toEqual([])
    g.move(p(2, 300, 100)) // twice the landing distance
    const [first] = of('pinch')
    expect(first.first).toBe(true)
    expect(first.scale).toBeCloseTo(2, 9) // from the landing distance, not from the dead zone's edge
    expect(first.x).toBe(200)
    g.move(p(2, 400, 100))
    const [, second] = of('pinch')
    expect(second.first).toBe(false)
    expect(second.scale).toBeCloseTo(300 / 200, 9) // step by step from there
  })

  it('does not twist within the dead zone, and past it twists with all of the turn', () => {
    const { g, p, of } = rig()
    g.down(p(1, 0, 0))
    g.down(p(2, 100, 0))
    const small = TWIST_START * 0.8
    g.move(p(2, 100 * Math.cos(small), 100 * Math.sin(small)))
    expect(of('twist')).toEqual([])
    const big = TWIST_START * 2
    g.move(p(2, 100 * Math.cos(big), 100 * Math.sin(big)))
    const total = of('twist').reduce((sum, t) => sum + t.angle, 0)
    expect(total).toBeCloseTo(big, 9)
    expect(of('pinch')).toEqual([]) // the distance never changed
  })

  it('pans the midpoint through the low-pass filter: half of each move', () => {
    const { g, p, of } = rig()
    g.down(p(1, 0, 0))
    g.down(p(2, 100, 0))
    g.move(p(1, 40, 0)) // midpoint 50 -> 70 (one finger moved)
    g.move(p(2, 140, 0)) // midpoint -> 90
    const pans = of('pan')
    expect(pans[0].dx).toBeCloseTo(10, 9) // half of 20
    expect(pans[1].dx).toBeCloseTo(15, 9) // half of the 30 still to go
  })

  it('ends the two-finger gesture when either lifts, and the finger left starts nothing', () => {
    const { g, p, types } = rig()
    g.down(p(1, 100, 100))
    g.down(p(2, 200, 100))
    g.move(p(2, 320, 100))
    g.up(p(2, 320, 100))
    const afterLift = types().length
    g.move(p(1, 160, 160))
    g.move(p(1, 220, 220))
    expect(types().length).toBe(afterLift) // no drag, no tap from the one left
    g.up(p(1, 220, 220))
    expect(types().at(-1)).toBe('end')
  })

  it('forgets every finger on cancel and says the gesture ended', () => {
    const { g, p, types } = rig()
    g.down(p(1, 0, 0))
    g.down(p(2, 100, 0))
    g.cancel(p(1, 0, 0))
    g.cancel(p(2, 100, 0))
    expect(g.count).toBe(0)
    expect(types().at(-1)).toBe('end')
    // A fresh press afterwards is a fresh tap.
    g.down(p(3, 50, 50))
    g.up(p(3, 50, 50))
    expect(types().at(-1)).toBe('tap')
  })

  it('ignores events of pointers it never saw go down', () => {
    const { g, p, out } = rig()
    g.move(p(9, 10, 10))
    g.up(p(9, 10, 10))
    g.cancel(p(9, 10, 10))
    expect(out).toEqual([])
  })
})
