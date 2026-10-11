import { describe, expect, it } from 'vitest'
import { BufferOwner, failureText } from './bufferOwner'
import { RobovastError } from '@/lib/robovastClient'

// The config editor autosaves its buffer to the selected file. When another file is selected and
// its read fails, the buffer still holds the previous file's text, which must never be written there.
describe('BufferOwner', () => {
  const a = { id: 'ws-1', path: 'a.vast' }
  const b = { id: 'ws-1', path: 'b.vast' }

  it('writes a file only once its own text is in the buffer', () => {
    const owner = new BufferOwner()
    expect(owner.mayWrite(a)).toBe(false)
    owner.begin()
    owner.loaded(a)
    expect(owner.mayWrite(a)).toBe(true)
    expect(owner.mayWrite(b)).toBe(false)
  })

  it('does not write the previous file into one whose read failed', () => {
    const owner = new BufferOwner()
    owner.begin()
    owner.loaded(a)
    owner.begin() // b selected; its read then fails, so loaded(b) never comes
    expect(owner.mayWrite(b)).toBe(false)
    expect(owner.mayWrite(a)).toBe(false)
  })

  it('does not write while the next read is still in flight', () => {
    const owner = new BufferOwner()
    owner.loaded(a)
    owner.begin()
    expect(owner.mayWrite(b)).toBe(false)
    owner.loaded(b)
    expect(owner.mayWrite(b)).toBe(true)
  })

  it('tells the other workspace apart from the same path', () => {
    const owner = new BufferOwner()
    owner.loaded(a)
    expect(owner.mayWrite({ id: 'ws-2', path: 'a.vast' })).toBe(false)
  })
})

describe('failureText', () => {
  it("is the service's sentence, without the error's class name", () => {
    expect(failureText(new RobovastError(409, 'a search campaign is reading this workspace'))).toBe(
      'a search campaign is reading this workspace',
    )
    expect(failureText('plain')).toBe('plain')
  })
})
