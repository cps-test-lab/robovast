// Which file an autosaving buffer holds, so a write can only ever go back to that file.
//
// The editor's buffer is written back on every keystroke. Selecting another file starts a read, and
// until that read lands the buffer still holds the previous file's text -- under the new path. If
// the read fails it never lands, and a write from then on would put one file's text into another.
// So the buffer is writable for a file only once that file's own text has been read into it.

export type FileKey = { id: string; path: string }

const same = (a: FileKey | null, b: FileKey) => !!a && a.id === b.id && a.path === b.path

export class BufferOwner {
  /** The file whose text the buffer holds, or null while none does. */
  private holds: FileKey | null = null

  /** A read of `key` has started: until it lands, the buffer belongs to no file. */
  begin(): void {
    this.holds = null
  }

  /** `key`'s text is now in the buffer. */
  loaded(key: FileKey): void {
    this.holds = { ...key }
  }

  /** Whether the buffer may be written to `key`. */
  mayWrite(key: FileKey): boolean {
    return same(this.holds, key)
  }
}

/** The service's sentence from a failed call: the message alone, never `Error: …`. */
export const failureText = (e: unknown): string => (e instanceof Error ? e.message : String(e))
