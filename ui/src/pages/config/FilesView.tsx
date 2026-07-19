import { useEffect, useMemo, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import Editor from '@monaco-editor/react'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import Chip from '@mui/material/Chip'
import Collapse from '@mui/material/Collapse'
import IconButton from '@mui/material/IconButton'
import Paper from '@mui/material/Paper'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import AddRoundedIcon from '@mui/icons-material/AddRounded'
import UploadFileRoundedIcon from '@mui/icons-material/UploadFileRounded'
import FolderRoundedIcon from '@mui/icons-material/FolderRounded'
import FolderOpenRoundedIcon from '@mui/icons-material/FolderOpenRounded'
import InsertDriveFileOutlinedIcon from '@mui/icons-material/InsertDriveFileOutlined'
import { robovast } from '@/lib/robovastClient'
import { buildTree, isReserved, type TreeNode } from '@/lib/fileTree'
import { useDialogs } from '@/components/DialogProvider'
import { useEditableFile } from './useEditableFile'
import { useCreateVast } from './useCreateVast'

const isInline = (p: string) => p.endsWith('.vast') || p.endsWith('.osc')

// Monaco language by extension (highlighting only). Anything unrecognised falls back to plaintext.
function languageFor(path: string): string {
  const ext = path.slice(path.lastIndexOf('.') + 1).toLowerCase()
  const map: Record<string, string> = {
    vast: 'yaml', osc: 'yaml', yaml: 'yaml', yml: 'yaml',
    json: 'json', md: 'markdown', py: 'python', sh: 'shell',
    xml: 'xml', txt: 'plaintext',
  }
  return map[ext] ?? 'plaintext'
}

// The workspace file browser: a tree on the left, a text editor on the right. .vast/.osc files are
// editable (autosaved); other text files are shown read-only (the service only accepts inline writes
// for .vast/.osc — replace others by re-uploading). Reserved artefacts (.cache/.robovast*) are hidden.
export function FilesView({ workspaceId }: { workspaceId: string }) {
  const qc = useQueryClient()
  const { confirm } = useDialogs()
  const [selected, setSelected] = useState('')

  const files = useQuery({
    queryKey: ['files', workspaceId],
    queryFn: () => robovast.listProjectFiles(workspaceId),
    enabled: !!workspaceId,
  })
  const paths = useMemo(() => (files.data?.files ?? []).map((f) => f.path), [files.data])
  const tree = useMemo(() => buildTree(paths), [paths])
  const createVast = useCreateVast(workspaceId, paths, setSelected)

  // Drop a stale selection when the file no longer exists (e.g. after switching workspace).
  useEffect(() => {
    if (selected && files.isSuccess && !paths.includes(selected)) setSelected('')
  }, [paths, selected, files.isSuccess])

  const upload = async (file: File) => {
    // Uploads land at the file's own name (workspace root). Confirm before clobbering an existing one.
    const collides = paths.includes(file.name)
    if (collides) {
      const ok = await confirm({
        title: `Overwrite ${file.name}?`,
        message: `A file named "${file.name}" already exists in this workspace. Replace it?`,
        confirmLabel: 'Overwrite',
        danger: true,
      })
      if (!ok) return
    }
    await robovast.uploadFile(workspaceId, file.name, file)
    await qc.invalidateQueries({ queryKey: ['files', workspaceId] })
  }

  return (
    <Box sx={{ display: 'grid', gridTemplateColumns: '280px 1fr', gap: 2, minHeight: 460 }}>
      {/* Tree */}
      <Paper sx={{ p: 1, overflow: 'auto' }}>
        <Stack direction="row" alignItems="center" justifyContent="space-between" mb={0.5}>
          <Typography variant="subtitle2" sx={{ pl: 1 }}>
            Files
          </Typography>
          <Stack direction="row">
            <IconButton size="small" title="New .vast file" onClick={createVast}>
              <AddRoundedIcon fontSize="small" />
            </IconButton>
            <IconButton size="small" component="label" title="Upload a file">
              <UploadFileRoundedIcon fontSize="small" />
              <input
                hidden
                type="file"
                onChange={(e) => {
                  const f = e.target.files?.[0]
                  if (f) upload(f)
                  e.target.value = ''
                }}
              />
            </IconButton>
          </Stack>
        </Stack>
        {tree.length ? (
          <TreeList nodes={tree} selected={selected} onSelect={setSelected} />
        ) : (
          <Typography variant="caption" color="text.secondary" sx={{ pl: 1 }}>
            no files — create a <code>.vast</code> or upload a scenario file
          </Typography>
        )}
      </Paper>

      {/* Editor */}
      <FileEditor workspaceId={workspaceId} path={selected} />
    </Box>
  )
}

// A collapsible file tree. Folders expand/collapse in place; files are selectable.
function TreeList({
  nodes,
  selected,
  onSelect,
  depth = 0,
}: {
  nodes: TreeNode[]
  selected: string
  onSelect: (path: string) => void
  depth?: number
}) {
  return (
    <>
      {nodes.map((node) =>
        node.kind === 'dir' ? (
          <TreeDirRow key={node.path} node={node} selected={selected} onSelect={onSelect} depth={depth} />
        ) : (
          <Box
            key={node.path}
            onClick={() => onSelect(node.path)}
            sx={{
              display: 'flex',
              alignItems: 'center',
              gap: 0.75,
              px: 1,
              py: 0.4,
              pl: 1 + depth * 2,
              borderRadius: 1,
              cursor: 'pointer',
              bgcolor: node.path === selected ? 'rgba(45, 212, 191, 0.14)' : 'transparent',
              '&:hover': { bgcolor: node.path === selected ? 'rgba(45, 212, 191, 0.14)' : 'action.hover' },
            }}
          >
            <InsertDriveFileOutlinedIcon sx={{ fontSize: 16, color: 'text.secondary' }} />
            <Typography variant="caption" sx={{ fontFamily: 'monospace' }} noWrap>
              {node.name}
            </Typography>
          </Box>
        ),
      )}
    </>
  )
}

function TreeDirRow({
  node,
  selected,
  onSelect,
  depth,
}: {
  node: Extract<TreeNode, { kind: 'dir' }>
  selected: string
  onSelect: (path: string) => void
  depth: number
}) {
  const [open, setOpen] = useState(true)
  return (
    <>
      <Box
        onClick={() => setOpen((o) => !o)}
        sx={{
          display: 'flex',
          alignItems: 'center',
          gap: 0.75,
          px: 1,
          py: 0.4,
          pl: 1 + depth * 2,
          borderRadius: 1,
          cursor: 'pointer',
          '&:hover': { bgcolor: 'action.hover' },
        }}
      >
        {open ? (
          <FolderOpenRoundedIcon sx={{ fontSize: 16, color: 'primary.main' }} />
        ) : (
          <FolderRoundedIcon sx={{ fontSize: 16, color: 'primary.main' }} />
        )}
        <Typography variant="caption" sx={{ fontWeight: 600 }} noWrap>
          {node.name}
        </Typography>
      </Box>
      <Collapse in={open} unmountOnExit>
        <TreeList nodes={node.children} selected={selected} onSelect={onSelect} depth={depth + 1} />
      </Collapse>
    </>
  )
}

// Right pane: shows the selected file. Editable + autosaved for .vast/.osc; read-only for other text;
// a short note for binary files (which are managed via upload, not inline editing).
function FileEditor({ workspaceId, path }: { workspaceId: string; path: string }) {
  const editable = !!path && isInline(path)
  const { content, saving, onChange } = useEditableFile(workspaceId, path)
  // read_file replaces undecodable bytes; a NUL byte is a reliable "this is binary" signal.
  const binary = !!path && content.includes(String.fromCharCode(0))

  if (!path) {
    return (
      <Alert severity="info" variant="outlined">
        Select a file to view or edit it.
      </Alert>
    )
  }
  if (isReserved(path)) {
    return <Alert severity="warning">Reserved file — not editable.</Alert>
  }

  return (
    <Stack spacing={1} sx={{ minWidth: 0 }}>
      <Stack direction="row" spacing={1} alignItems="center">
        <Typography variant="caption" sx={{ fontFamily: 'monospace' }}>
          {path}
        </Typography>
        <Box flexGrow={1} />
        {editable ? (
          <Chip
            size="small"
            variant="outlined"
            label={saving === 'saving' ? 'saving…' : saving === 'saved' ? 'saved' : saving === 'error' ? 'save failed' : '—'}
            color={saving === 'error' ? 'error' : saving === 'saved' ? 'success' : 'default'}
          />
        ) : (
          <Chip size="small" variant="outlined" label="read-only" />
        )}
      </Stack>
      {binary ? (
        <Alert severity="info" variant="outlined">
          Binary file — not editable here. Re-upload to replace it.
        </Alert>
      ) : (
        <Paper sx={{ height: 460, overflow: 'hidden' }}>
          <Editor
            height="460px"
            language={languageFor(path)}
            path={path}
            value={content}
            onChange={editable ? onChange : undefined}
            theme="vs-dark"
            options={{
              minimap: { enabled: false },
              fontSize: 13,
              scrollBeyondLastLine: false,
              readOnly: !editable,
            }}
          />
        </Paper>
      )}
      {!editable && !binary ? (
        <Typography variant="caption" color="text.secondary">
          Only <code>.vast</code>/<code>.osc</code> files are editable here; replace others by re-uploading.
        </Typography>
      ) : null}
    </Stack>
  )
}
