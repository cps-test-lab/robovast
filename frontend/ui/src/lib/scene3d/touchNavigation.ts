// Shared-candidate code: imports only its siblings here (see README.md).
//
// What fingers do to the view. `touch.ts` says which gesture the fingers are making; this decides
// what each one moves, against the few operations a viewport offers (`TouchView`). Kept apart from
// `viewport.ts` so the mapping is testable without a WebGL context or a DOM.
//
// - one finger turns the view about the surface under it. That is the orbit controls' own rotate,
//   not done here: the viewport anchors the pivot under every press, so it is the right surface.
// - two fingers pinch to fly toward their midpoint (the wheel's flight, `cursorDolly.ts`), pan the
//   view so what is under them stays under them, and twist to turn the view in place.
// - a double tap flies to frame what was tapped, as a double-click does.
//
// From the second finger until the last one lifts, the orbit controls may not rotate: they would
// otherwise take the finger left behind after a pinch as the start of a turn.

import { TouchGestures, type Gesture, type PointerLike } from './touch'

/** Wheel pixels per unit of log-scale of a pinch: spreading the fingers to twice their distance flies
 *  about as far as two notches of a mouse wheel, so a pinch and the wheel feel alike. */
export const PINCH_PX = 290

/** A point on the canvas, in client coordinates -- what the viewport's pickers take. */
export interface ClientPoint {
  clientX: number
  clientY: number
}

/** What a viewport offers fingers to move. */
export interface TouchView {
  /** Whether the free camera is in use. Through a sensor's camera fingers move nothing, as the
   *  wheel does not. */
  free(): boolean
  /** Put the orbit pivot on the surface under `at`, without moving the camera. */
  anchor(at: ClientPoint): void
  /** Fly as the wheel would for a wheel delta of `deltaY` pixels at `at`. */
  dolly(at: ClientPoint, deltaY: number): void
  /** Move the view by a screen offset in CSS pixels, keeping what is under the fingers under them. */
  panBy(dx: number, dy: number): void
  /** Turn the view in place: `yaw` about the world's vertical, `pitch` up. Radians. */
  lookBy(yaw: number, pitch: number): void
  /** Fly to frame what is under `at`. */
  focusAt(at: ClientPoint): void
  /** Let the orbit controls turn the view with one finger, or not. */
  setRotate(enabled: boolean): void
}

/** A pointer event as the navigation reads it: which kind of pointer, and where. */
export interface TouchPointer extends PointerLike {
  pointerType: string
}

export class TouchNavigation {
  private readonly gestures: TouchGestures

  constructor(private readonly view: TouchView, now?: () => number) {
    this.gestures = new TouchGestures((g) => this.act(g), now)
  }

  /** How many fingers are down. */
  get fingers(): number {
    return this.gestures.count
  }

  down(e: TouchPointer): void {
    if (e.pointerType !== 'touch') return
    this.gestures.down(e)
    if (this.gestures.count >= 2) this.view.setRotate(false)
  }

  move(e: TouchPointer): void {
    if (e.pointerType === 'touch') this.gestures.move(e)
  }

  up(e: TouchPointer): void {
    if (e.pointerType !== 'touch') return
    this.gestures.up(e)
    if (this.gestures.count === 0) this.view.setRotate(true)
  }

  cancel(e: TouchPointer): void {
    if (e.pointerType !== 'touch') return
    this.gestures.cancel(e)
    if (this.gestures.count === 0) this.view.setRotate(true)
  }

  private act(g: Gesture): void {
    if (!this.view.free()) return
    switch (g.type) {
      case 'pinch': {
        const at = { clientX: g.x, clientY: g.y }
        // Measured once, at the midpoint, when the pinch starts: the flight is toward what lies
        // between the fingers, at a pace set by how far away that is.
        if (g.first) this.view.anchor(at)
        this.view.dolly(at, -Math.log(g.scale) * PINCH_PX)
        break
      }
      case 'pan':
        this.view.panBy(g.dx, g.dy)
        break
      case 'twist':
        this.view.lookBy(g.angle, 0)
        break
      case 'doubletap':
        this.view.focusAt({ clientX: g.x, clientY: g.y })
        break
      // A tap selects nothing in this viewer, and a one-finger drag is the orbit controls' rotate.
      default:
        break
    }
  }
}
