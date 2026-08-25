import { describe, expect, it } from 'vitest'
import { isLoadFailure } from './ErrorBoundary'

// This predicate is the whole switch between the boundary's two faces: a chunk that did not
// arrive gets the amber panel with Reload, anything else gets the red one with a stack trace.
// The engines word the identical failure three different ways and none of them is a standard,
// so the pattern can only be a list — and a list is exactly what gets "tidied" by someone who
// has seen one of the three. Losing a phrase here does not break a test elsewhere; it quietly
// shows a missing chunk as a code bug and takes the Reload button away, which is how the
// original report ("Results stopped working / can't access property ...") read.
const MISSING_CHUNK = [
  ['Chromium', 'Failed to fetch dynamically imported module: https://x/assets/Page-a1.js'],
  ['Firefox', 'error loading dynamically imported module: https://x/assets/Page-a1.js'],
  ['Safari', 'Importing a module script failed.'],
  ['webpack-era wording, still in the wild', 'Loading chunk 42 failed.'],
]

describe('isLoadFailure', () => {
  it.each(MISSING_CHUNK)('recognises %s', (_engine, message) => {
    expect(isLoadFailure(new Error(message))).toBe(true)
  })

  it('leaves a real bug on the red branch', () => {
    // What the swallowed preload error used to surface as. It must NOT be called transient:
    // retrying a genuine TypeError just reproduces it.
    expect(isLoadFailure(new TypeError("can't access property \"ResultsPage\", e is undefined")))
      .toBe(false)
  })

  it('does not match a service error that merely mentions a module', () => {
    expect(isLoadFailure(new Error('plugin module roqsim.nav is not installed'))).toBe(false)
  })
})
