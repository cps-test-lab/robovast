import { describe, expect, it } from 'vitest'
import { readUploadProgress, type Status } from './robovastClient'

// `extra` is an open dict on the wire, so this reader is the only thing standing between
// the campaign card and whatever the service put there.
const status = (upload: unknown): Status =>
  ({ extra: upload === undefined ? {} : { upload } }) as unknown as Status

describe('readUploadProgress', () => {
  it('returns null when the campaign is not uploading', () => {
    expect(readUploadProgress(status(undefined))).toBeNull()
    expect(readUploadProgress(undefined)).toBeNull()
  })

  it('reads the source counters and the wire count as separate numbers', () => {
    const up = readUploadProgress(
      status({ sent: 40, source_done: 50, source_total: 200, percent: 25, rate: 10 }),
    )
    expect(up).toEqual({ sent: 40, sourceDone: 50, sourceTotal: 200, percent: 25, rate: 10 })
  })

  it('reports no percentage when there is no source total', () => {
    // The streamed path before a total is known: a bar pinned at 0% through a multi-hour
    // upload is the exact failure this replaced, so the caller must get null and go
    // indeterminate rather than render a fabricated zero.
    const up = readUploadProgress(status({ sent: 4096, source_total: 0, percent: 0 }))
    expect(up?.percent).toBeNull()
    expect(up?.sent).toBe(4096)
  })

  it('survives a record missing fields or carrying non-numbers', () => {
    const up = readUploadProgress(status({ sent: null, source_done: 'lots', percent: 12 }))
    expect(up).toEqual({ sent: 0, sourceDone: 0, sourceTotal: 0, percent: null, rate: null })
  })
})
