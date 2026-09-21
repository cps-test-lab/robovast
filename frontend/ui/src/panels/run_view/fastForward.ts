// The fast-forward button's behaviour, kept apart from the panel so it is testable without a DOM.

import type { PlaybackClock } from '@robovast/panel-kit'

// The rates the fast-forward button cycles through, in order; it wraps back to 1× from the last one,
// so a single button reaches every speed and always has a way back to real time.
const SPEEDS = [1, 2, 4, 8]

export function nextSpeed(speed: number): number {
  const i = SPEEDS.indexOf(speed)
  // A speed set from outside this list still steps somewhere useful rather than sticking.
  return i < 0 ? 2 : SPEEDS[(i + 1) % SPEEDS.length]
}

/** Step the clock to the next speed, and start playback if it is paused: pressing fast-forward
 *  asks to watch the run faster, which a paused clock would silently not do. While playing it
 *  only cycles the speed, so the button never pauses. */
export function fastForward(clock: PlaybackClock): void {
  const { speed, playing } = clock.getSnapshot()
  clock.setSpeed(nextSpeed(speed))
  if (!playing) clock.play()
}
