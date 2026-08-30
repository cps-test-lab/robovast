import { useCallback, useEffect, useRef, useState } from 'react'
import { useActiveView } from '@/lib/activeView'
import { useDialogs } from '@/components/DialogProvider'
import { robovast } from '@/lib/robovastClient'
import { configFileUrl, isEmptySource, type ConfigSource } from '@/lib/configSource'

export type SaveState = 'idle' | 'saving' | 'saved' | 'error' | 'reloaded'

// Loads a project file into an editable buffer and autosaves it (debounced, server-side) — the
// engine behind the Configuration editor. `afterSave` runs after a successful write (the
// Configuration view uses it to validate); errors there surface as 'error'.
//
// A read-only source (a campaign's frozen config) never schedules a write at all. That is the point
// of the flag: autosave fires on every keystroke, so "read-only" cannot be left to the editor's
// appearance or to the service answering 405 — by then the request has been made.
//
// Autosave is also why this hook has to look at the file again when the page is returned to. The
// buffer is not a draft that can be left stale harmlessly: it is written back on every keystroke,
// so a buffer holding a file's *old* contents does not merely display something out of date, it
// overwrites whoever changed that file in the meantime, on the next character typed. The page is
// kept mounted while hidden (lib/activeView.tsx), so without the check below that window lasts as
// long as the tab is open.
export function useEditableFile(
  source: ConfigSource,
  path: string,
  afterSave?: (text: string) => Promise<void>,
  readOnly = false,
) {
  const [content, setContent] = useState('')
  const [saving, setSaving] = useState<SaveState>('idle')
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  // Keep the latest afterSave without retriggering the debounce closure identity.
  const afterSaveRef = useRef(afterSave)
  afterSaveRef.current = afterSave
  const { confirm } = useDialogs()

  const { kind, id } = source

  // What the server held the last time this hook knew: set when the file is read, and again after
  // every write of our own. This — not the buffer — is what the arrival check compares against.
  // Comparing against the buffer would call every one of our own saves a conflict, since the whole
  // point of autosave is that the two are normally equal.
  const serverText = useRef('')
  // The buffer, readable from an async callback without going stale.
  const contentRef = useRef('')
  contentRef.current = content
  // Which file the buffer is actually showing, for the same reason: the arrival check below awaits
  // a read, and the user can pick a different file while it is in flight. Adopting then would put
  // one file's text into another file's buffer — and autosave would write it there.
  const shown = useRef({ id, path })
  shown.current = { id, path }
  // A write of ours is scheduled or in flight. The check stands down while one is: the file is
  // about to become the buffer anyway, and reading it mid-write answers about neither.
  const writing = useRef(false)
  // A conflict is on screen, so a second one must not be asked about the same file.
  const asking = useRef(false)

  const adopt = useCallback((text: string) => {
    setContent(text)
    contentRef.current = text
    serverText.current = text
  }, [])

  const writeNow = useCallback(
    async (text: string) => {
      try {
        await robovast.writeProjectFile(id, path, text)
        serverText.current = text
        await afterSaveRef.current?.(text)
        setSaving('saved')
      } catch {
        setSaving('error')
      }
    },
    [id, path],
  )

  // Load the selected file's content into the buffer.
  useEffect(() => {
    if (!id || !path) {
      adopt('')
      setSaving('idle')
      return
    }
    let cancelled = false
    robovast.readFileAt(configFileUrl({ kind, id }, path)).then((f) => {
      if (!cancelled) {
        adopt(f.content)
        setSaving('idle')
      }
    })
    return () => {
      cancelled = true
    }
  }, [kind, id, path, adopt])

  // Returning to the Config page: has this file changed underneath the buffer?
  //
  // Three outcomes, and only one of them is worth a question. Unchanged on the server: nothing to
  // do. Changed on the server while the buffer still holds exactly what we last wrote — the
  // ordinary case, since autosave keeps those equal — means there is nothing of the user's to
  // lose, so the new text is adopted and the save chip says `reloaded`, rather than the editor
  // silently swapping its contents. Only when *both* have moved is there a real conflict, and that
  // needs a person: one of the two versions is about to be lost either way.
  const active = useActiveView()
  const wasActive = useRef(active)
  useEffect(() => {
    const entering = active && !wasActive.current
    wasActive.current = active
    if (!entering) return
    // A campaign's frozen config cannot change, and nothing here can write it.
    if (readOnly || kind !== 'workspace') return
    if (!id || !path || isEmptySource({ kind, id })) return
    if (writing.current || asking.current) return
    let cancelled = false
    void (async () => {
      let disk: string
      try {
        disk = (await robovast.readFileAt(configFileUrl({ kind, id }, path))).content
      } catch {
        // A read that failed says nothing about the file. Leave the buffer alone rather than
        // announce a change on the strength of an error.
        return
      }
      if (cancelled || shown.current.id !== id || shown.current.path !== path) return
      const known = serverText.current
      if (disk === known) return
      if (contentRef.current === known) {
        adopt(disk)
        setSaving('reloaded')
        return
      }
      asking.current = true
      try {
        const takeDisk = await confirm({
          title: 'This file changed on disk',
          confirmLabel: 'Load the new version',
          cancelLabel: 'Keep mine',
          message: (
            <>
              <p>
                <b>{path}</b> was changed outside this editor, and the copy here has edits that
                never reached the server.
              </p>
              <p>
                Loading the new version discards those edits. Keeping yours saves them over the
                change on disk.
              </p>
            </>
          ),
        })
        if (cancelled || shown.current.id !== id || shown.current.path !== path) return
        if (takeDisk) {
          adopt(disk)
          setSaving('reloaded')
        } else {
          // "Keep mine" has to mean it. Without this write the buffer wins only once the user
          // happens to type again, and until then the file on disk quietly holds the other
          // version — the answer the dialog was given and the state of the world disagreeing.
          await writeNow(contentRef.current)
        }
      } finally {
        asking.current = false
      }
    })()
    return () => {
      cancelled = true
    }
  }, [active]) // eslint-disable-line react-hooks/exhaustive-deps

  const onChange = useCallback(
    (value?: string) => {
      // `kind` as well as the flag: only a workspace has a writable address, so the write below is
      // unreachable for a campaign source by construction rather than by the caller pairing the two.
      if (readOnly || kind !== 'workspace') return
      const text = value ?? ''
      setContent(text)
      contentRef.current = text
      if (isEmptySource({ kind, id }) || !path) return
      if (timer.current) clearTimeout(timer.current)
      setSaving('saving')
      writing.current = true
      timer.current = setTimeout(async () => {
        try {
          await writeNow(text)
        } finally {
          writing.current = false
        }
      }, 600)
    },
    [kind, id, path, readOnly, writeNow],
  )

  return { content, saving, onChange }
}
