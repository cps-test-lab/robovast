// Shared-candidate code: this file imports nothing -- keep it that way (see README.md here).
//
// Fingers to gestures. Touch pointer events go in; out come the gestures the viewport acts on: a
// tap, a double tap, a one-finger drag, and for two fingers together a pinch (how much closer or
// further apart, about their midpoint), a pan (how far the midpoint travelled) and a twist (how far
// the pair turned). Recognising them here, apart from three and the DOM, is what lets them be tested
// over synthetic events.
//
// Two rules make two fingers usable on a phone:
//
// - **A gesture of two fingers is over when either lifts, and the finger left behind starts
//   nothing.** Lifting one finger a moment before the other is how every pinch ends; if the remaining
//   finger then began a one-finger drag, every pinch would end in a lurch of the view.
// - **Two fingers never hold still relative to each other.** Their distance and angle wobble by a few
//   percent and degrees on every event, and the midpoint jumps with each finger's own sampling. A pan
//   with two fingers would otherwise also be a small flight and a small turn, fighting it. So the pan
//   follows the midpoint through a low-pass filter, and a pinch or a twist acts only once it has gone
//   past a dead zone (PINCH_START, TWIST_START) measured from where the fingers landed -- then with
//   all of the change at once, so nothing the person did is lost, and step by step from there.

export type Gesture =
  | { type: 'tap'; x: number; y: number }
  | { type: 'doubletap'; x: number; y: number }
  | { type: 'drag'; phase: 'start' | 'move' | 'end'; x: number; y: number; dx: number; dy: number }
  | { type: 'pinch'; x: number; y: number; scale: number; first: boolean }
  | { type: 'pan'; dx: number; dy: number }
  | { type: 'twist'; angle: number }
  | { type: 'end' }

/** The part of a `PointerEvent` the recogniser reads, so tests can hand it plain objects. */
export interface PointerLike {
  pointerId: number
  clientX: number
  clientY: number
}

/** Below this travel, in CSS pixels, a finger has not moved: a tap, not the start of a drag. */
export const SLOP_PX = 8
/** A press longer than this is not a tap. */
export const TAP_MS = 300
/** Two taps are a double tap when the second finger lands within this of the first one lifting --
 *  Android's own measure and timeout -- and within DOUBLE_TAP_PX of it. */
export const DOUBLE_TAP_MS = 300
export const DOUBLE_TAP_PX = 30
/** How far a pinch goes before it acts, as |log(distance / distance at landing)|: about 6 %. */
export const PINCH_START = 0.06
/** How far a twist goes before it acts, in radians: 12 degrees. */
export const TWIST_START = (12 * Math.PI) / 180
/** The share of the midpoint's latest move a pan takes per event: a low-pass filter on the pair. */
export const PAN_SMOOTHING = 0.5

interface Point {
  x: number
  y: number
}

interface Press {
  at: Point
  t: number
  moved: boolean
  /** A second finger came down: this press is neither a tap nor a drag, now or later. */
  spoiled: boolean
}

interface Pair {
  /** Distance and angle where the fingers landed: what the dead zones are measured from. */
  dist0: number
  angle0: number
  /** Distance and angle last acted on: what the next step is measured from. */
  dist: number
  angle: number
  /** The filtered midpoint. */
  mid: Point
  pinching: boolean
  twisting: boolean
  firstPinch: boolean
}

/** The signed difference of two angles, wrapped into (-pi, pi]. */
const wrapAngle = (a: number) => Math.atan2(Math.sin(a), Math.cos(a))

export class TouchGestures {
  private fingers = new Map<number, Point>()
  private press: Press | null = null
  private pair: Pair | null = null
  private lastTap: { at: Point; t: number } | null = null

  constructor(
    private readonly emit: (gesture: Gesture) => void,
    private readonly now: () => number = () => performance.now(),
  ) {}

  /** How many fingers are down. */
  get count(): number {
    return this.fingers.size
  }

  down(e: PointerLike): void {
    this.fingers.set(e.pointerId, { x: e.clientX, y: e.clientY })
    if (this.fingers.size === 1) {
      this.press = { at: { x: e.clientX, y: e.clientY }, t: this.now(), moved: false, spoiled: false }
      return
    }
    // A second finger ends a drag in progress -- with an `end`, so whatever it turned settles --
    // and takes the tap away from the first.
    if (this.press && this.press.moved && !this.press.spoiled) this.dragEnd(this.press)
    if (this.press) this.press.spoiled = true
    this.pair = this.landed()
  }

