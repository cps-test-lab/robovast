import { useCallback, useState } from 'react'
import type { DragEvent } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { robovast } from '@/lib/robovastClient'
import { useDialogs } from '@/components/DialogProvider'
import { collectDroppedFiles, type DroppedFile } from '@/lib/dropEntries'

const isInline = (p: string) => p.endsWith('.vast') || p.endsWith('.osc')

// Uploading files into a workspace, shared by the drop zone and the file-picker button.
// Files keep their directory-relative path (so a dropped folder recreates its structure);
// .vast/.osc go inline, everything else via the upload side channel; collisions prompt with
// Overwrite / Overwrite all / Skip / Cancel. This is the browser twin of `vast workspace update`
// (additive + overwrite — no prune).
export function useDirectoryUpload(workspaceId: string) {
  const qc = useQueryClient()
  const { choose } = useDialogs()
  const [dragging, setDragging] = useState(false)
  const [progress, setProgress] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const run = useCallback(
    async (files: DroppedFile[]) => {
      if (!files.length) return
      // Snapshot existing paths once for collision detection; add to it as we go so a
      // just-uploaded file doesn't re-prompt within the same batch.
      const existing = new Set(
        (await robovast.listProjectFiles(workspaceId)).files.map((f) => f.path),
      )
      let overwriteAll = false
      try {
        for (let i = 0; i < files.length; i++) {
          const { relPath, file } = files[i]
          setProgress(`uploading ${i + 1}/${files.length}…`)
          if (existing.has(relPath) && !overwriteAll) {
            const choice = await choose({
              title: `Overwrite ${relPath}?`,
              message: `"${relPath}" already exists in this workspace.`,
              choices: [
                { label: 'Overwrite', value: 'yes', primary: true },
                { label: 'Overwrite all', value: 'all' },
                { label: 'Skip', value: 'skip' },
                { label: 'Cancel', value: 'cancel' },
              ],
            })
            if (choice === 'cancel' || choice === null) break
            if (choice === 'skip') continue
            if (choice === 'all') overwriteAll = true
          }
          if (isInline(relPath)) {
            await robovast.writeProjectFile(workspaceId, relPath, await file.text())
          } else {
            await robovast.uploadFile(workspaceId, relPath, file)
          }
          existing.add(relPath)
        }
      } finally {
        setProgress(null)
        await qc.invalidateQueries({ queryKey: ['files', workspaceId] })
      }
    },
    [workspaceId, choose, qc],
  )

  const onDragOver = useCallback((e: DragEvent) => {
    e.preventDefault()
    setDragging(true)
  }, [])
  const onDragLeave = useCallback((e: DragEvent) => {
    e.preventDefault()
    setDragging(false)
  }, [])
  const onDrop = useCallback(
    async (e: DragEvent) => {
      e.preventDefault()
      setDragging(false)
      setError(null)
      try {
        await run(await collectDroppedFiles(e.dataTransfer))
      } catch (err) {
        setError((err as Error).message)
      }
    },
    [run],
  )

  // The single-file "Upload" button funnels through the same collision/dispatch path.
  const uploadPicked = useCallback(
    async (list: FileList | null) => {
      if (!list?.length) return
      setError(null)
      try {
        await run(Array.from(list).map((file) => ({ relPath: file.name, file })))
      } catch (err) {
        setError((err as Error).message)
      }
    },
    [run],
  )

  return { dragging, progress, error, onDragOver, onDragLeave, onDrop, uploadPicked }
}
