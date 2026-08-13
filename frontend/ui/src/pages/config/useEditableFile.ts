import { useCallback, useEffect, useRef, useState } from 'react'
import { robovast } from '@/lib/robovastClient'
import { configFileUrl, isEmptySource, type ConfigSource } from '@/lib/configSource'

export type SaveState = 'idle' | 'saving' | 'saved' | 'error'

// Loads a project file into an editable buffer and autosaves it (debounced, server-side) — the
// engine behind the Configuration editor. `afterSave` runs after a successful write (the
// Configuration view uses it to validate); errors there surface as 'error'.
//
// A read-only source (a campaign's frozen config) never schedules a write at all. That is the point
// of the flag: autosave fires on every keystroke, so "read-only" cannot be left to the editor's
// appearance or to the service answering 405 — by then the request has been made.
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

  const { kind, id } = source

  // Load the selected file's content into the buffer.
  useEffect(() => {
    if (!id || !path) {
      setContent('')
      setSaving('idle')
      return
    }
    let cancelled = false
    robovast.readFileAt(configFileUrl({ kind, id }, path)).then((f) => {
      if (!cancelled) {
        setContent(f.content)
        setSaving('idle')
      }
    })
    return () => {
      cancelled = true
    }
  }, [kind, id, path])

  const onChange = useCallback(
    (value?: string) => {
      // `kind` as well as the flag: only a workspace has a writable address, so the write below is
      // unreachable for a campaign source by construction rather than by the caller pairing the two.
      if (readOnly || kind !== 'workspace') return
      const text = value ?? ''
      setContent(text)
      if (isEmptySource({ kind, id }) || !path) return
      if (timer.current) clearTimeout(timer.current)
      setSaving('saving')
      timer.current = setTimeout(async () => {
        try {
          await robovast.writeProjectFile(id, path, text)
          await afterSaveRef.current?.(text)
          setSaving('saved')
        } catch {
          setSaving('error')
        }
      }, 600)
    },
    [kind, id, path, readOnly],
  )

  return { content, saving, onChange }
}
