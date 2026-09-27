import { describe, expect, it } from 'vitest'
import { minimalVast, VAST_BODY } from './vastTemplate'

describe('minimalVast', () => {
  it('declares the version the schema publishes as its default', () => {
    const text = minimalVast({ properties: { version: { type: 'integer', default: 6 } } })
    expect(text).toBe(`version: 6\n${VAST_BODY}`)
  })

  it('refuses a schema without an integer default instead of guessing one', () => {
    expect(() => minimalVast({ properties: { version: { type: 'integer' } } })).toThrow(/no default/)
    expect(() => minimalVast({})).toThrow(/no default/)
  })
})
