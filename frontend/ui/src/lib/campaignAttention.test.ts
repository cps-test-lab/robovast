import { describe, expect, it } from 'vitest'

import { attentionFor } from './campaignAttention'

describe('attentionFor', () => {
  it('marks nothing on a campaign that is saying nothing', () => {
    expect(attentionFor({})).toBeNull()
    expect(attentionFor({ attention: '' })).toBeNull()
    expect(attentionFor({ attention: '   ' })).toBeNull()
    expect(attentionFor(null)).toBeNull()
    expect(attentionFor(undefined)).toBeNull()
  })

  it("carries the campaign's own sentence, numbers and all", () => {
    const a = attentionFor({
      attention: '3 run(s) lost so far: OOM-killed at memory this campaign MEASURED -- sut on '
        + "node n1 at 0.13GiB (measured: 0.10GiB). The campaign keeps going; to bound it, "
        + 'state `calibration.min.memory`.',
    })
    expect(a).not.toBeNull()
    expect(a!.title).toBe('This campaign needs attention')
    // The reader acts on the numbers and on what to state, so neither may be summarised away.
    expect(a!.detail).toContain('0.13GiB')
    expect(a!.detail).toContain('calibration.min.memory')
  })
})
