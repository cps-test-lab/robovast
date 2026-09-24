// A bounded cache of frames already fetched, keyed by the time the route answered for. Scrubbing
// back over a moment shows the frame it showed before without another request; the bound keeps a
// long session from holding every frame it ever showed. Entries are object URLs, so eviction
// revokes them -- the browser frees nothing until then.

export class FrameCache {
  private readonly map = new Map<number, string>()

  constructor(
    private readonly capacity: number,
    private readonly revoke: (url: string) => void = (url) => URL.revokeObjectURL(url),
  ) {}

  get(t: number): string | undefined {
    const url = this.map.get(t)
    if (url === undefined) return undefined
    // Re-insert so the order of the map is the recency order.
    this.map.delete(t)
    this.map.set(t, url)
    return url
  }

  has(t: number): boolean {
    return this.map.has(t)
  }

  set(t: number, url: string): void {
    const old = this.map.get(t)
    if (old !== undefined && old !== url) this.revoke(old)
    this.map.delete(t)
    this.map.set(t, url)
    while (this.map.size > this.capacity) {
      const [oldest, oldUrl] = this.map.entries().next().value as [number, string]
      this.map.delete(oldest)
      this.revoke(oldUrl)
    }
  }

  get size(): number {
    return this.map.size
  }

  clear(): void {
    for (const url of this.map.values()) this.revoke(url)
    this.map.clear()
  }
}
