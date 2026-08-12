import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { robovast, type PreviewResponse, type ValidationReport } from '@/lib/robovastClient'
import { useEditableFile } from './useEditableFile'
import { useCreateVast } from './useCreateVast'

const isVast = (p: string) => p.endsWith('.vast')

export type ConfigEditor = ReturnType<typeof useConfigEditor>

// The shared state of the Config topic's .vast editor: which .vast is selected, its live
// buffer + save status, server-side validation, and the "Generate" preview. Held in one hook
// so the editor pane (a left tab) and the preview pane (always on the right) act on a single
// selection instead of two disconnected copies.
export function useConfigEditor(workspaceId: string) {
  const [selected, setSelected] = useState('')
  const [validation, setValidation] = useState<ValidationReport | null>(null)
  const [preview, setPreview] = useState<PreviewResponse | null>(null)
  const [previewErr, setPreviewErr] = useState<string | null>(null)
  const [selectedCfg, setSelectedCfg] = useState(0)

  const files = useQuery({
    queryKey: ['files', workspaceId],
    queryFn: () => robovast.listProjectFiles(workspaceId),
    enabled: !!workspaceId,
  })

  const vastFiles = useMemo(
    () => (files.data?.entries ?? []).filter(isVast),
    [files.data],
  )
  // Auto-select the first .vast on startup / workspace change; when the selection vanishes
  // (e.g. workspace change) drop back to the first available so something is always picked.
  useEffect(() => {
    if (selected && !vastFiles.includes(selected)) setSelected(vastFiles[0] ?? '')
    else if (!selected && vastFiles.length) setSelected(vastFiles[0])
  }, [vastFiles, selected])

  const { content, saving, onChange } = useEditableFile(workspaceId, selected, async () => {
    setValidation(await robovast.validateProject(workspaceId, selected))
  })

  const generate = useMutation({
    mutationFn: () => robovast.previewConfigurations(workspaceId, 0, selected),
    onSuccess: (p) => {
      setPreview(p)
      setPreviewErr(null)
      setSelectedCfg(0)
    },
    onError: (e) => setPreviewErr((e as Error).message),
  })

  const allNames = useMemo(() => files.data?.entries ?? [], [files.data])
  const createVast = useCreateVast(workspaceId, allNames, (name) => {
    setSelected(name)
    setValidation(null)
    setPreview(null)
  })

  return {
    selected,
    setSelected,
    vastFiles,
    content,
    saving,
    onChange,
    validation,
    createVast,
    generate,
    preview,
    previewErr,
    selectedCfg,
    setSelectedCfg,
  }
}
