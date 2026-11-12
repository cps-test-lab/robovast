import { describe, expect, it, vi } from 'vitest'
import { awaitPreview, composingLabel, FIRST_POLL_MS, PreviewFailed } from './awaitPreview'
import type { PreviewResponse } from './robovastClient'

const STAGE =
  "building the campaign's image for container 'simulation' (build b-1); the simulator is asked in it once it is built"

function answer(fields: Partial<PreviewResponse>): PreviewResponse {
  return { state: 'composing', configurations: [], ...fields } as PreviewResponse
}

/** A service that answers each poll with the next of *answers*, recording how it was asked. */
function service(...answers: PreviewResponse[]) {
  return vi.fn(async (_id: string, _max: number, _path: string, _wait: boolean) => {
    const next = answers.shift()
    if (!next) throw new Error('asked once more than the test expects')
    return next
  })
}

const noSleep = vi.fn(async (_ms: number) => {})

describe('awaitPreview', () => {
  it('polls in the background until the preview is ready, never making a waited call', async () => {
    const ready = answer({ state: 'ready', configs: 2 })
    const preview = service(answer({ stage: STAGE }), answer({ stage: STAGE }), ready)
    const said: string[] = []

    const got = await awaitPreview(preview, 'ws', 'campaign.vast', {
      sleep: noSleep,
      onComposing: (a) => said.push(composingLabel(a)),
    })

    expect(got).toBe(ready)
    expect(preview.mock.calls).toEqual(Array(3).fill(['ws', 0, 'campaign.vast', false]))
    expect(said).toEqual([STAGE, STAGE])
  })

  it('asks again quickly once, then at the poll interval', async () => {
    const sleep = vi.fn(async (_ms: number) => {})
    const preview = service(answer({}), answer({}), answer({ state: 'ready' }))

    await awaitPreview(preview, 'ws', '', { sleep, pollMs: 1000 })

    expect(sleep.mock.calls).toEqual([[FIRST_POLL_MS], [1000]])
  })

  it('rejects with the reason a failed preview carries', async () => {
    const preview = service(
      answer({ stage: STAGE }),
      answer({ state: 'failed', error: "the campaign's image for container 'simulation' did not build" }),
    )

    const waiting = awaitPreview(preview, 'ws', '', { sleep: noSleep })

    await expect(waiting).rejects.toBeInstanceOf(PreviewFailed)
    await expect(waiting).rejects.toThrow("container 'simulation' did not build")
  })

  it('stops polling once aborted', async () => {
    const controller = new AbortController()
    const preview = service(answer({}), answer({ state: 'ready' }))
    const sleep = vi.fn(async (_ms: number) => controller.abort())

    const waiting = awaitPreview(preview, 'ws', '', { sleep, signal: controller.signal })

    await expect(waiting).rejects.toMatchObject({ name: 'AbortError' })
    expect(preview).toHaveBeenCalledTimes(1)
  })
})

describe('composingLabel', () => {
  it('says the variation counter, else the stage, else that it composes', () => {
    expect(composingLabel(answer({ progress: { done: 3, total: 8 }, stage: STAGE }))).toBe(
      'composing: 3 of 8 variations done',
    )
    expect(composingLabel(answer({ stage: STAGE }))).toBe(STAGE)
    expect(composingLabel(answer({}))).toBe('composing configurations…')
  })
})
