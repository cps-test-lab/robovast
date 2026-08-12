// Flatten a drag-and-drop DataTransfer (a dropped folder, or loose files) into a
// list of files paired with their directory-relative paths, so a whole workspace
// directory can be uploaded in one gesture. Uses the de-facto-standard
// webkitGetAsEntry API (Chromium/Firefox/WebKit); we fail loudly if it is absent
// rather than silently dropping the folder structure.

export interface DroppedFile {
  relPath: string
  file: File
}

// Minimal shape of the FileSystem*Entry API we rely on (not in older lib.dom typings).
interface FsEntry {
  isFile: boolean
  isDirectory: boolean
  name: string
  file(onOk: (f: File) => void, onErr: (e: unknown) => void): void
  createReader(): {
    readEntries(onOk: (entries: FsEntry[]) => void, onErr: (e: unknown) => void): void
  }
}

const entryFile = (e: FsEntry) => new Promise<File>((res, rej) => e.file(res, rej))

// readEntries() returns at most ~100 children per call, so drain it until empty.
function readAllChildren(reader: ReturnType<FsEntry['createReader']>): Promise<FsEntry[]> {
  const out: FsEntry[] = []
  return new Promise((resolve, reject) => {
    const pump = () =>
      reader.readEntries((batch) => {
        if (!batch.length) return resolve(out)
        out.push(...batch)
        pump()
      }, reject)
    pump()
  })
}

async function walk(entry: FsEntry, prefix: string, out: DroppedFile[]): Promise<void> {
  if (entry.isFile) {
    out.push({ relPath: prefix + entry.name, file: await entryFile(entry) })
  } else if (entry.isDirectory) {
    const children = await readAllChildren(entry.createReader())
    for (const child of children) await walk(child, `${prefix}${entry.name}/`, out)
  }
}

export async function collectDroppedFiles(dt: DataTransfer): Promise<DroppedFile[]> {
  const items = Array.from(dt.items).filter((i) => i.kind === 'file')
  if (!items.length) return []
  if (typeof (items[0] as { webkitGetAsEntry?: unknown }).webkitGetAsEntry !== 'function') {
    throw new Error('This browser does not support dropping folders to upload.')
  }
  // Grab every entry *synchronously* — the DataTransfer is neutered once the drop
  // handler returns, so we must not await before collecting them.
  const entries = items
    .map((i) => (i.webkitGetAsEntry() as unknown as FsEntry | null))
    .filter((e): e is FsEntry => e !== null)
  const out: DroppedFile[] = []
  for (const entry of entries) await walk(entry, '', out)
  return out
}
