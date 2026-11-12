// A preview the web UI waits for by polling, as `robovast.client.preview.await_preview` does for
// the CLI and the MCP tool. A waited call holds one request for as long as the campaign's image
// takes to build and says nothing meanwhile; polled (`wait=false`), the service answers at once
// with `composing` and what it waits on, so the caller can show it.

import type { PreviewResponse } from './robovastClient'

/** How often a waiting caller asks again. */
export const POLL_MS = 1000

/** The first poll after the call that starts the preview: one that needs no build lands in a
 *  fraction of a second, and a whole interval would be a delay the waited call never had. */
export const FIRST_POLL_MS = 100

type Previewer = (
  workspaceId: string,
  maxConfigs: number,
  path: string,
  wait: boolean,
) => Promise<PreviewResponse>

export interface AwaitPreviewOptions {
  /** Hears every `composing` answer, to show its stage or progress. */
  onComposing?: (answer: PreviewResponse) => void
  /** Stops the polling; the promise then rejects with an `AbortError`. */
  signal?: AbortSignal
  pollMs?: number
  sleep?: (ms: number) => Promise<void>
}

/** The preview failed: its composition, or the image build it waited on. */
export class PreviewFailed extends Error {
  override name = 'PreviewFailed'
}

const wait = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms))

function checkAborted(signal?: AbortSignal) {
  if (signal?.aborted) throw new DOMException('the preview was no longer wanted', 'AbortError')
}

/** Poll `preview(workspaceId, 0, path, false)` until it is `ready`, and return it.
 *  A `failed` answer rejects with {@link PreviewFailed} carrying the service's reason. */
export async function awaitPreview(
  preview: Previewer,
  workspaceId: string,
  path: string,
  { onComposing, signal, pollMs = POLL_MS, sleep = wait }: AwaitPreviewOptions = {},
): Promise<PreviewResponse> {
  let pause = Math.min(pollMs, FIRST_POLL_MS)
  for (;;) {
    checkAborted(signal)
    const answer = await preview(workspaceId, 0, path, false)
    checkAborted(signal)
    if (answer.state === 'failed') throw new PreviewFailed(answer.error || 'the preview failed')
    if (answer.state !== 'composing') return answer
    onComposing?.(answer)
    await sleep(pause)
    pause = pollMs
  }
}

/** One line on what a `composing` preview is doing: its variation counter, else what it waits
 *  on before it can compose, such as the build of the campaign's image. */
export function composingLabel(answer: PreviewResponse | undefined): string {
  const progress = answer?.progress
  if (progress) return `composing: ${progress.done} of ${progress.total} variations done`
  return answer?.stage || 'composing configurations…'
}
