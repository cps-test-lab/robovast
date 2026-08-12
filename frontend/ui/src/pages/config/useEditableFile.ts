import { useCallback, useEffect, useRef, useState } from 'react'
import { robovast } from '@/lib/robovastClient'

export type SaveState = 'idle' | 'saving' | 'saved' | 'error'

// Loads a workspace file into an editable buffer and autosaves it (debounced, server-side) — the
// shared engine behind both the Configuration editor and the Files editor. `afterSave` runs after a
// successful write (the Configuration view uses it to validate); errors there surface as 'error'.
export function useEditableFile(
  workspaceId: string,
  path: string,
  afterSave?: (text: string) => Promise<void>,
) {
  const [content, setContent] = useState('')
  const [saving, setSaving] = useState<SaveState>('idle')
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  // Keep the latest afterSave without retriggering the debounce closure identity.
  const afterSaveRef = useRef(afterSave)
  afterSaveRef.current = afterSave

  // Load the selected file's content into the buffer.
  useEffect(() => {
    if (!workspaceId || !path) {
      setContent('')
      setSaving('idle')
      return
    }
    let cancelled = false
    robovast.readProjectFile(workspaceId, path).then((f) => {
      if (!cancelled) {
        setContent(f.content)
        setSaving('idle')
      }
    })
    return () => {
      cancelled = true
    }
  }, [workspaceId, path])

  const onChange = useCallback(
    (value?: string) => {
      const text = value ?? ''
      setContent(text)
      if (!workspaceId || !path) return
      if (timer.current) clearTimeout(timer.current)
      setSaving('saving')
      timer.current = setTimeout(async () => {
        try {
          await robovast.writeProjectFile(workspaceId, path, text)
          await afterSaveRef.current?.(text)
          setSaving('saved')
        } catch {
          setSaving('error')
        }
      }, 600)
    },
    [workspaceId, path],
  )

  return { content, saving, onChange }
}