  move(e: PointerLike): void {
    if (!this.fingers.has(e.pointerId)) return
    this.fingers.set(e.pointerId, { x: e.clientX, y: e.clientY })
    if (this.fingers.size >= 2) {
      this.movePair()
      return
    }
    const press = this.press
    if (!press || press.spoiled) return
    const dx = e.clientX - press.at.x
    const dy = e.clientY - press.at.y
    if (!press.moved && Math.hypot(dx, dy) > SLOP_PX) {
      press.moved = true
      this.emit({ type: 'drag', phase: 'start', x: press.at.x, y: press.at.y, dx: 0, dy: 0 })
    }
    if (press.moved) this.emit({ type: 'drag', phase: 'move', x: e.clientX, y: e.clientY, dx, dy })
  }

  up(e: PointerLike): void {
    const at = this.fingers.get(e.pointerId)
    if (!at) return
    this.fingers.delete(e.pointerId)
    if (this.fingers.size >= 1) {
      // One of two lifted: the two-finger gesture is over, and the finger left starts nothing (its
      // press is spoiled). A third finger's lifting re-measures the two that remain.
      if (this.pair) this.emit({ type: 'end' })
      this.pair = this.fingers.size >= 2 ? this.landed() : null
      return
    }
    const press = this.press
    this.press = null
    this.pair = null
    if (!press || press.spoiled) {
      this.emit({ type: 'end' })
      return
    }
    if (press.moved) {
      this.dragEnd(press, at)
      return
    }
    const t = this.now()
    if (t - press.t > TAP_MS) return
    const last = this.lastTap
    if (last && press.t - last.t < DOUBLE_TAP_MS
        && Math.hypot(last.at.x - at.x, last.at.y - at.y) < DOUBLE_TAP_PX) {
      this.lastTap = null
      this.emit({ type: 'doubletap', x: at.x, y: at.y })
    } else {
      this.lastTap = { at, t }
      this.emit({ type: 'tap', x: at.x, y: at.y })
    }
  }

  /** The browser took the pointer away (a scroll it started, a system gesture): forget it. */
  cancel(e: PointerLike): void {
    if (!this.fingers.delete(e.pointerId)) return
    if (this.fingers.size > 0) {
      if (this.pair) this.emit({ type: 'end' })
      this.pair = this.fingers.size >= 2 ? this.landed() : null
      if (this.press) this.press.spoiled = true
      return
    }
    this.press = null
    this.pair = null
    this.emit({ type: 'end' })
  }

  private movePair(): void {
    const now = this.measure()
    const pair = this.pair
    if (!now || !pair) return

    const mid = {
      x: pair.mid.x + (now.mid.x - pair.mid.x) * PAN_SMOOTHING,
      y: pair.mid.y + (now.mid.y - pair.mid.y) * PAN_SMOOTHING,
    }
    if (mid.x !== pair.mid.x || mid.y !== pair.mid.y) {
      this.emit({ type: 'pan', dx: mid.x - pair.mid.x, dy: mid.y - pair.mid.y })
    }
    pair.mid = mid

    if (!pair.pinching && pair.dist0 > 0 && now.dist > 0
        && Math.abs(Math.log(now.dist / pair.dist0)) > PINCH_START) {
      pair.pinching = true
    }
    if (pair.pinching && pair.dist > 0 && now.dist > 0 && now.dist !== pair.dist) {
      this.emit({ type: 'pinch', x: now.mid.x, y: now.mid.y, scale: now.dist / pair.dist, first: pair.firstPinch })
      pair.firstPinch = false
      pair.dist = now.dist
    }

    if (!pair.twisting && Math.abs(wrapAngle(now.angle - pair.angle0)) > TWIST_START) pair.twisting = true
    if (pair.twisting) {
      const turn = wrapAngle(now.angle - pair.angle)
      if (turn !== 0) this.emit({ type: 'twist', angle: turn })
      pair.angle = now.angle
    }
  }

  private dragEnd(press: Press, at: Point = press.at): void {
    this.emit({ type: 'drag', phase: 'end', x: at.x, y: at.y, dx: at.x - press.at.x, dy: at.y - press.at.y })
  }

  /** The first two fingers down: their distance, the angle of the line through them, the midpoint. */
  private measure(): { dist: number; angle: number; mid: Point } | null {
    const [a, b] = [...this.fingers.values()]
    if (!a || !b) return null
    return {
      dist: Math.hypot(b.x - a.x, b.y - a.y),
      angle: Math.atan2(b.y - a.y, b.x - a.x),
      mid: { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 },
    }
  }

  /** A pair as it lands: where its dead zones are measured from until a pinch or twist starts. */
  private landed(): Pair | null {
    const m = this.measure()
    if (!m) return null
    return {
      dist0: m.dist, angle0: m.angle, dist: m.dist, angle: m.angle, mid: m.mid,
      pinching: false, twisting: false, firstPinch: true,
    }
  }
}
