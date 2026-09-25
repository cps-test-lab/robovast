// Fetch one picture and say why when there is none. A frame route's 404 carries a sentence -- which
// frame was asked for and why the run has none -- and the panel shows that sentence rather than a
// broken image, so the words reach the reader that the route wrote them for.

/** A fetched picture: an object URL to show, and the moment the server says it is of. */
export interface FetchedImage {
  url: string
  /** `X-Frame-Time` when the route sends one; the requested moment is the caller's to remember. */
  frameTime?: number
}

/** The route said there is no picture (404). `message` is its sentence. */
export class NoImageError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'NoImageError'
  }
}

async function bodyText(res: Response): Promise<string> {
  const text = await res.text().catch(() => '')
  try {
    const parsed: unknown = JSON.parse(text)
    const detail = (parsed as { detail?: unknown })?.detail
    if (typeof detail === 'string') return detail
  } catch {
    // not JSON: the text itself is the sentence
  }
  return text || res.statusText
}

export async function fetchImage(
  url: string, signal: AbortSignal, init: RequestInit = {},
): Promise<FetchedImage> {
  const res = await fetch(url, { ...init, signal })
  if (res.status === 404) throw new NoImageError(await bodyText(res))
  if (!res.ok) throw new Error(`${res.status} ${await bodyText(res)}`)
  const blob = await res.blob()
  const header = res.headers.get('X-Frame-Time')
  const frameTime = header === null ? undefined : Number(header)
  return {
    url: URL.createObjectURL(blob),
    frameTime: frameTime !== undefined && Number.isFinite(frameTime) ? frameTime : undefined,
  }
}
