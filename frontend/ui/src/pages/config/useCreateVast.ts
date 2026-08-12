import { useCallback } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { robovast } from '@/lib/robovastClient'
import { MINIMAL_VAST } from '@/lib/vastTemplate'
import { useDialogs } from '@/components/DialogProvider'

const isVast = (p: string) => p.endsWith('.vast')

// Prompt for a name, write a minimal .vast scaffold, refresh the file list, and hand the new path to
// `onCreated`. Shared by the Configuration and Files views so both create files the same way.
export function useCreateVast(
  workspaceId: string,
  existingNames: string[],
  onCreated: (name: string) => void,
) {
  const qc = useQueryClient()
  const { prompt } = useDialogs()

  return useCallback(async () => {
    const name = await prompt({
      title: 'New .vast file',
      label: 'File name',
      defaultValue: 'config.vast',
      confirmLabel: 'Create',
      validate: (v) => {
        if (!v) return 'Enter a file name'
        if (!isVast(v)) return 'Name must end in .vast'
        if (existingNames.includes(v)) return 'A file with this name already exists'
        return null
      },
    })
    if (!name) return
    await robovast.writeProjectFile(workspaceId, name, MINIMAL_VAST)
    await qc.invalidateQueries({ queryKey: ['files', workspaceId] })
    onCreated(name)
  }, [workspaceId, existingNames, onCreated, prompt, qc])
}
