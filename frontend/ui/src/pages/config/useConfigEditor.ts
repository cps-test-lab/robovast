import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { useActiveView } from '@/lib/activeView'
import { robovast, type PreviewResponse, type ValidationReport } from '@/lib/robovastClient'
import {
  configDirUrl,
  configFilesKey,
  configSourceKey,
  isEmptySource,
  isReadOnlySource,
  type ConfigSource,
} from '@/lib/configSource'
import { useEditableFile } from './useEditableFile'
import { useCreateVast } from './useCreateVast'

const isVast = (p: string) => p.endsWith('.vast')

export type ConfigEditor = ReturnType<typeof useConfigEditor>

// The shared state of the Config topic's .vast editor: which .vast is selected, its live
// buffer + save status, server-side validation, and the "Generate" preview. Held in one hook
// so the editor pane (a left tab) and the preview pane (always on the right) act on a single
// selection instead of two disconnected copies.
//
// The project is a ConfigSource, not a workspace id, so the same panes serve a campaign's frozen
// `_config/`. That source is read-only, and validation/Generate are workspace-scoped operations, so
// in read-only mode neither is called at all — see `readOnly` below.
export function useConfigEditor(source: ConfigSource) {
  const [selected, setSelected] = useState('')
  const [validation, setValidation] = useState<ValidationReport | null>(null)
  const [preview, setPreview] = useState<PreviewResponse | null>(null)
  const [previewErr, setPreviewErr] = useState<string | null>(null)
  const [selectedCfg, setSelectedCfg] = useState(0)
  const active = useActiveView()

  const sourceKey = configSourceKey(source)
  const readOnly = isReadOnlySource(source)

  const files = useQuery({
    queryKey: configFilesKey(source),
    queryFn: () => robovast.listFilesAt(configDirUrl(source)),
    // Files change on disk without this tab doing anything, and the page stays mounted once
    // visited, so arriving is when the tree must be re-read. This covers the *listing*; the open
    // file's own contents are checked separately, and against a different question — see
    // useEditableFile, where a stale buffer is a write conflict rather than a stale read.
    enabled: active && !isEmptySource(source),
    // A campaign that froze no config answers 404 — a real answer about that campaign, not a
    // hiccup worth retrying three times before the view can say so.
    retry: false,
  })

  const vastFiles = useMemo(
    () => (files.data?.entries ?? []).filter(isVast),
    [files.data],
  )
  // Auto-select the first .vast on startup / project change; when the selection vanishes
  // (e.g. workspace change) drop back to the first available so something is always picked.
  useEffect(() => {
    if (selected && !vastFiles.includes(selected)) setSelected(vastFiles[0] ?? '')
    else if (!selected && vastFiles.length) setSelected(vastFiles[0])
  }, [vastFiles, selected])

  // A validation report and a preview are about *one* project. Switching project has to clear
  // them, or a workspace's "Valid · 12 configs" stays on screen under a campaign's banner and
  // reads as a statement about the campaign.
  useEffect(() => {
    setValidation(null)
    setPreview(null)
    setPreviewErr(null)
    setSelectedCfg(0)
  }, [sourceKey])

  const { content, saving, onChange } = useEditableFile(source, selected, async () => {
    setValidation(await robovast.validateProject(source.id, selected))
  }, readOnly)

  const generate = useMutation({
    mutationFn: () => robovast.previewConfigurations(source.id, 0, selected),
    onSuccess: (p) => {
      setPreview(p)
      setPreviewErr(null)
      setSelectedCfg(0)
    },
    onError: (e) => setPreviewErr((e as Error).message),
  })

  const allNames = useMemo(() => files.data?.entries ?? [], [files.data])
  const createVast = useCreateVast(source.id, allNames, (name) => {
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
    readOnly,
    /** Why the file list is empty, when it is empty for a reason. A campaign whose snapshot never
     *  got written is the first project that can legitimately fail to load at all. */
    filesError: files.error as Error | null,
  }
}
