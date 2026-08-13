import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import Chip from '@mui/material/Chip'
import Collapse from '@mui/material/Collapse'
import IconButton from '@mui/material/IconButton'
import Paper from '@mui/material/Paper'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import UploadFileRoundedIcon from '@mui/icons-material/UploadFileRounded'
import FolderRoundedIcon from '@mui/icons-material/FolderRounded'
import FolderOpenRoundedIcon from '@mui/icons-material/FolderOpenRounded'
import InsertDriveFileOutlinedIcon from '@mui/icons-material/InsertDriveFileOutlined'
import { robovast } from '@/lib/robovastClient'
import { buildTree, type TreeNode } from '@/lib/fileTree'
import {
  configDirUrl,
  configFilesKey,
  isEmptySource,
  isReadOnlySource,
  type ConfigSource,
} from '@/lib/configSource'
import { useDirectoryUpload } from './useDirectoryUpload'

// The project's file browser: shows its structure and accepts uploads — a single file via the
// button, or a whole folder dropped onto the panel (structure preserved). No editing here: author
// .vast/.osc in the Editor tab, replace other files by re-uploading.
//
// A read-only source (a campaign's frozen config) shows the tree and nothing else: no upload
// button, and the drop handlers are not attached at all, so a dragged folder is the browser's
// problem rather than a request the service refuses.
export function FilesView({ source }: { source: ConfigSource }) {
  const readOnly = isReadOnlySource(source)
  const files = useQuery({
    queryKey: configFilesKey(source),
    queryFn: () => robovast.listFilesAt(configDirUrl(source)),
    enabled: !isEmptySource(source),
    retry: false,
  })
  const paths = useMemo(() => files.data?.entries ?? [], [files.data])
  const tree = useMemo(() => buildTree(paths), [paths])
  const { dragging, progress, error, onDragOver, onDragLeave, onDrop, uploadPicked } =
    useDirectoryUpload(source.kind === 'workspace' ? source.id : '')

  return (
    <Paper
      onDragOver={readOnly ? undefined : onDragOver}
      onDragLeave={readOnly ? undefined : onDragLeave}
      onDrop={readOnly ? undefined : onDrop}
      sx={{
        p: 1,
        height: '100%',
        overflow: 'auto',
        outline: dragging ? '2px dashed' : '2px solid transparent',
        outlineColor: dragging ? 'primary.main' : 'transparent',
        bgcolor: dragging ? 'action.hover' : 'background.paper',
        transition: 'outline-color 120ms, background-color 120ms',
      }}
    >
      <Stack direction="row" alignItems="center" justifyContent="space-between" mb={0.5}>
        <Typography variant="subtitle2" sx={{ pl: 1 }}>
          Files
        </Typography>
        <Stack direction="row" alignItems="center" spacing={0.5}>
          {progress ? <Chip size="small" variant="outlined" label={progress} /> : null}
          {readOnly ? null : (
            <IconButton size="small" component="label" title="Upload a file">
              <UploadFileRoundedIcon fontSize="small" />
              <input
                hidden
                type="file"
                onChange={(e) => {
                  uploadPicked(e.target.files)
                  e.target.value = ''
                }}
              />
            </IconButton>
          )}
        </Stack>
      </Stack>

      {error ? (
        <Alert severity="error" variant="outlined" sx={{ mb: 1 }}>
          {error}
        </Alert>
      ) : null}

      {tree.length ? (
        <TreeList nodes={tree} />
      ) : (
        <Typography variant="caption" color="text.secondary" sx={{ pl: 1 }}>
          {readOnly
            ? 'no files — this campaign froze no configuration'
            : <>no files — drop a project folder here, or author a <code>.vast</code> in the Editor tab</>}
        </Typography>
      )}

      {dragging ? (
        <Box sx={{ p: 2, textAlign: 'center' }}>
          <Typography variant="caption" color="primary">
            Drop to upload into this workspace
          </Typography>
        </Box>
      ) : null}
    </Paper>
  )
}

// A collapsible, read-only file tree. Folders expand/collapse in place; files just render.
function TreeList({ nodes, depth = 0 }: { nodes: TreeNode[]; depth?: number }) {
  return (
    <>
      {nodes.map((node) =>
        node.kind === 'dir' ? (
          <TreeDirRow key={node.path} node={node} depth={depth} />
        ) : (
          <Box
            key={node.path}
            sx={{
              display: 'flex',
              alignItems: 'center',
              gap: 0.75,
              px: 1,
              py: 0.4,
              pl: 1 + depth * 2,
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
  depth,
}: {
  node: Extract<TreeNode, { kind: 'dir' }>
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
        <TreeList nodes={node.children} depth={depth + 1} />
      </Collapse>
    </>
  )
}
